variable "region" {
  description = "AWS Region. The pod templates in ../ assume us-east-1 (AWS_REGION, the KMS call)."
  type        = string
  default     = "us-east-1"
}
