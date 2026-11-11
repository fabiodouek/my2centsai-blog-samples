# Dependencies only: a VPC with a NAT gateway and an EKS Auto Mode cluster on Kubernetes 1.37.
# The Graviton NodePool, the pods, the Pod Identity role and the KMS key are created with
# kubectl and the AWS CLI, as the README shows.

locals {
  name = "my2cents-nitro-eks"
}

data "aws_availability_zones" "available" {
  state = "available"
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name               = local.name
  cidr               = "10.63.0.0/16"
  azs                = slice(data.aws_availability_zones.available.names, 0, 2)
  private_subnets    = ["10.63.10.0/24", "10.63.11.0/24"]
  public_subnets     = ["10.63.0.0/24", "10.63.1.0/24"]
  enable_nat_gateway = true
  single_nat_gateway = true

  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
}

# EKS Auto Mode, Kubernetes 1.37. Nitro Sandboxes need 1.36 or later; there is no
# sandbox-specific argument in the module, the provider or the EKS API.
# Only the "system" built-in pool is enabled: it keeps the default NodeClass and does not
# launch a node on its own. The general-purpose pool is amd64-only, so the Graviton
# (m8g.xlarge) NodePool is created with kubectl (../nodepool.yaml).
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = local.name
  kubernetes_version = "1.37"

  # Public endpoint for the evaluation. Restrict it with endpoint_public_access_cidrs
  # for anything that lives longer than a test.
  endpoint_public_access                   = true
  enable_cluster_creator_admin_permissions = true

  compute_config = {
    enabled    = true
    node_pools = ["system"]
  }

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets
}
