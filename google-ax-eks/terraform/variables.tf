variable "name" {
  description = "Name for the cluster and the prefix for every resource it owns"
  type        = string
  default     = "ax-eks"
}

variable "region" {
  type    = string
  default = "us-east-1"
}

# Substrate needs PodCertificateRequest and ClusterTrustBundle, which are GA
# and served by default only from Kubernetes 1.37. EKS does not let you turn
# on beta APIs, so 1.36 and older cannot run it.
variable "kubernetes_version" {
  type    = string
  default = "1.37"
}

# Substrate runs linux/amd64 only. AX's install guide asks for c3-standard-4
# or larger on GKE (4 vCPU, 16 GB); m7i.xlarge is the same shape on AWS.
variable "instance_type" {
  type    = string
  default = "m7i.xlarge"
}

variable "node_count" {
  type    = number
  default = 2
}

# Amazon CloudWatch Observability add-on (Container Insights) for the stress
# test. Bills per metric observation (a few dollars a day for this cluster);
# leave it off otherwise.
variable "container_insights" {
  type    = bool
  default = false
}
