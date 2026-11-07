terraform {
  required_version = ">= 1.9"

  # The same provider pin as ../opensandbox-eks/terraform. The repository ignores
  # .terraform.lock.hcl, so this pin is what makes a later `terraform init` resolve the
  # same provider.
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.71.0"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}
