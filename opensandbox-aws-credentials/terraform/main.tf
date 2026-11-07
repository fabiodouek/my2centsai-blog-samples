# Part 5: the AWS side of giving a sandbox AWS access without putting a key in it.
#
#   osb-broker   EKS Pod Identity role of the in-cluster broker (namespace opensandbox-broker,
#                service account osb-broker). It may assume osb-sandbox, plus one read-only IAM action
#                (iam:GetRequestAuthorizationDetails) for `broker.py deny-check`.
#   osb-sandbox  the role the broker assumes once per sandbox, with the sandbox ID as the
#                session name. Only the broker may assume it. It allows the test bucket, the
#                test table and one Bedrock model, and explicitly denies the credential-vending
#                actions in broker/vending_actions.json (broker/sandbox_permissions.json).
#   osb-cred-test  a DynamoDB table for the non-S3 tests.
#
# Its own state, separate from ../opensandbox-eks/terraform: applying or destroying it changes
# nothing in the cluster stack. 01_iam.sh runs it.

data "aws_caller_identity" "current" {}

# Fails the plan early if the cluster from ../opensandbox-eks is not there.
data "aws_eks_cluster" "this" {
  name = var.cluster_name
}

locals {
  tags       = { project = "opensandbox-eks", part = "opensandbox-aws-credentials" }
  account_id = data.aws_caller_identity.current.account_id

  broker_namespace       = "opensandbox-broker"
  broker_service_account = "osb-broker"

  # The sandbox role's permissions come from two files in broker/, which the broker also builds
  # every session policy from, so the role and the sessions can't drift apart:
  #   sandbox_permissions.json  the Allow statements, a template filled in here
  #   vending_actions.json      the actions of the explicit Deny
  sandbox_allows = jsondecode(templatefile("${path.module}/../broker/sandbox_permissions.json", {
    region           = var.region
    account          = local.account_id
    bucket           = var.test_bucket
    table            = var.ddb_table
    model            = var.bedrock_model_id
    foundation_model = trimprefix(var.bedrock_model_id, "us.")
  }))
  vending_actions = jsondecode(file("${path.module}/../broker/vending_actions.json"))
}

# --- The broker's role (EKS Pod Identity) -----------------------------------------

# Same trust shape as the cluster's own Pod Identity roles: only EKS Pod Identity, only for
# this service account in this namespace of this cluster (the session tags it adds).
data "aws_iam_policy_document" "broker_trust" {
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
      values   = [local.broker_namespace]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/kubernetes-service-account"
      values   = [local.broker_service_account]
    }
  }
}

resource "aws_iam_role" "broker" {
  name               = "osb-broker"
  description        = "OpenSandbox part 5: the in-cluster credential broker (EKS Pod Identity)"
  assume_role_policy = data.aws_iam_policy_document.broker_trust.json
}

# The broker's own credentials are an EKS Pod Identity role session, so its AssumeRole on
# osb-sandbox is role chaining: AWS caps that session at one hour. Pod Identity's session
# tags are transitive and travel with the chained call. That needs sts:TagSession in
# osb-sandbox's trust policy (below), not here: tested on 2026-11-01, removing it from the
# trust made the broker's AssumeRole fail with AccessDenied on sts:TagSession, while removing
# it from this policy changed nothing.
data "aws_iam_policy_document" "broker" {
  statement {
    actions   = ["sts:AssumeRole"]
    resources = [aws_iam_role.sandbox.arn]
  }

  # Read-only, for `broker.py deny-check`: AWS's AccessDenied for a sandbox session's AssumeRole
  # gives no reason, only an authorization id, and the authorization details show whether the
  # explicit Deny refused it. The API (iam-toolbox GetRequestAuthorizationDetails) is in
  # preview, so IAM Access Analyzer doesn't know this action name yet.
  statement {
    actions   = ["iam:GetRequestAuthorizationDetails"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "broker" {
  name   = "assume-osb-sandbox"
  role   = aws_iam_role.broker.id
  policy = data.aws_iam_policy_document.broker.json
}

# Pod Identity only injects credentials into pods created after the association exists:
# 01_iam.sh runs before 02_broker.sh starts the broker.
resource "aws_eks_pod_identity_association" "broker" {
  cluster_name    = data.aws_eks_cluster.this.name
  namespace       = local.broker_namespace
  service_account = local.broker_service_account
  role_arn        = aws_iam_role.broker.arn
}

# --- The sandbox role -------------------------------------------------------------
#
# No Pod Identity association and no service account: sandbox pods never get this role.
# Only the broker assumes it, and the sandbox only ever sees placeholder keys.

# sts:TagSession is required: the broker's chained AssumeRole carries Pod Identity's transitive
# session tags (see the broker policy above).
data "aws_iam_policy_document" "sandbox_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "AWS"
      identifiers = [aws_iam_role.broker.arn]
    }
  }
}

resource "aws_iam_role" "sandbox" {
  name               = "osb-sandbox"
  description        = "OpenSandbox part 5: assumed by osb-broker once per sandbox"
  assume_role_policy = data.aws_iam_policy_document.sandbox_trust.json
  # The role itself allows two hours, on purpose: the broker's own credentials are a role session (EKS Pod
  # Identity), so its AssumeRole is role chaining, which AWS caps at one hour whatever the role allows. With
  # the role at its 1 hour default, a request for more would be refused on the role's own limit and the
  # chaining cap would never show. The broker asks for 3,600 s, so nothing else changes.
  max_session_duration = 7200
}

# What broker/sandbox_permissions.json allows:
#   - the test bucket: ListBucket on it; GetObject, PutObject, DeleteObject and
#     AbortMultipartUpload on its objects;
#   - the test table: the *Item actions, Query and Scan;
#   - one Bedrock model: InvokeModel and InvokeModelWithResponseStream on the inference profile
#     in var.region and on the foundation model it routes to, in any of the US Regions (there is
#     no bedrock:Converse action: Converse and ConverseStream are authorized as these two, and
#     IAM Access Analyzer rejects the other names);
#   - CallWithBearerToken for short-term Bedrock API keys only (the broker mints them from the
#     sandbox's session).
# The Deny: the proxy re-signs whatever the sandbox sends, so any action that returns
# credentials in its response would hand the sandbox real ones. vending_actions.json lists all
# of STS except GetCallerIdentity, GetAccessKeyInfo and DecodeAuthorizationMessage (as the
# SigV4 addon does), all of IAM, and the token and credential calls of ECR, CodeArtifact,
# S3 Express, Redshift and Secrets Manager. An explicit Deny wins over any Allow, including
# one added to this role later.
resource "aws_iam_role_policy" "sandbox" {
  name = "osb-sandbox-test-access"
  role = aws_iam_role.sandbox.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.sandbox_allows, [{
      Sid      = "NoCredentialVending"
      Effect   = "Deny"
      Action   = local.vending_actions
      Resource = "*"
    }])
  })
}

# --- Test table ------------------------------------------------------------------

resource "aws_dynamodb_table" "test" {
  name         = var.ddb_table
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"

  attribute {
    name = "pk"
    type = "S"
  }
}
