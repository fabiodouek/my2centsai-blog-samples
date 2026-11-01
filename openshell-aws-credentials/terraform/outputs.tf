# 01_setup.sh writes these into ../env.sh, which the later scripts source.

output "bucket_name" {
  description = "Throwaway bucket for the S3 tests (7-day expiry, emptied by terraform destroy)."
  value       = aws_s3_bucket.creds.bucket
}

output "gateway_role_arn" {
  description = "Role the OpenShell gateway gets through EKS Pod Identity."
  value       = aws_iam_role.gateway.arn
}

output "sandbox_role_arn" {
  description = "Role the gateway assumes for sandboxes (refresh material role_arn)."
  value       = aws_iam_role.sandbox.arn
}

output "pod_identity_association_id" {
  description = "Pod Identity association for the gateway ServiceAccount."
  value       = aws_eks_pod_identity_association.gateway.association_id
}

output "region" {
  description = "Region of the bucket, STS and Bedrock calls."
  value       = var.region
}
