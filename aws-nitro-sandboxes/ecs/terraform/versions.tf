terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Validated with 6.72.0 and terraform-aws-modules/vpc 6.7.3. The repository ignores
      # .terraform.lock.hcl, so this constraint is what a fresh `terraform init` resolves.
      version = "~> 6.72"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = { project = "nitro-sandboxes-blog" }
  }
}
