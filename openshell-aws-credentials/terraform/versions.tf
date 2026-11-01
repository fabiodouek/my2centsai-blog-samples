terraform {
  required_version = ">= 1.9"

  # Exact versions: the repository ignores .terraform.lock.hcl, so these pins are what
  # makes a later `terraform init` resolve the same providers. Same AWS provider as part 2.
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.71.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.7.2"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = { project = "openshell-eks", part = "5" }
  }
}
