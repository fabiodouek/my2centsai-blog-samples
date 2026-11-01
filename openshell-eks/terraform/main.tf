data "aws_caller_identity" "current" {}

# Regular availability zones only (no Local Zones), restricted to those that offer the
# node instance type: m7i is missing from one us-east-1 zone, and zone names map to
# different physical zones in every account.
data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

data "aws_ec2_instance_type_offerings" "nodes" {
  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = [var.instance_type]
  }
}

locals {
  tags           = { project = "openshell-eks" }
  account_id     = data.aws_caller_identity.current.account_id
  nodegroup_name = "osh-nodes"
  azs = var.azs != null ? var.azs : slice(sort(tolist(setintersection(
    data.aws_availability_zones.available.names,
    data.aws_ec2_instance_type_offerings.nodes.locations,
  ))), 0, 2)
}

# --- Network ------------------------------------------------------------------

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.7.3"

  name = var.cluster_name
  cidr = "10.70.0.0/16"
  azs  = local.azs

  # Nodes and pods live in the private subnets (the VPC CNI gives every pod a VPC IP,
  # hence /19). The public subnets hold the NAT gateway and internet-facing load balancers.
  private_subnets = ["10.70.0.0/19", "10.70.32.0/19"]
  public_subnets  = ["10.70.128.0/24", "10.70.129.0/24"]

  # One NAT gateway for both zones: enough for a test cluster, and half the hourly cost.
  enable_nat_gateway = true
  single_nat_gateway = true

  # Subnet discovery for the AWS Load Balancer Controller.
  public_subnet_tags  = { "kubernetes.io/role/elb" = "1" }
  private_subnet_tags = { "kubernetes.io/role/internal-elb" = "1" }
}

# Image layers pulled from ECR are served from S3. A gateway endpoint keeps that traffic
# off the NAT gateway, which bills per GB processed. The endpoint itself is free.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = module.vpc.private_route_table_ids
}

# --- Cluster ------------------------------------------------------------------

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.29.0"

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version

  # The API endpoint is public so the scripts can run from a laptop, but only from one
  # address. Nodes reach the API through the private endpoint.
  endpoint_public_access       = true
  endpoint_public_access_cidrs = [var.public_access_cidr]
  endpoint_private_access      = true

  # Access entries only (no aws-auth ConfigMap), with cluster admin for whoever runs
  # Terraform.
  authentication_mode                      = "API"
  enable_cluster_creator_admin_permissions = true

  # EKS already envelope-encrypts Kubernetes API data with an AWS-owned key (1.28 and
  # later), so no customer-managed KMS key: it would outlive a teardown by 7 to 30 days.
  create_kms_key    = false
  encryption_config = null

  # No control plane logs: CloudWatch bills their ingestion. Add "audit" here to debug.
  enabled_log_types           = []
  create_cloudwatch_log_group = false

  # Workloads get AWS credentials from EKS Pod Identity, so no IRSA OIDC provider.
  enable_irsa = false

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  # Add-on versions are pinned for the same reason as the AMI: unpinned, the module
  # picks the newest build on every plan, and a re-run upgrades whatever AWS published
  # since the last one. Bump them together with kubernetes_version.
  #
  # The second map is Container Insights for part 6. It is empty while
  # var.container_insights is false, which is the case for the whole of part 2.
  addons = merge({
    vpc-cni = {
      addon_version  = "v1.23.2-eksbuild.8"
      before_compute = true
      # OpenShell's Kubernetes driver fences every sandbox workload with NetworkPolicies
      # (no egress except to its own supervisor). Kubernetes accepts NetworkPolicies even
      # when nothing enforces them; with the VPC CNI, enforcement is this setting, which
      # runs the network policy agent on every node.
      configuration_values = jsonencode({ enableNetworkPolicy = "true" })
    }
    eks-pod-identity-agent = {
      addon_version  = "v1.4.1-eksbuild.3"
      before_compute = true
    }
    kube-proxy = {
      addon_version  = "v1.37.0-eksbuild.32"
      before_compute = true
    }
    coredns = {
      addon_version = "v1.14.7-eksbuild.22"
    }
    aws-ebs-csi-driver = {
      addon_version = "v1.67.2-eksbuild.1"
      # A default gp3 StorageClass for the gateway's SQLite volume and every sandbox's
      # workspace volume, and the project tag
      # on every volume the driver creates.
      configuration_values = jsonencode({
        defaultStorageClass = { enabled = true }
        controller          = { extraVolumeTags = local.tags }
      })
      pod_identity_association = [{
        role_arn        = aws_iam_role.pod_identity["ebs-csi-controller"].arn
        service_account = "ebs-csi-controller-sa"
      }]
    }
    }, var.container_insights ? { # part 6 only
    amazon-cloudwatch-observability = {
      addon_version = "v7.0.0-eksbuild.1"
      # Metrics only: no container logs, no Application Signals.
      configuration_values = jsonencode({
        containerLogs = { enabled = false }
        manager       = { applicationSignals = { autoMonitor = { monitorAllServices = false } } }
      })
      pod_identity_association = [{
        role_arn        = aws_iam_role.pod_identity["cloudwatch-agent"].arn
        service_account = "cloudwatch-agent"
      }]
      preserve = false # turning the variable off removes the agent too
    }
  } : {})

  # With the VPC CNI, pods use the node's security group. The module only opens
  # node-to-node ports 53 and 1025-65535, so a pod on one node could not reach a pod on
  # the other on a port below 1025 (a sandbox serving on 80, say). Open all ports
  # between the two nodes.
  node_security_group_additional_rules = {
    ingress_self_all = {
      description = "Node to node, all ports (pod to pod across nodes)"
      protocol    = "-1"
      from_port   = 0
      to_port     = 0
      type        = "ingress"
      self        = true
    }
  }

  eks_managed_node_groups = {
    nodes = {
      name            = local.nodegroup_name
      use_name_prefix = false

      ami_type                       = "AL2023_x86_64_STANDARD"
      ami_release_version            = var.ami_release_version
      use_latest_ami_release_version = false
      instance_types                 = [var.instance_type]

      # Desired 2 at creation. Terraform ignores later changes to desired size, so the
      # node group can be scaled to 0 between sessions without a re-apply undoing it.
      min_size     = 0
      max_size     = 2
      desired_size = 2

      # Exposes VT-x to the node, so the kvm module loads and /dev/kvm exists.
      cpu_options = {
        nested_virtualization = "enabled"
      }

      # IMDSv2 only, with a hop limit of 1: the token response can't make the extra hop
      # into a pod's network namespace, so pods can't get a token or the node role's
      # credentials. (hostNetwork pods still can: they share the node's namespace.)
      metadata_options = {
        http_endpoint               = "enabled"
        http_tokens                 = "required"
        http_put_response_hop_limit = 1
      }

      # The AMI's 20 GiB root volume fills up with images, Kata guest assets and sandbox
      # layers. (disk_size is ignored when the module builds the launch template.)
      block_device_mappings = {
        root = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 100
            volume_type           = "gp3"
            encrypted             = true
            delete_on_termination = true
          }
        }
      }
    }
  }

  # Passed to the launch template too, so the instances and their volumes carry the
  # project tag (provider default tags don't reach resources the node group launches).
  tags = local.tags
}

# --- EKS Pod Identity roles ---------------------------------------------------
#
# One role per service account. The trust policy only lets EKS Pod Identity assume the
# role for that service account, in that namespace, in this cluster (the session tags
# Pod Identity adds to every request).

locals {
  pod_identity_roles = merge({
    ebs-csi-controller = {
      namespace       = "kube-system"
      service_account = "ebs-csi-controller-sa"
    }
    aws-load-balancer-controller = {
      namespace       = "kube-system"
      service_account = "aws-load-balancer-controller"
    }
    }, var.container_insights ? { # part 6 only
    cloudwatch-agent = {
      namespace       = "amazon-cloudwatch"
      service_account = "cloudwatch-agent"
    }
  } : {})
}

data "aws_iam_policy_document" "pod_identity_trust" {
  for_each = local.pod_identity_roles

  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/eks-cluster-name"
      values   = [var.cluster_name]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-namespace"
      values   = [each.value.namespace]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-service-account"
      values   = [each.value.service_account]
    }
  }
}

resource "aws_iam_role" "pod_identity" {
  for_each = local.pod_identity_roles

  name               = "${var.cluster_name}-${each.key}"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_trust[each.key].json
}

resource "aws_iam_role_policy_attachment" "ebs_csi" {
  role       = aws_iam_role.pod_identity["ebs-csi-controller"].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy"
}

resource "aws_iam_role_policy_attachment" "cloudwatch" {
  count = var.container_insights ? 1 : 0 # part 6 only

  role       = aws_iam_role.pod_identity["cloudwatch-agent"].name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

# AWS Load Balancer Controller: the IAM policy the controller project publishes for
# this version (docs/install/iam_policy.json at tag v3.6.2), copied verbatim. Update
# it together with LBC_CHART_VERSION in 01_cluster.sh.
resource "aws_iam_policy" "lbc" {
  name   = "${var.cluster_name}-aws-load-balancer-controller"
  policy = file("${path.module}/lbc-iam-policy-v3.6.2.json")
}

resource "aws_iam_role_policy_attachment" "lbc" {
  role       = aws_iam_role.pod_identity["aws-load-balancer-controller"].name
  policy_arn = aws_iam_policy.lbc.arn
}

# The controller is installed with Helm (01_cluster.sh), so its association lives here
# rather than on an add-on. It must exist before the controller's pods start: Pod
# Identity only injects credentials into pods created after the association.
resource "aws_eks_pod_identity_association" "lbc" {
  cluster_name    = module.eks.cluster_name
  namespace       = local.pod_identity_roles["aws-load-balancer-controller"].namespace
  service_account = local.pod_identity_roles["aws-load-balancer-controller"].service_account
  role_arn        = aws_iam_role.pod_identity["aws-load-balancer-controller"].arn
}

# --- Container images ---------------------------------------------------------
#
# Every image goes under openshell-eks/. Repositories are created on the first push
# with these settings, so no script has to create them first.

data "aws_iam_policy_document" "ecr_repository_creation_trust" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecr.amazonaws.com"]
    }
  }
}

# ECR needs a role of its own to create repositories that carry tags.
resource "aws_iam_role" "ecr_repository_creation" {
  name               = "${var.cluster_name}-ecr-repository-creation"
  assume_role_policy = data.aws_iam_policy_document.ecr_repository_creation_trust.json
}

data "aws_iam_policy_document" "ecr_repository_creation" {
  statement {
    actions   = ["ecr:CreateRepository", "ecr:TagResource"]
    resources = ["arn:aws:ecr:${var.region}:${local.account_id}:repository/${var.ecr_prefix}/*"]
  }
}

resource "aws_iam_role_policy" "ecr_repository_creation" {
  role   = aws_iam_role.ecr_repository_creation.id
  policy = data.aws_iam_policy_document.ecr_repository_creation.json
}

resource "aws_ecr_repository_creation_template" "images" {
  prefix               = var.ecr_prefix
  description          = "OpenShell on Amazon EKS: mirrored images and the workload image"
  applied_for          = ["CREATE_ON_PUSH"]
  image_tag_mutability = "MUTABLE"
  custom_role_arn      = aws_iam_role.ecr_repository_creation.arn
  resource_tags        = local.tags

  # Untagged images (superseded pushes) go after 7 days. An image that a multi-arch
  # index still references is never expired, so mirrored indexes stay whole.
  lifecycle_policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after 7 days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = 7
      }
      action = { type = "expire" }
    }]
  })

  depends_on = [aws_iam_role_policy.ecr_repository_creation]
}

# Scan on push is a registry-wide setting (creation templates have none), and this
# account's registry is shared with other projects, so this Terraform leaves it alone.
# Scan a mirrored image on demand with:
#   aws ecr start-image-scan --repository-name openshell-eks/openshell/gateway --image-id imageTag=0.1.6
