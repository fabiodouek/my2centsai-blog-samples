terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.68" }
    tls = { source = "hashicorp/tls", version = "~> 4.0" }
  }
}
