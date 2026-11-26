# What the durable function and its MicroVMs need, besides the function itself
# (01_deploy.py creates the function, its version and its alias):
#
# - an S3 bucket for the image zip and the replay test's marker objects
# - the image build role
# - the MicroVM execution role, which completes the durable callbacks
# - two function roles: the full one, and a "leaky" one without lambda:TerminateMicrovm
#   that 09_leak.py uses to show what a missing permission costs
# - an SQS queue (with a dead-letter queue) that 05_sqs.py connects to the function

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
    tags = { project = "my2cents-lambda-durable-microvms" }
  }
}

data "aws_caller_identity" "me" {}

locals {
  name     = "my2cents-ldm"
  account  = data.aws_caller_identity.me.account_id
  image    = "arn:aws:lambda:${var.region}:${local.account}:microvm-image:${local.name}-*"
  function = "arn:aws:lambda:${var.region}:${local.account}:function:${local.name}-*"
}

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
  microvm_logs = {
    Effect   = "Allow"
    Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    Resource = "arn:aws:logs:${var.region}:${local.account}:log-group:/aws/lambda-microvms/*"
  }
}

# ---------- image build role: read the zip, write build logs ----------

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
      local.microvm_logs,
    ]
  })
}

# ---------- MicroVM execution role: the worker completes the callbacks with it ----------

resource "aws_iam_role" "microvm" {
  name               = "${local.name}-microvm"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy" "microvm" {
  role = aws_iam_role.microvm.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "lambda:SendDurableExecutionCallbackSuccess",
          "lambda:SendDurableExecutionCallbackFailure",
          "lambda:SendDurableExecutionCallbackHeartbeat",
        ]
        # The callback APIs authorize on the function's qualified ARN
        Resource = "${local.function}:*"
      },
      local.microvm_logs,
    ]
  })
}

# ---------- function roles ----------

locals {
  microvm_actions = [
    "lambda:RunMicrovm",
    "lambda:TerminateMicrovm",
    "lambda:CreateMicrovmAuthToken",
    "lambda:GetMicrovm",
    "lambda:SuspendMicrovm", # only for suspend_after in 06_session.py
  ]
  function_roles = {
    full = local.microvm_actions
    # Everything except TerminateMicrovm, for 09_leak.py
    leaky = [for a in local.microvm_actions : a if a != "lambda:TerminateMicrovm"]
  }
}

resource "aws_iam_role" "function" {
  for_each           = local.function_roles
  name               = "${local.name}-function-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

# Checkpointing (CheckpointDurableExecution, GetDurableExecutionState) and logs
resource "aws_iam_role_policy_attachment" "durable" {
  for_each   = local.function_roles
  role       = aws_iam_role.function[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicDurableExecutionRolePolicy"
}

# Receive and delete messages for the SQS event source mapping
resource "aws_iam_role_policy_attachment" "sqs" {
  for_each   = local.function_roles
  role       = aws_iam_role.function[each.key].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaSQSQueueExecutionRole"
}

resource "aws_iam_role_policy" "function" {
  for_each = local.function_roles
  role     = aws_iam_role.function[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = each.value, Resource = local.image },
      {
        Effect   = "Allow"
        Action   = ["lambda:PassNetworkConnector"]
        Resource = "arn:aws:lambda:${var.region}:aws:network-connector:aws-network-connector:*"
      },
      { Effect = "Allow", Action = ["iam:PassRole"], Resource = aws_iam_role.microvm.arn },
      # 08_replay.py: the handler writes a marker object so it crashes only once
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.artifacts.arn}/replay/*"
      },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.artifacts.arn },
    ]
  })
}

# ---------- SQS ----------

resource "aws_sqs_queue" "dlq" {
  name = "${local.name}-jobs-dlq"
}

resource "aws_sqs_queue" "jobs" {
  name = "${local.name}-jobs"
  # Longer than a synchronous durable invocation can last, so a message is not
  # delivered again while its execution still runs
  visibility_timeout_seconds = 1800
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = 1 # no second MicroVM for a message that failed once
  })
}

output "region" { value = var.region }
output "bucket" { value = aws_s3_bucket.artifacts.bucket }
output "build_role_arn" { value = aws_iam_role.build.arn }
output "microvm_role_arn" { value = aws_iam_role.microvm.arn }
output "function_role_arn" { value = aws_iam_role.function["full"].arn }
output "leaky_role_arn" { value = aws_iam_role.function["leaky"].arn }
output "queue_arn" { value = aws_sqs_queue.jobs.arn }
output "queue_url" { value = aws_sqs_queue.jobs.url }
output "dlq_url" { value = aws_sqs_queue.dlq.url }
