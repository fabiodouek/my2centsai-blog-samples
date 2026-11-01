# The AWS side of part 5: one throwaway bucket, and two roles.
#
#   gateway Pod --(EKS Pod Identity)--> osh-eks-gateway --(sts:AssumeRole)--> osh-eks-sandbox-s3bedrock
#
# The gateway is the only AWS principal in the cluster. It can do nothing except assume
# osh-eks-sandbox-* roles. Sandboxes never get an IAM identity of their own: their supervisor
# signs requests with the sandbox-role session the gateway minted.

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  # Every role the gateway may assume starts with this. The sandbox role below is one.
  sandbox_role_prefix = "${var.cluster_name}-sandbox-"
}

resource "random_id" "bucket" {
  byte_length = 4
}

# --- Bucket ---------------------------------------------------------------------------

resource "aws_s3_bucket" "creds" {
  bucket = "${var.cluster_name}-creds-${random_id.bucket.hex}"
  # Test objects only: terraform destroy empties and deletes it.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "creds" {
  bucket                  = aws_s3_bucket.creds.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "creds" {
  bucket = aws_s3_bucket.creds.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Anything a test leaves behind is gone after a week, even if nobody runs the cleanup.
resource "aws_s3_bucket_lifecycle_configuration" "creds" {
  bucket = aws_s3_bucket.creds.id

  rule {
    id     = "expire-test-objects"
    status = "Enabled"
    filter {}

    expiration {
      days = 7
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }
}

# --- Gateway role (EKS Pod Identity) --------------------------------------------------------

# Same trust as part 2's Pod Identity roles: the EKS Pod Identity service, limited by the
# session tags it sets to this cluster, namespace and ServiceAccount.
data "aws_iam_policy_document" "gateway_trust" {
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
      values   = [var.gateway_namespace]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-service-account"
      values   = [var.gateway_service_account]
    }
  }
}

resource "aws_iam_role" "gateway" {
  name                 = "${var.cluster_name}-gateway"
  description          = "OpenShell gateway (EKS Pod Identity): may only assume ${local.sandbox_role_prefix}* roles"
  assume_role_policy   = data.aws_iam_policy_document.gateway_trust.json
  max_session_duration = 3600
}

# sts:TagSession because Pod Identity's session tags are transitive: the gateway's
# AssumeRole carries them into the sandbox-role session.
data "aws_iam_policy_document" "gateway" {
  statement {
    sid       = "AssumeSandboxRoles"
    actions   = ["sts:AssumeRole", "sts:TagSession"]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/${local.sandbox_role_prefix}*"]
  }
}

resource "aws_iam_role_policy" "gateway" {
  name   = "assume-sandbox-roles"
  role   = aws_iam_role.gateway.id
  policy = data.aws_iam_policy_document.gateway.json
}

# New gateway Pods get AWS_CONTAINER_CREDENTIALS_FULL_URI and the projected token. A running
# Pod doesn't: 01_setup.sh restarts the StatefulSet after this exists.
resource "aws_eks_pod_identity_association" "gateway" {
  cluster_name    = var.cluster_name
  namespace       = var.gateway_namespace
  service_account = var.gateway_service_account
  role_arn        = aws_iam_role.gateway.arn
}

# --- Sandbox role -----------------------------------------------------------------------------

# Only the gateway role may assume it. TagSession for the inherited transitive tags (see above).
data "aws_iam_policy_document" "sandbox_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.gateway.arn]
    }
  }
}

resource "aws_iam_role" "sandbox" {
  name        = "${local.sandbox_role_prefix}s3bedrock"
  description = "Sandbox session minted by the OpenShell gateway: one bucket, one Bedrock model"
  # The gateway's own credentials are a role session, so its AssumeRole is role chaining,
  # which AWS caps at one hour whatever this says. 3600 matches the profile's max_lifetime_seconds.
  assume_role_policy   = data.aws_iam_policy_document.sandbox_trust.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "sandbox" {
  statement {
    sid       = "ListTheBucket"
    actions   = ["s3:ListBucket", "s3:ListBucketMultipartUploads"]
    resources = [aws_s3_bucket.creds.arn]
  }

  # PutObject also covers CreateMultipartUpload, UploadPart and CompleteMultipartUpload;
  # DeleteObject also covers the keys in a DeleteObjects (POST ?delete) request.
  statement {
    sid = "ObjectsInTheBucket"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:AbortMultipartUpload",
      "s3:ListMultipartUploadParts",
    ]
    resources = ["${aws_s3_bucket.creds.arn}/*"]
  }

  # Converse and ConverseStream authorize as InvokeModel and InvokeModelWithResponseStream.
  # Through the us. inference profile, the call needs the profile and the model in every
  # Region the profile routes to.
  statement {
    sid     = "OneBedrockModel"
    actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = concat(
      [for r in var.bedrock_profile_regions : "arn:${local.partition}:bedrock:${r}::foundation-model/${var.bedrock_model_id}"],
      ["arn:${local.partition}:bedrock:${var.region}:${local.account_id}:inference-profile/us.${var.bedrock_model_id}"],
    )
  }

  # GetCallerIdentity needs no permission (it can't be denied); listed so the policy says
  # what the tests call.
  statement {
    sid       = "WhoAmI"
    actions   = ["sts:GetCallerIdentity"]
    resources = ["*"]
  }

  # The supervisor signs every request that reaches a signed endpoint, and OpenShell doesn't
  # filter which API is called or rewrite responses. An API that returns credentials in its
  # response body would hand the sandbox a real, usable key. IAM is the only place to stop
  # that, so deny them explicitly, even though nothing above allows them.
  statement {
    sid    = "NoCredentialVendingOrEscalation"
    effect = "Deny"
    actions = [
      "sts:AssumeRole*",
      "sts:AssumeRoot",
      "sts:GetSessionToken",
      "sts:GetFederationToken",
      "sts:GetServiceBearerToken",
      "sts:SetSourceIdentity",
      "ecr:GetAuthorizationToken",
      "ecr-public:GetAuthorizationToken",
      "codeartifact:GetAuthorizationToken",
      "s3express:CreateSession",
      "secretsmanager:GetSecretValue",
      "secretsmanager:BatchGetSecretValue",
      "ssm:GetParameter*",
      "ssm:StartSession",
      "ssm:SendCommand",
      "rds-db:connect",
      "redshift:GetClusterCredentials*",
      "redshift-serverless:GetCredentials",
      "bedrock:CallWithBearerToken",
      "iam:*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "sandbox" {
  name   = "s3-bedrock-least-privilege"
  role   = aws_iam_role.sandbox.id
  policy = data.aws_iam_policy_document.sandbox.json
}
