# 01_iam.sh writes these into ../broker.env (gitignored). The broker Deployment reads that
# file as a ConfigMap, and the test scripts read it with a parser.

output "sandbox_role_arn" {
  description = "SANDBOX_ROLE_ARN: the role the broker assumes for each sandbox."
  value       = aws_iam_role.sandbox.arn
}

output "broker_role_arn" {
  description = "BROKER_ROLE_ARN: the broker's EKS Pod Identity role."
  value       = aws_iam_role.broker.arn
}

output "ddb_table" {
  description = "DDB_TABLE: the test table."
  value       = aws_dynamodb_table.test.name
}

output "broker_ns" {
  description = "BROKER_NS: namespace of the broker Deployment."
  value       = local.broker_namespace
}

output "broker_sa" {
  description = "BROKER_SA: service account of the broker (the Pod Identity association's)."
  value       = local.broker_service_account
}

output "region" {
  description = "AWS_REGION: Region of the roles' resources."
  value       = var.region
}

output "test_bucket" {
  description = "AWS_TEST_BUCKET: the bucket the sandbox role may use."
  value       = var.test_bucket
}

output "bedrock_model_id" {
  description = "BEDROCK_MODEL_ID: the one model the sandbox role may invoke (the pinned variable, not .env)."
  value       = var.bedrock_model_id
}
