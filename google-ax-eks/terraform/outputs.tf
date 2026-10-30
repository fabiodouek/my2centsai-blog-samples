output "cluster_name" {
  value = module.eks.cluster_name
}

output "region" {
  value = var.region
}

output "image_repo" {
  description = "ECR prefix that Substrate and AX images are pushed under"
  value       = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.region}.amazonaws.com/${var.name}"
}

output "snapshot_location" {
  value = "s3://${aws_s3_bucket.snapshots.bucket}/"
}
