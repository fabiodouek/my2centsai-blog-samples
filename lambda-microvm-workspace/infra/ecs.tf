# Backend (FastAPI, port 8000) as the ECS Fargate service `workspace-api` on part 2's cluster,
# in part 2's private proxy subnets (egress via its NAT gateway).

resource "aws_cloudwatch_log_group" "api" {
  name              = "/my2cents/workspace-api"
  retention_in_days = 7
}

# CloudFront <-> backend shared secret, injected into the task as a secret (not plain env).
# SecureString with the AWS-managed aws/ssm key: free, and the execution role needs no kms grant.
resource "aws_ssm_parameter" "origin_secret" {
  name  = "/${local.name}/origin-secret"
  type  = "SecureString"
  value = random_password.origin_secret.result
}

data "aws_iam_policy_document" "ecs_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# ---------- Execution role: image pull, logs, the origin secret ----------

resource "aws_iam_role" "exec" {
  name               = "${local.name}-api-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_trust.json
}

resource "aws_iam_role_policy_attachment" "exec" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "exec_secret" {
  role = aws_iam_role.exec.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "ssm:GetParameters", Resource = aws_ssm_parameter.origin_secret.arn }]
  })
}

# ---------- Task role: what the backend itself may do ----------

resource "aws_iam_role" "task" {
  name               = "${local.name}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_trust.json
}

locals {
  secret_arns = values(local.p2.secret_arns)
  log_groups  = [local.proxy_log_group_arn, local.dns_log_group_arn]
}

resource "aws_iam_role_policy" "task" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Per the service authorization reference, every per-MicroVM action authorizes against the
      # microvm-image resource the MicroVM was launched from, so this scopes the backend to
      # MicroVMs of its own images (one per size). ":*" covers versioned image ARNs.
      { Sid = "MicrovmsOfWorkspaceImage", Effect = "Allow",
        Action = [
          "lambda:RunMicrovm", "lambda:GetMicrovm", "lambda:SuspendMicrovm", "lambda:ResumeMicrovm",
          "lambda:TerminateMicrovm", "lambda:CreateMicrovmAuthToken", "lambda:CreateMicrovmShellAuthToken",
          "lambda:GetMicrovmImage",
        ],
      Resource = flatten([for arn in values(local.image_arns) : [arn, "${arn}:*"]]) },
      # ListMicrovms has no resource type
      { Sid = "ListMicrovms", Effect = "Allow", Action = "lambda:ListMicrovms", Resource = "*" },
      # The account's MicroVM memory quota (app/quota.py): "Max allocated ARM_64 MicroVM memory"
      { Sid = "MicrovmMemoryQuota", Effect = "Allow", Action = "servicequotas:GetServiceQuota",
      Resource = "arn:aws:servicequotas:${var.region}:${local.account}:lambda/L-CD1C0CC4" },
      { Sid = "PassConnectors", Effect = "Allow", Action = "lambda:PassNetworkConnector",
      Resource = [local.p2.connector_arn, "arn:aws:lambda:${var.region}:aws:network-connector:aws-network-connector:*"] },
      { Sid = "SandboxTable", Effect = "Allow",
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
          "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchGetItem", "dynamodb:BatchWriteItem",
        ],
      Resource = aws_dynamodb_table.sandboxes.arn },
      { Sid    = "ReadProxyAndDnsLogs", Effect = "Allow",
        Action = ["logs:StartLiveTail", "logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams"],
      Resource = concat(local.log_groups, [for a in local.log_groups : "${a}:*"]) },
      # Revoke = deny policy + a forced new deployment of iron-proxy (it keeps a cached secret value when a
      # refresh fails, so only a fresh task really loses it). Scoped to that one service.
      { Sid    = "RestartIronProxy", Effect = "Allow",
        Action = ["ecs:UpdateService", "ecs:DescribeServices"],
      Resource = "arn:aws:ecs:${var.region}:${local.account}:service/${local.p2.ecs_cluster}/${local.p2.ecs_service}" },
      # Revoke/restore: a resource policy denying iron-proxy's task role GetSecretValue.
      # No GetSecretValue here: the backend never reads secret values.
      { Sid    = "RevokeRestoreSecrets", Effect = "Allow",
        Action = ["secretsmanager:GetResourcePolicy", "secretsmanager:PutResourcePolicy", "secretsmanager:DeleteResourcePolicy", "secretsmanager:DescribeSecret"],
      Resource = local.secret_arns },
      { Sid = "PolicyChangeLog", Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:Query"], Resource = aws_dynamodb_table.changes.arn },
      # The AWS access catalog greys out what the chosen sandbox role can't do: it reads the roles' policies
      # (part 2's my2cents-ip-<role>) and the AWS managed policies they attach. Read only.
      { Sid    = "ReadSandboxRolePolicies", Effect = "Allow",
        Action = ["iam:ListAttachedRolePolicies", "iam:ListRolePolicies", "iam:GetRolePolicy"],
      Resource = "arn:aws:iam::${local.account}:role/my2cents-ip-*" },
      { Sid = "ReadManagedPolicies", Effect = "Allow", Action = ["iam:GetPolicy", "iam:GetPolicyVersion"],
      Resource = "arn:aws:iam::aws:policy/*" },
    ]
  })
}

# Proxy control plane (part 2's control_plane=true): edit the policy objects in the control bucket, read the
# sidecar's status, and manage the credentials admins add under my2cents-ip/user/. Writing a value is
# allowed (create, put); reading one is not.
resource "aws_iam_role_policy" "task_control" {
  count = local.control_on ? 1 : 0
  name  = "proxy-control-plane"
  role  = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "EditPolicy", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"],
      Resource = [for k in ["config", "scope", "credentials"] : "arn:aws:s3:::${local.control_bucket}/${local.control_keys[k]}"] },
      { Sid = "ReadStatus", Effect = "Allow", Action = "s3:GetObject", Resource = "arn:aws:s3:::${local.control_bucket}/${local.control_keys.status}" },
      # Part 2's deploy-time config, for the factory reset (older part 2 states have no such key: the default name)
      { Sid = "ReadDefaultConfig", Effect = "Allow", Action = "s3:GetObject",
      Resource = "arn:aws:s3:::${local.control_bucket}/${try(local.control_keys.default_config, "iron-proxy.default.yaml")}" },
      # Without ListBucket a missing object reads as 403 instead of 404
      { Sid = "ListControlBucket", Effect = "Allow", Action = "s3:ListBucket", Resource = "arn:aws:s3:::${local.control_bucket}" },
      { Sid = "UserCredentials", Effect = "Allow",
        Action = ["secretsmanager:CreateSecret", "secretsmanager:PutSecretValue", "secretsmanager:DeleteSecret", "secretsmanager:RestoreSecret",
          "secretsmanager:DescribeSecret", "secretsmanager:TagResource", "secretsmanager:GetResourcePolicy",
        "secretsmanager:PutResourcePolicy", "secretsmanager:DeleteResourcePolicy"],
      Resource = "arn:aws:secretsmanager:${var.region}:${local.account}:secret:${local.user_prefix}*" },
    ]
  })
}

# ---------- Task definition and service ----------

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.exec.arn
  task_role_arn            = aws_iam_role.task.arn
  runtime_platform {
    cpu_architecture        = "ARM64"
    operating_system_family = "LINUX"
  }
  container_definitions = jsonencode([{
    name         = "api"
    image        = "${aws_ecr_repository.backend.repository_url}:${var.image_tag}"
    essential    = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    environment = concat([
      { name = "TABLE_NAME", value = aws_dynamodb_table.sandboxes.name },
      { name = "COGNITO_USER_POOL_ID", value = aws_cognito_user_pool.pool.id },
      { name = "COGNITO_CLIENT_ID", value = aws_cognito_user_pool_client.spa.id },
      { name = "AWS_REGION", value = var.region },
      { name = "IMAGE_ARN", value = local.image_arn },
      { name = "IMAGE_ARNS_JSON", value = jsonencode(local.image_arns) },
      # Part 2's sandbox connector: transparent interception, no proxy settings in the sandbox
      { name = "CONNECTOR_ARN", value = local.p2.connector_arn },
      { name = "PLACEHOLDERS_JSON", value = jsonencode(local.p2.placeholders) },
      { name = "SECRET_ARNS_JSON", value = jsonencode(local.p2.secret_arns) },
      { name = "PROXY_LOG_GROUP_ARN", value = local.proxy_log_group_arn },
      { name = "DNS_LOG_GROUP_ARN", value = local.dns_log_group_arn },
      { name = "TASK_ROLE_ARN", value = local.p2.task_role_arn },
      { name = "PROXY_ECS_CLUSTER", value = local.p2.ecs_cluster },
      { name = "PROXY_ECS_SERVICE", value = local.p2.ecs_service },
      { name = "SANDBOX_CIDRS", value = join(",", sort(local.p2.connector_subnet_cidrs)) },
      { name = "CHANGES_TABLE", value = aws_dynamodb_table.changes.name },
      { name = "ADMIN_GROUP", value = aws_cognito_user_group.admins.name },
      ], local.control_on ? [
      { name = "CONTROL_BUCKET", value = local.control_bucket },
      { name = "CONTROL_KEYS_JSON", value = jsonencode(local.control_keys) },
      { name = "ROLES_JSON", value = jsonencode(local.sandbox_roles) },
      { name = "SCOPE_BASELINE_JSON", value = jsonencode(local.scope_baseline) },
      { name = "USER_SECRET_PREFIX", value = local.user_prefix },
      { name = "CONTROL_POLL_S", value = tostring(local.control_poll_s) },
    ] : [])
    secrets = [{ name = "ORIGIN_SECRET", valueFrom = aws_ssm_parameter.origin_secret.arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "api"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name                              = "workspace-api"
  cluster                           = local.p2.ecs_cluster_arn
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = var.desired_count
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 30
  network_configuration {
    subnets          = local.p2.proxy_subnet_ids
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }
  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
  depends_on = [aws_lb_listener_rule.from_cloudfront]
}
