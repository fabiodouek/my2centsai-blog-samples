# control_plane=true: the roles sandbox AWS calls can be signed as. Part 3's admin picks one (view-only by default)
# and may narrow it further with a session policy. The control sidecar assumes it with the iron-proxy task role and
# hands iron-proxy the credentials, so iron-proxy also fetches its injected secrets with this session: every role
# allows that (the baseline), and nothing else in Secrets Manager or SSM parameter values.
#
#   view-only    ViewOnlyAccess: list and describe, no data reads
#   read-only    ReadOnlyAccess: reads, data included
#   data-reader  the demo bucket and my2cents-demo-* tables, read-only; nothing else
#   builder      PowerUserAccess, in this Region only, and no changes to this stack, part 3's or what they run under

locals {
  default_role = "view-only"

  # The secrets iron-proxy injects: this stack's three and the ones part 3 adds under var.user_secret_prefix
  proxy_secret_arns = concat([for k in ["anthropic", "github", "demo"] : aws_secretsmanager_secret.injected[k].arn],
  ["arn:aws:secretsmanager:${var.region}:${local.account}:secret:${var.user_secret_prefix}*"])

  # On every role. The baseline is also the first statement of every session policy (the sidecar adds it).
  scope_baseline = {
    Sid      = "ProxySecrets"
    Effect   = "Allow"
    Action   = "secretsmanager:GetSecretValue"
    Resource = local.proxy_secret_arns
  }
  role_guards = [
    local.scope_baseline,
    # SecureString values come back decrypted with the AWS-managed key
    { Sid = "NoParameterValues", Effect = "Deny", Action = "ssm:GetParameter*", Resource = "*" },
    { Sid = "OnlyReadProxySecrets", Effect = "Deny", NotAction = "secretsmanager:GetSecretValue", Resource = local.proxy_secret_arns },
    { Sid = "NoOtherSecrets", Effect = "Deny", Action = "secretsmanager:*", NotResource = local.proxy_secret_arns },
  ]

  demo_bucket_arn = aws_s3_bucket.demo.arn
  data_reader = [
    { Sid = "DemoBucket", Effect = "Allow", Action = ["s3:GetObject", "s3:ListBucket"],
    Resource = [local.demo_bucket_arn, "${local.demo_bucket_arn}/*"] },
    { Sid    = "DemoTables", Effect = "Allow",
      Action = ["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:DescribeTable"],
    Resource = ["arn:aws:dynamodb:${var.region}:${local.account}:table/my2cents-demo-*", "arn:aws:dynamodb:${var.region}:${local.account}:table/my2cents-demo-*/index/*"] },
  ]

  # Services without a Region (or billed globally) stay usable under the Region lock
  global_actions = [
    "a4b:*", "account:*", "acm:*", "aws-marketplace-management:*", "aws-marketplace:*", "aws-portal:*", "billing:*",
    "budgets:*", "ce:*", "chime:*", "cloudfront:*", "cur:*", "directconnect:*", "ec2:DescribeRegions",
    "ec2:DescribeTransitGateways", "ec2:DescribeVpnGateways", "fms:*", "freetier:*", "globalaccelerator:*", "health:*",
    "iam:*", "importexport:*", "invoicing:*", "networkmanager:*", "organizations:*", "payments:*", "pricing:*",
    "route53:*", "route53domains:*", "route53-recovery-cluster:*", "route53-recovery-control-config:*",
    "route53-recovery-readiness:*", "s3:GetAccountPublic*", "s3:ListAllMyBuckets", "s3:ListMultiRegionAccessPoints",
    "s3:PutAccountPublic*", "savingsplans:*", "shield:*", "sts:*", "support:*", "tax:*", "trustedadvisor:*",
    "waf-regional:*", "waf:*", "wafv2:*", "wellarchitected:*",
  ]
  ec2_writes = ["ec2:Create*", "ec2:Delete*", "ec2:Modify*", "ec2:Replace*", "ec2:Authorize*", "ec2:Revoke*",
    "ec2:Associate*", "ec2:Disassociate*", "ec2:Attach*", "ec2:Detach*", "ec2:Accept*", "ec2:Reject*", "ec2:Enable*",
    "ec2:Disable*", "ec2:Allocate*", "ec2:Release*", "ec2:Assign*", "ec2:Unassign*", "ec2:RunInstances",
  "ec2:StartInstances", "ec2:StopInstances", "ec2:TerminateInstances", "ec2:RebootInstances"]
  arn = "arn:aws:%s:*:${local.account}:%s"

  # The networks every sandbox depends on: both VPCs and the peering between them
  stack_vpc_arns       = [aws_vpc.proxy.arn, aws_vpc.sandbox.arn]
  stack_peering_arn    = "arn:aws:ec2:${var.region}:${local.account}:vpc-peering-connection/${aws_vpc_peering_connection.p.id}"
  stack_zone_arns      = [aws_route53_zone.sandbox.arn]
  stack_connector_arns = [aws_lambdacore_network_connector.sandbox.arn]

  # builder: Denies win over PowerUserAccess. Stack resources are all named my2cents-*; where a service has no
  # usable name in its ARNs (Resolver, CloudFront, Cognito pools, connectors) the guard covers the whole service.
  builder_guards = [
    { Sid = "RegionLock", Effect = "Deny", NotAction = local.global_actions, Resource = "*",
    Condition = { StringNotEquals = { "aws:RequestedRegion" = [var.region] } } },
    # aws:RequestedRegion is the endpoint's Region: S3 can still create a bucket elsewhere through the us-east-1
    # endpoint (LocationConstraint), so that key is checked too (absent means us-east-1)
    { Sid = "S3RegionLock", Effect = "Deny", Action = "s3:CreateBucket", Resource = "*",
    Condition = { StringNotEquals = { "s3:LocationConstraint" = [var.region] }, Null = { "s3:LocationConstraint" = "false" } } },
    # Nothing that would let it act as someone else: no role assumption (roles that trust the account would accept it)
    { Sid = "NoAssumeRole", Effect = "Deny", Action = ["sts:AssumeRole", "sts:SetSourceIdentity", "sts:TagSession", "sts:GetFederationToken"], Resource = "*" },
    # The control bucket (its scope and config), the SPA, artifacts and demo buckets: read only
    { Sid = "NoStackBuckets", Effect = "Deny", NotAction = ["s3:Get*", "s3:List*"], Resource = ["arn:aws:s3:::my2cents-*", "arn:aws:s3:::my2cents-*/*"] },
    # iron-proxy (and part 3's API) run on the my2cents-ip cluster
    { Sid = "NoStackEcs", Effect = "Deny", NotAction = ["ecs:Describe*", "ecs:List*"],
      Resource = [format(local.arn, "ecs", "cluster/my2cents-*"), format(local.arn, "ecs", "service/my2cents-*/*"),
        format(local.arn, "ecs", "task/my2cents-*/*"), format(local.arn, "ecs", "task-definition/my2cents-*:*"),
    format(local.arn, "ecs", "container-instance/my2cents-*/*"), format(local.arn, "ecs", "task-set/my2cents-*/*/*")] },
    { Sid = "NoTasksOnStackCluster", Effect = "Deny", Action = ["ecs:RunTask", "ecs:StartTask"], Resource = "*",
    Condition = { ArnLike = { "ecs:cluster" = format(local.arn, "ecs", "cluster/my2cents-*") } } },
    # Route 53 Resolver: DNS Firewall rule groups, domain lists, VPC associations, query logging
    { Sid = "NoResolverChanges", Effect = "Deny",
      Action = ["route53resolver:Associate*", "route53resolver:Disassociate*", "route53resolver:Create*", "route53resolver:Delete*",
      "route53resolver:Update*", "route53resolver:Put*", "route53resolver:Import*", "route53resolver:TagResource", "route53resolver:UntagResource"],
    Resource = "*" },
    # The VPC: security groups, routes, subnets, NAT, peering, ENIs (the egress connector's included)
    { Sid = "NoStackNetworkInVpc", Effect = "Deny", Action = local.ec2_writes, Resource = "*",
    Condition = { ArnEquals = { "ec2:Vpc" = local.stack_vpc_arns } } },
    { Sid = "NoStackVpc", Effect = "Deny", Action = local.ec2_writes, Resource = local.stack_vpc_arns },
    { Sid = "NoStackPeering", Effect = "Deny", Action = local.ec2_writes, Resource = local.stack_peering_arn },
    { Sid = "NoStackNetworkTagged", Effect = "Deny", Action = local.ec2_writes, Resource = "*",
    Condition = { StringLike = { "aws:ResourceTag/project" = "my2cents-*" } } },
    { Sid = "NoStackLoadBalancers", Effect = "Deny", NotAction = ["elasticloadbalancing:Describe*"],
      Resource = [format(local.arn, "elasticloadbalancing", "loadbalancer/*/my2cents-*/*"), format(local.arn, "elasticloadbalancing", "targetgroup/my2cents-*/*"),
    format(local.arn, "elasticloadbalancing", "listener/*/my2cents-*/*"), format(local.arn, "elasticloadbalancing", "listener-rule/*/my2cents-*/*")] },
    { Sid = "NoConnectorChanges", Effect = "Deny", Action = ["lambda:CreateNetworkConnector", "lambda:DeleteNetworkConnector", "lambda:UpdateNetworkConnector"], Resource = "*" },
    { Sid = "NoStackConnector", Effect = "Deny", Action = "lambda:PassNetworkConnector", Resource = local.stack_connector_arns },
    # Other people's sandboxes: no run, suspend, terminate, shell or endpoint tokens on my2cents-* images
    { Sid = "NoStackMicroVMs", Effect = "Deny", NotAction = ["lambda:Get*", "lambda:List*"],
    Resource = [format(local.arn, "lambda", "microvm-image:my2cents-*"), format(local.arn, "lambda", "microvm-image:my2cents-*:*")] },
    { Sid = "NoStackTables", Effect = "Deny", NotAction = ["dynamodb:Get*", "dynamodb:BatchGet*", "dynamodb:Query", "dynamodb:Scan", "dynamodb:Describe*", "dynamodb:List*"],
    Resource = [format(local.arn, "dynamodb", "table/my2cents-ws-*"), format(local.arn, "dynamodb", "table/my2cents-ws-*/*")] },
    # The backend's image repository: an image pushed over its tag would run with the backend's role
    { Sid = "NoStackImages", Effect = "Deny", NotAction = ["ecr:Describe*", "ecr:List*", "ecr:Get*", "ecr:BatchGet*", "ecr:BatchCheck*"],
    Resource = format(local.arn, "ecr", "repository/my2cents-*") },
    # The proxies' audit logs, the DNS query logs, the alarm log and the API's log: no deleting, no injected lines
    { Sid = "NoStackLogs", Effect = "Deny", NotAction = ["logs:Describe*", "logs:Get*", "logs:List*", "logs:FilterLogEvents", "logs:StartLiveTail", "logs:StartQuery"],
    Resource = [format(local.arn, "logs", "log-group:/my2cents*"), format(local.arn, "logs", "log-group:/aws/events/my2cents-*")] },
    # The private zones every sandbox lookup is rewritten to: no record changes, no deleting them, and no private
    # zones attached to any VPC (one on the proxy VPC could change the addresses iron-proxy connects to, and one
    # overlapping sandbox.internal could redirect the override's target)
    { Sid = "NoStackZones", Effect = "Deny", NotAction = ["route53:Get*", "route53:List*"], Resource = local.stack_zone_arns },
    { Sid    = "NoPrivateZones", Effect = "Deny",
      Action = ["route53:AssociateVPCWithHostedZone", "route53:DisassociateVPCFromHostedZone", "route53:CreateVPCAssociationAuthorization"],
    Resource = "*" },
    { Sid = "NoNewPrivateZones", Effect = "Deny", Action = "route53:CreateHostedZone", Resource = "*",
    Condition = { Null = { "route53:VPCs" = "false" } } },
    # The DNS Firewall alarm (EventBridge rule, SNS topic): no disabling or retargeting it
    { Sid = "NoStackAlarm", Effect = "Deny", NotAction = ["events:Describe*", "events:List*", "sns:Get*", "sns:List*"],
    Resource = [format(local.arn, "events", "rule/my2cents-*"), format(local.arn, "sns", "my2cents-*")] },
    { Sid = "NoStackParameters", Effect = "Deny", NotAction = ["ssm:Describe*", "ssm:List*"], Resource = format(local.arn, "ssm", "parameter/my2cents-*") },
    { Sid = "NoCloudFrontChanges", Effect = "Deny",
      Action = ["cloudfront:Create*", "cloudfront:Update*", "cloudfront:Delete*", "cloudfront:Associate*", "cloudfront:Disassociate*",
      "cloudfront:Copy*", "cloudfront:Publish*", "cloudfront:Put*", "cloudfront:TagResource", "cloudfront:UntagResource"],
    Resource = "*" },
    # Cognito: no admin group changes, users, passwords or app clients (the workspace's admins are a Cognito group)
    { Sid = "NoCognitoChanges", Effect = "Deny", NotAction = ["cognito-idp:Describe*", "cognito-idp:List*", "cognito-idp:Get*"], Resource = format(local.arn, "cognito-idp", "userpool/*") },
  ]

  sandbox_roles = {
    "view-only" = {
      description = "AWS ViewOnlyAccess: list and describe resources, no data reads."
      managed     = ["arn:aws:iam::aws:policy/job-function/ViewOnlyAccess"]
      statements  = local.role_guards
      default     = true
    }
    "read-only" = {
      description = "AWS ReadOnlyAccess: read resources and their data (objects, items, logs)."
      managed     = ["arn:aws:iam::aws:policy/ReadOnlyAccess"]
      statements  = local.role_guards
      default     = false
    }
    "data-reader" = {
      description = "Read the demo S3 bucket and the my2cents-demo-* DynamoDB tables; nothing else."
      managed     = []
      statements  = concat(local.role_guards, local.data_reader)
      default     = false
    }
    "builder" = {
      description = "AWS PowerUserAccess in ${var.region}, minus changes to the sandboxes' own stack; can create billable resources."
      managed     = ["arn:aws:iam::aws:policy/PowerUserAccess"]
      statements  = concat(local.role_guards, local.builder_guards)
      default     = false
    }
  }
  sandbox_role_attachments = { for pair in flatten([for r, v in local.sandbox_roles : [for m in v.managed : { key = "${r}|${m}", role = r, arn = m }]]) : pair.key => pair }
}

# Each trusts only the iron-proxy task role (proxy.tf), which the control sidecar runs as
data "aws_iam_policy_document" "sandbox_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:SetSourceIdentity"]
    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.task.arn]
    }
  }
}

resource "aws_iam_role" "sandbox" {
  for_each           = { for k, v in local.sandbox_roles : k => v if var.control_plane }
  name               = "${local.name}-${each.key}"
  description        = each.value.description
  assume_role_policy = data.aws_iam_policy_document.sandbox_trust.json
  # Role chaining (task role -> this role) caps sessions at 1 hour whatever this says
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "sandbox" {
  for_each   = { for k, v in local.sandbox_role_attachments : k => v if var.control_plane }
  role       = aws_iam_role.sandbox[each.value.role].name
  policy_arn = each.value.arn
}

resource "aws_iam_role_policy" "sandbox" {
  for_each = { for k, v in local.sandbox_roles : k => v if var.control_plane }
  name     = "sandbox-guards"
  role     = aws_iam_role.sandbox[each.key].id
  policy   = jsonencode({ Version = "2012-10-17", Statement = each.value.statements })
}

# A tiny table for data-reader (and the roles' tests): three items, on demand
resource "aws_dynamodb_table" "demo" {
  count        = var.control_plane ? 1 : 0
  name         = "my2cents-demo-items"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"
  attribute {
    name = "id"
    type = "S"
  }
}

resource "aws_dynamodb_table_item" "demo" {
  for_each   = { for k, v in { "item-1" = "first demo item", "item-2" = "second demo item", "item-3" = "third demo item" } : k => v if var.control_plane }
  table_name = aws_dynamodb_table.demo[0].name
  hash_key   = "id"
  item       = jsonencode({ id = { S = each.key }, note = { S = each.value } })
}
