variable "region" {
  description = "AWS Region of the cluster. 01_iam.sh passes AWS_REGION from ../opensandbox-eks/env.sh."
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "The EKS cluster from ../opensandbox-eks. 01_iam.sh passes CLUSTER_NAME from env.sh."
  type        = string
  default     = "osb-eks"
}

# No default: the bucket is yours, and it must already exist. 01_iam.sh reads
# AWS_TEST_BUCKET from the secrets file (OSB_SECRETS_ENV, default ~/.config/opensandbox-eks/secrets.env).
# Terraform does not create or delete it; the sandbox role may only list it and read,
# write and delete its objects.
variable "test_bucket" {
  description = "An existing S3 bucket in var.region that sandboxes may use."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.test_bucket))
    error_message = "test_bucket must be an S3 bucket name (3-63 characters: lowercase letters, digits, dots, hyphens)."
  }
}

# Pinned on purpose, not read from .env: this cross-Region inference profile invokes in
# the series' account. The role may call it and the foundation model it routes to, and
# nothing else on Bedrock.
variable "bedrock_model_id" {
  description = "The one Bedrock model sandboxes may call: a US cross-Region inference profile ID."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

  validation {
    condition     = startswith(var.bedrock_model_id, "us.")
    error_message = "bedrock_model_id must be a US cross-Region inference profile (us.<model id>)."
  }
}

variable "ddb_table" {
  description = "Name of the DynamoDB table this stack creates for the tests."
  type        = string
  default     = "osb-cred-test"
}
