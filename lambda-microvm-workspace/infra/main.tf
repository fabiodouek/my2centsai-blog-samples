variable "region" {
  type    = string
  default = "us-east-1"
}

# Part 2 (two VPCs, iron-proxy, the sandbox connector and, with control_plane=true, the proxy control plane: the
# config bucket, the sidecar and the four sandbox roles). Relative to infra/, where terraform runs.
variable "part2_state_path" {
  type    = string
  default = "../../../lambda-microvm-iron-proxy/lambda-microvm-iron-proxy/infra/terraform.tfstate"
}

# 0 until a backend image exists in ECR; scripts/deploy.sh applies with 1.
# A plain `terraform apply` scales the service back to 0, so deploy through the script.
variable "desired_count" {
  type    = number
  default = 0
}

# deploy.sh pushes :latest and a timestamp tag, and applies with the timestamp so every deploy
# registers a new task definition revision (which is what rolls the service).
variable "image_tag" {
  type    = string
  default = "latest"
}

# The sandbox image the backend runs (built by scripts/build_sandbox_image.py)
variable "sandbox_image_name" {
  type    = string
  default = "my2cents-ws-sandbox"
}

# Extra OAuth callback/logout URLs, e.g. the Vite dev server for local frontend work
variable "extra_callback_urls" {
  type    = list(string)
  default = ["http://localhost:5173/"]
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "my2cents-lambda-microvm-workspace" }
  }
}

data "terraform_remote_state" "part2" {
  backend = "local"
  config  = { path = var.part2_state_path }
}

data "aws_caller_identity" "me" {}

locals {
  name    = "my2cents-ws"
  p2      = data.terraform_remote_state.part2.outputs
  account = data.aws_caller_identity.me.account_id

  image_arn = "arn:aws:lambda:${var.region}:${local.account}:microvm-image:${var.sandbox_image_name}"
  # One image per sandbox size (backend/app/sizes.py, scripts/build_sandbox_image.py): medium is the original image
  image_arns = { for size, suffix in { small = "-1g", medium = "", large = "-4g", xlarge = "-8g" } :
  size => "${local.image_arn}${suffix}" }

  # Part 2's proxy control plane (control_plane=true there); null when it's off
  control_bucket = try(local.p2.control_bucket, null)
  control_on     = local.control_bucket != null
  control_keys   = try(local.p2.control_keys, null)
  sandbox_roles  = try(local.p2.sandbox_roles, null)
  scope_baseline = try(local.p2.scope_baseline, null)
  user_prefix    = coalesce(try(local.p2.user_secret_prefix, null), "my2cents-ip/user/")
  # How often part 2's control sidecar checks the bucket (older part 2 states have no output: its old 2 s)
  control_poll_s = coalesce(try(local.p2.control_poll_s, null), 2)

  # Log group ARNs without the trailing ":*" (StartLiveTail wants this form)
  proxy_log_group_arn = "arn:aws:logs:${var.region}:${local.account}:log-group:${local.p2.proxy_log_group}"
  # Part 2's Resolver query log for its sandbox VPC: only the connector's ENIs live there, so every line is a sandbox's
  dns_log_group_arn = "arn:aws:logs:${var.region}:${local.account}:log-group:${local.p2.query_log_group}"
}

resource "random_id" "suffix" {
  byte_length = 4
}

# Shared secret between CloudFront and the ALB/backend. Alphanumeric only: ALB header
# conditions treat * and ? as wildcards.
resource "random_password" "origin_secret" {
  length  = 48
  special = false
}

# ---------- Data store and registry ----------

resource "aws_dynamodb_table" "sandboxes" {
  name         = "${local.name}-sandboxes"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "sub"
  range_key    = "id"
  attribute {
    name = "sub"
    type = "S"
  }
  attribute {
    name = "id"
    type = "S"
  }
  # Optional: the backend may set "ttl" (epoch seconds) on terminated items to expire them
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# The proxy policy change log: who changed what, when, and the config/scope version (never a secret value)
resource "aws_dynamodb_table" "changes" {
  name         = "${local.name}-policy-changes"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

resource "aws_ecr_repository" "backend" {
  name                 = "${local.name}-backend"
  image_tag_mutability = "MUTABLE" # :latest is re-pushed on every deploy
  force_delete         = true
}

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}
