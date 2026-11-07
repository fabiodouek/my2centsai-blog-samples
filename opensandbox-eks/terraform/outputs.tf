# 01_cluster.sh writes these into ../env.sh, which every later script sources.

output "cluster_name" {
  description = "EKS cluster name (also the kubectl context name 01_cluster.sh creates)."
  value       = module.eks.cluster_name
}

output "region" {
  description = "AWS Region of the cluster and the ECR registry."
  value       = var.region
}

output "ecr_registry" {
  description = "Private ECR registry host for this account and Region."
  value       = "${local.account_id}.dkr.ecr.${var.region}.amazonaws.com"
}

output "ecr_prefix" {
  description = "Repository prefix; repositories under it are created on first push."
  value       = var.ecr_prefix
}

output "nodegroup_name" {
  description = "Managed node group name, for scaling it with the AWS CLI."
  value       = local.nodegroup_name
}

output "vpc_id" {
  description = "VPC of the cluster (the AWS Load Balancer Controller needs it)."
  value       = module.vpc.vpc_id
}

output "lbc_role_arn" {
  description = "IAM role the AWS Load Balancer Controller gets through EKS Pod Identity."
  value       = aws_iam_role.pod_identity["aws-load-balancer-controller"].arn
}
