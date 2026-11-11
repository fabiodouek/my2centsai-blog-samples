# Dependencies only: VPC with a NAT gateway, a task security group, IAM roles and a log group.
# The ECS cluster, the capacity provider (where Nitro Enclaves is turned on) and the task
# definitions are created in the console or with the AWS CLI, as the README shows.
#
# The role names are fixed because the JSON templates in ../ reference them.

locals {
  name = "my2cents-nitro-ecs"
}

data "aws_availability_zones" "available" {
  state = "available"
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name               = local.name
  cidr               = "10.62.0.0/16"
  azs                = slice(data.aws_availability_zones.available.names, 0, 2)
  private_subnets    = ["10.62.10.0/24", "10.62.11.0/24"]
  public_subnets     = ["10.62.0.0/24", "10.62.1.0/24"]
  enable_nat_gateway = true
  single_nat_gateway = true
}

resource "aws_security_group" "tasks" {
  name        = "${local.name}-tasks"
  description = "Nitro Sandbox demo tasks: egress only"
  vpc_id      = module.vpc.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ECS Managed Instances: infrastructure role (ECS launches and manages the instances).
resource "aws_iam_role" "infrastructure" {
  name = "ecsInfrastructureRole-my2cents"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "infrastructure" {
  role       = aws_iam_role.infrastructure.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonECSInfrastructureRolePolicyForManagedInstances"
}

# Instance role: the name must start with ecsInstanceRole, because the infrastructure
# policy scopes iam:PassRole to that prefix.
resource "aws_iam_role" "instance" {
  name = "ecsInstanceRole-my2cents"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "instance" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonECSInstanceRolePolicyForManagedInstances"
}

resource "aws_iam_instance_profile" "instance" {
  name = aws_iam_role.instance.name
  role = aws_iam_role.instance.name
}

# Task execution role (image pulls, awslogs).
resource "aws_iam_role" "execution" {
  name = "my2cents-nitro-ecs-execution"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task role: what the container's own AWS calls run as.
#
# There is deliberately no KMS permission here. The confidential task's kms:Decrypt is
# granted by the KMS key policy only (../kms-ecs-final.json), with conditions on the
# attestation document (PCR17 and PCR8:ServiceName). An IAM policy granting kms:Decrypt
# would let any task with this role decrypt without attesting.
resource "aws_iam_role" "task" {
  name = "my2cents-nitro-ecs-task"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

# ssmmessages for ECS Exec (used on the isolated and shared probe tasks only).
resource "aws_iam_role_policy" "task_exec" {
  name = "ecs-exec"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ssmmessages:CreateControlChannel", "ssmmessages:CreateDataChannel", "ssmmessages:OpenControlChannel", "ssmmessages:OpenDataChannel"]
      Resource = "*"
    }]
  })
}

resource "aws_cloudwatch_log_group" "tasks" {
  name              = "/ecs/my2cents-nitro-sandbox"
  retention_in_days = 1
}
