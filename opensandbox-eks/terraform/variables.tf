variable "region" {
  description = "AWS Region for everything. 01_cluster.sh passes its own REGION, so change it there."
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "EKS cluster name, also the prefix of the IAM roles and the VPC name."
  type        = string
  default     = "osb-eks"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version. Bump it together with ami_release_version and the five add-on versions in main.tf."
  type        = string
  default     = "1.37"
}

# The node AMI release, pinned. Left unpinned, the EKS module looks up the newest AMI
# on every plan, and the first apply after AWS publishes one replaces both nodes.
# Find the current one with:
#   aws ssm get-parameter --name /aws/service/eks/optimized-ami/1.37/amazon-linux-2023/x86_64/standard/recommended/release_version --query Parameter.Value --output text
# It must match kubernetes_version. AWS deprecates each AMI two years after publishing it
# (this one on 2028-10-31), so a much later run needs a newer release here.
variable "ami_release_version" {
  description = "EKS-optimized AL2023 AMI release for the node group (<kubernetes version>-<date>)."
  type        = string
  default     = "1.37.1-20261031"
}

# 8 vCPU and 32 GiB. M7i supports nested virtualization, which puts /dev/kvm on the
# node, so Kata Containers can run VMs without bare metal.
variable "instance_type" {
  description = "Node instance type. It must support nested virtualization: M7i (used here) and the 8th-generation Intel types."
  type        = string
  default     = "m7i.2xlarge"
}

# Left null, the two zones are looked up: the first two regular zones that offer the
# instance type. If AWS ever changes that list, the lookup changes too, and the subnets
# (and with them the cluster) would be replaced. Set two zones here to rule that out.
variable "azs" {
  description = "Two availability zones for the subnets, or null to pick the first two that offer instance_type."
  type        = list(string)
  default     = null

  # try(): with azs null, length() errors and the fallback (azs == null) holds.
  validation {
    condition     = try(length(var.azs) == 2 && length(distinct(var.azs)) == 2, var.azs == null)
    error_message = "azs must list two different availability zones (or be null)."
  }
}

# No default on purpose: a plain `terraform apply` must not change who can reach the
# API. 01_cluster.sh passes your IP. Destroy doesn't use the value but still needs one,
# so a teardown passes the same -var.
variable "public_access_cidr" {
  description = "The only address that can reach the EKS API endpoint: your public IP as a /32. 01_cluster.sh sets it from checkip.amazonaws.com (or MY_IP)."
  type        = string

  validation {
    condition     = can(cidrhost(var.public_access_cidr, 0)) && endswith(var.public_access_cidr, "/32")
    error_message = "public_access_cidr must be a single address as a /32, such as 203.0.113.7/32."
  }
}

# Repository prefix for every image in this series: the mirrored OpenSandbox images,
# the workload image and sandbox snapshots all live under opensandbox-eks/.
variable "ecr_prefix" {
  description = "ECR repository prefix for every image in this series."
  type        = string
  default     = "opensandbox-eks"
}

# Scan on push can only be set for the whole registry, not per creation template. With
# this on, Terraform owns the registry's scanning configuration (01_cluster.sh checks
# first that it is the default). Set it to false and the apply removes the rule if this
# Terraform already manages it, and otherwise leaves the registry alone. Images are then
# only scanned when you ask.
variable "manage_ecr_scan_on_push" {
  description = "Manage the registry-wide ECR scanning configuration (basic scanning, scan on push for ecr_prefix/*)."
  type        = bool
  default     = true
}

# Amazon CloudWatch Observability add-on (Container Insights) for the stress test in
# part 6. It bills per metric observation, so it stays off until then; the part 2 run
# never turns it on.
variable "container_insights" {
  description = "Install Container Insights (off until part 6)."
  type        = bool
  default     = false
}
