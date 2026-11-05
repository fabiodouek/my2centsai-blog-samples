# What the lifecycle tests need besides the MicroVMs themselves: an S3 bucket for the
# build zips, a build role, and two execution roles (logs only, and logs plus one harmless
# read so 09_egress_identity.py can show what code in the VM can do with the role).
# No VPC: part 1 uses the managed INTERNET_EGRESS connector.

terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.68" }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "my2cents-lambda-microvm-lifecycle" }
  }
}

locals { name = "my2cents-lc" }

resource "aws_s3_bucket" "artifacts" {
  bucket_prefix = "${local.name}-artifacts-"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

locals {
  logs_statement = {
    Effect   = "Allow"
    Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    Resource = "arn:aws:logs:${var.region}:*:log-group:/aws/lambda-microvms/*"
  }
}

# Build role: read the zip, write build logs
resource "aws_iam_role" "build" {
  name               = "${local.name}-build"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy" "build" {
  role = aws_iam_role.build.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.artifacts.arn}/*" },
      local.logs_statement,
    ]
  })
}

# Execution role used by most tests: runtime logs only
resource "aws_iam_role" "exec_logs" {
  name               = "${local.name}-exec-logs"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy" "exec_logs" {
  role   = aws_iam_role.exec_logs.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [local.logs_statement] })
}

# Execution role for 09_egress_identity.py: logs plus s3:ListAllMyBuckets, to show the
# role is usable by any code in the VM
resource "aws_iam_role" "exec_probe" {
  name               = "${local.name}-exec-probe"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy" "exec_probe" {
  role = aws_iam_role.exec_probe.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      local.logs_statement,
      { Effect = "Allow", Action = ["s3:ListAllMyBuckets"], Resource = "*" },
    ]
  })
}

output "region" { value = var.region }
output "bucket" { value = aws_s3_bucket.artifacts.bucket }
output "build_role_arn" { value = aws_iam_role.build.arn }
output "exec_logs_role_arn" { value = aws_iam_role.exec_logs.arn }
output "exec_probe_role_arn" { value = aws_iam_role.exec_probe.arn }
