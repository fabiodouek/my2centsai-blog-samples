variable "region" {
  description = "AWS Region of the cluster, the bucket, STS and Bedrock."
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "The EKS cluster from part 2 (../../openshell-eks)."
  type        = string
  default     = "osh-eks"
}

variable "gateway_namespace" {
  description = "Namespace of the OpenShell gateway StatefulSet."
  type        = string
  default     = "openshell"
}

# kubectl --context osh-eks -n openshell get sts openshell \
#   -o jsonpath='{.spec.template.spec.serviceAccountName}'
variable "gateway_service_account" {
  description = "ServiceAccount of the gateway StatefulSet (the chart names it after the release)."
  type        = string
  default     = "openshell"
}

# Bedrock model the sandbox may call. The US inference profile routes to these three
# Regions (aws bedrock get-inference-profile --inference-profile-identifier us.<model>).
variable "bedrock_model_id" {
  description = "Foundation model ID the sandbox role may invoke."
  type        = string
  default     = "amazon.nova-micro-v1:0"
}

variable "bedrock_profile_regions" {
  description = "Regions the us. inference profile of bedrock_model_id routes to."
  type        = list(string)
  default     = ["us-east-1", "us-east-2", "us-west-2"]
}
