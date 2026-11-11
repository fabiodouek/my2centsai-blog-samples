terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Planned with 6.72.0, terraform-aws-modules/vpc 6.7.3 and terraform-aws-modules/eks
      # 21.29.0. The repository ignores .terraform.lock.hcl, so this constraint is what a
      # fresh `terraform init` resolves. The EKS module declares its own tls, time,
      # cloudinit and null providers.
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
