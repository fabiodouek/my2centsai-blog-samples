# Two VPCs, joined by peering. The sandboxes get no proxy settings: every name they look up resolves to
# iron-proxy, and the network can't reach anything else.
#
#   proxy VPC   (10.50.0.0/16)  NAT gateway, iron-proxy on Fargate, internal NLB. Normal Route 53 DNS.
#   sandbox VPC (10.51.0.0/16)  the MicroVM egress connector only. No internet gateway, no NAT. Its routes
#                               send the two NLB /32s to the peering connection and nothing else. DNS Firewall
#                               answers every name with a CNAME to proxy.sandbox.internal (both NLB IPs).

variable "region" {
  type    = string
  default = "us-east-1"
}

# The two AZs, by ID so they're the same physical zones in any account. Change them with the region.
variable "az_ids" {
  type    = list(string)
  default = ["use1-az4", "use1-az6"]
}

# strict: GitHub limited to one repo with path rules, and the GitHub secret marked require: true (Test 10).
variable "strict" {
  type    = bool
  default = false
}

variable "github_repo" {
  type    = string
  default = "fabiodouek/my2centsai-sandbox-test"
}

# ENABLED: the DNS Firewall association can't be removed or changed until this is switched off, so nobody can
# quietly turn recursive DNS back on for the sandboxes (12_dns_firewall.py). scripts/destroy.sh switches it
# off before `terraform destroy`, which otherwise fails on the association.
variable "firewall_mutation_protection" {
  type    = string
  default = "ENABLED"
  validation {
    condition     = contains(["ENABLED", "DISABLED"], var.firewall_mutation_protection)
    error_message = "firewall_mutation_protection must be ENABLED or DISABLED."
  }
}

# Bump to force iron-proxy to re-read secrets immediately (redeploys the task).
variable "proxy_generation" {
  type    = number
  default = 1
}

# Part 3's proxy control plane: iron-proxy's config lives in an S3 bucket the workspace edits, a `control` sidecar
# in the iron-proxy task syncs it and reloads iron-proxy, and sandbox AWS calls are signed as one of four sandbox
# roles (roles.tf) instead of the task role. Off by default: part 2's post and scripts assume it's off.
variable "control_plane" {
  type    = bool
  default = false
}

# The sidecar's image: AWS's aws-cli image (bash, curl, jq), pinned by index digest
variable "control_image" {
  type    = string
  default = "amazon/aws-cli@sha256:ed3233c28ad56d6a66e9e7623a495ab2d7eb08d425f8eea0fdbdfacb12de2ba8" # 2.38.0
}

# control_plane=true: where part 3 keeps the credentials admins add. The sandbox roles may read these secrets
# (iron-proxy fetches them with the role's session) and no others.
variable "user_secret_prefix" {
  type    = string
  default = "my2cents-ip/user/"
}

variable "iron_proxy_image" {
  type = string
  # ironsh/iron-proxy:0.55.0, pinned by index digest (the images aren't signed)
  default = "ironsh/iron-proxy@sha256:a77e4aed1b3dd5e4d70198a89c6a489b6361041f9fa4ed3aa8df31e3c91dd286"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "my2cents-lambda-microvm-iron-proxy" }
  }
}

data "aws_caller_identity" "me" {}

locals {
  name    = "my2cents-ip"
  account = data.aws_caller_identity.me.account_id

  proxy_cidr   = "10.50.0.0/16"
  sandbox_cidr = "10.51.0.0/16"

  # Static private IPs for the internal NLB, one per AZ: 10.50.10.10 and 10.50.11.10.
  # Every name a sandbox resolves leads here.
  nlb_ips = [for i in range(2) : cidrhost(aws_subnet.proxy[i].cidr_block, 10)]

  # The ports a sandbox may open to the NLB: 80 and 443 for transparent interception, 8080 for explicit
  # mode (HTTPS_PROXY), kept only for the comparisons in 11_path_rules.py and 14_latency.py.
  sandbox_ports = [80, 443, 8080]
}
