variable "region" {
  description = "AWS Region. The task definition templates in ../ assume us-east-1 (awslogs-region, the KMS call in attest.sh)."
  type        = string
  default     = "us-east-1"
}
