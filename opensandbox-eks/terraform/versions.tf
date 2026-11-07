terraform {
  required_version = ">= 1.9"

  # Exact versions: the repository ignores .terraform.lock.hcl, so these pins are what
  # makes a later `terraform init` resolve the same providers.
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # cpu_options.nested_virtualization exists on aws_launch_template since 6.33.0. The
      # provider only checks the value (enabled or disabled); EC2 decides which instance
      # types accept it. Its docs still say 8th-generation Intel only, but M7i works.
      version = "6.71.0"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "4.4.2"
    }
    time = {
      source  = "hashicorp/time"
      version = "0.14.2"
    }
    # Used inside the EKS module (node user data, ordering helpers).
    cloudinit = {
      source  = "hashicorp/cloudinit"
      version = "2.4.1"
    }
    null = {
      source  = "hashicorp/null"
      version = "3.3.2"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = local.tags
  }
}
