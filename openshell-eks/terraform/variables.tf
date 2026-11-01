variable "region" {
  description = "AWS Region for everything. 01_cluster.sh passes its own REGION, so change it there."
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "EKS cluster name, also the prefix of the IAM roles and the VPC name."
  type        = string
  default     = "osh-eks"
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version. Bump it together with ami_release_version and the add-on versions in main.tf."
  type        = string
  default     = "1.37"
}

# The node AMI release, pinned. Left unpinned, the EKS module looks up the newest AMI
# on every plan, and the first apply after AWS publishes one replaces both nodes.
# Find the current one with:
#   aws ssm get-parameter --name /aws/service/eks/optimized-ami/1.37/amazon-linux-2023/x86_64/standard/recommended/release_version --query Parameter.Value --output text
variable "ami_release_version" {
  description = "EKS-optimized AL2023 AMI release for the node group (<kubernetes version>-<date>)."
  type        = string
  default     = "1.37.1-20261031"
}

# 8 vCPU and 32 GiB. M7i supports nested virtualization, which puts /dev/kvm on the
# node, so a VM-based RuntimeClass (Kata) can run without bare metal.
variable "instance_type" {
  description = "Node instance type. It must support nested virtualization: M7i (used here) and the 8th-generation Intel types."
  type        = string
  default     = "m7i.2xlarge"
}

# Left null, the two zones are looked up: the first two regular zones that offer the
# instance type. Set two zones here to pin them.
variable "azs" {
  description = "Two availability zones for the subnets, or null to pick the first two that offer instance_type."
  type        = list(string)
  default     = null

  validation {
    condition     = try(length(var.azs) == 2 && length(distinct(var.azs)) == 2, var.azs == null)
    error_message = "azs must list two different availability zones (or be null)."
  }
}

# No default on purpose: a plain `terraform apply` must not change who can reach the
# API. 01_cluster.sh passes your IP. Destroy doesn't use the value but still needs one.
variable "public_access_cidr" {
  description = "The only address that can reach the EKS API endpoint: your public IP as a /32. 01_cluster.sh sets it from checkip.amazonaws.com (or MY_IP)."
  type        = string

  validation {
    condition     = can(cidrhost(var.public_access_cidr, 0)) && endswith(var.public_access_cidr, "/32")
    error_message = "public_access_cidr must be a single address as a /32, such as 203.0.113.7/32."
  }
}

# Repository prefix for every image in this series: the mirrored OpenShell and
# Agent Sandbox images and the workload image all live under openshell-eks/.
variable "ecr_prefix" {
  description = "ECR repository prefix for every image in this series."
  type        = string
  default     = "openshell-eks"
}

# Amazon CloudWatch Observability add-on (Container Insights) for the stress test in
# part 6. It bills per metric observation, so it stays off until then.
variable "container_insights" {
  description = "Install Container Insights (off until part 6)."
  type        = bool
  default     = false
}
