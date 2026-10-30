data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_caller_identity" "current" {}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 6.0"

  name = var.name
  cidr = "10.40.0.0/16"
  azs  = local.azs

  private_subnets = ["10.40.0.0/19", "10.40.32.0/19"]
  public_subnets  = ["10.40.128.0/24", "10.40.129.0/24"]

  # One NAT gateway is enough for a test cluster and saves ~$33/month per AZ.
  enable_nat_gateway = true
  single_nat_gateway = true

  public_subnet_tags  = { "kubernetes.io/role/elb" = 1 }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = 1 }
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 21.0"

  name               = var.name
  kubernetes_version = var.kubernetes_version

  # Public endpoint so the scripts can run from a laptop. Lock it down to your
  # own IP with endpoint_public_access_cidrs for anything longer-lived.
  endpoint_public_access                   = true
  enable_cluster_creator_admin_permissions = true

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  addons = merge({
    vpc-cni = {
      before_compute = true
      # Substrate ships NetworkPolicies for ate-system; the VPC CNI ignores
      # them unless policy enforcement is on.
      configuration_values = jsonencode({ enableNetworkPolicy = "true" })
    }
    eks-pod-identity-agent = { before_compute = true }
    kube-proxy             = {}
    coredns                = {}
    aws-ebs-csi-driver = {
      # AX's PostgreSQL needs a default StorageClass for its 10 GiB volume.
      configuration_values = jsonencode({ defaultStorageClass = { enabled = true } })
      pod_identity_association = [{
        role_arn        = aws_iam_role.ebs_csi.arn
        service_account = "ebs-csi-controller-sa"
      }]
    }
    }, var.container_insights ? {
    amazon-cloudwatch-observability = {
      # Metrics only: no Fluent Bit container logs, no Application Signals.
      configuration_values = jsonencode({
        containerLogs = { enabled = false }
        manager       = { applicationSignals = { autoMonitor = { monitorAllServices = false } } }
      })
      pod_identity_association = [{
        role_arn        = aws_iam_role.cloudwatch[0].arn
        service_account = "cloudwatch-agent"
      }]
      preserve = false # removing it from this map must remove the agent too
    }
  } : {})

  # The module's node security group only lets nodes reach each other on
  # 1025-65535 and DNS. With the VPC CNI, pods use the node's security group,
  # and Substrate's egress gateway listens on 443: without this rule a
  # sandbox on one node can't reach the gateway on the other, and its
  # outbound connections time out.
  node_security_group_additional_rules = {
    ingress_self_all = {
      description = "Node to node, all ports (pod-to-pod across nodes)"
      protocol    = "-1"
      from_port   = 0
      to_port     = 0
      type        = "ingress"
      self        = true
    }
  }

  eks_managed_node_groups = {
    workers = {
      # AL2023 on EKS 1.37 ships a 6.18 kernel, which has the OverlayFS
      # lowerdir+ mount option Substrate's image cache needs (6.8+).
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = [var.instance_type]

      min_size     = var.node_count
      max_size     = max(var.node_count, 1) # EKS needs max >= 1; set node_count = 0 to park the cluster
      desired_size = var.node_count

      disk_size = 100
    }
  }
}

# --- EBS CSI driver identity ------------------------------------------------

data "aws_iam_policy_document" "pod_identity_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ebs_csi" {
  name               = "${var.name}-ebs-csi"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.ebs_csi.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

# --- Snapshot storage --------------------------------------------------------
#
# Substrate stores actor snapshots under this bucket (for AX tasks: the
# /workspace volume; AX does not request memory snapshots). On S3
# its objectstore-plugin sidecars use the pod's own AWS credentials, so the two
# pods that run that sidecar, ate-api-server and atelet, get a role through EKS
# Pod Identity. (On GCS each transfer gets a token scoped to one actor; S3 has
# no equivalent in Substrate yet, so these pods can read every snapshot.)

resource "aws_s3_bucket" "snapshots" {
  bucket        = "${var.name}-snapshots-${data.aws_caller_identity.current.account_id}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "snapshots" {
  bucket                  = aws_s3_bucket.snapshots.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "snapshots" {
  statement {
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [aws_s3_bucket.snapshots.arn]
  }
  statement {
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.snapshots.arn}/*"]
  }
}

resource "aws_iam_role" "snapshots" {
  name               = "${var.name}-substrate-snapshots"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

resource "aws_iam_role_policy" "snapshots" {
  role   = aws_iam_role.snapshots.id
  policy = data.aws_iam_policy_document.snapshots.json
}

# atelet, not the kubelet, pulls sandbox images. It runs the node's ECR
# credential provider as a child process, which picks up the pod's Pod
# Identity credentials, so the same role needs to read ECR.
resource "aws_iam_role_policy_attachment" "snapshots_ecr" {
  role       = aws_iam_role.snapshots.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
}

resource "aws_eks_pod_identity_association" "snapshots" {
  for_each = toset(["ate-api-server", "atelet"])

  cluster_name    = module.eks.cluster_name
  namespace       = "ate-system"
  service_account = each.key
  role_arn        = aws_iam_role.snapshots.arn
}

# --- Container images ---------------------------------------------------------
#
# Neither Substrate nor AX publishes images, so both are built from source and
# pushed here. ko pushes one repository per binary; create-on-push saves
# listing them all up front.

resource "aws_ecr_repository_creation_template" "images" {
  prefix               = var.name
  applied_for          = ["CREATE_ON_PUSH"]
  image_tag_mutability = "MUTABLE"
  description          = "Substrate and AX images built from source"
}

# --- Container Insights (optional) ------------------------------------------

resource "aws_iam_role" "cloudwatch" {
  count              = var.container_insights ? 1 : 0
  name               = "${var.name}-cloudwatch-agent"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust.json
}

resource "aws_iam_role_policy_attachment" "cloudwatch" {
  count      = var.container_insights ? 1 : 0
  role       = aws_iam_role.cloudwatch[0].name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}
