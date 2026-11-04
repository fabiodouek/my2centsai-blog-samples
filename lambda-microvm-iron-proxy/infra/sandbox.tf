# What the MicroVMs need besides the network: an artifact bucket and build role for the image, a demo
# bucket for the read-only AWS calls, and a logs-only execution role used only for the contrast run in
# 08_leaks.py (every other MicroVM runs with no execution role; aws_auth signs instead).

resource "aws_s3_bucket" "artifacts" {
  bucket_prefix = "${local.name}-artifacts-"
  force_destroy = true
}

resource "aws_s3_bucket" "demo" {
  bucket_prefix = "${local.name}-demo-"
  force_destroy = true
}

resource "aws_s3_object" "demo" {
  # "q3 summary.txt" has a space: iron-proxy v0.55.0 re-signs such keys wrongly (issue #220)
  for_each = toset(["reports/q3-summary.txt", "reports/q4-plan.txt", "reports/q3 summary.txt", "notes/readme.txt"])
  bucket   = aws_s3_bucket.demo.id
  key      = each.key
  content  = "demo object ${each.key}\n"
}

resource "aws_iam_role" "build" {
  name               = "${local.name}-build"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy" "build" {
  role = aws_iam_role.build.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.artifacts.arn}/*" },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      Resource = "arn:aws:logs:${var.region}:*:log-group:/aws/lambda-microvms/*" },
    ]
  })
}

# Only for the contrast run in 08_leaks.py: what the agent can read from the metadata endpoint when a role exists
resource "aws_iam_role" "exec_logs" {
  name               = "${local.name}-exec-logs"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy" "exec_logs" {
  role = aws_iam_role.exec_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
    Resource = "arn:aws:logs:${var.region}:*:log-group:/aws/lambda-microvms/*" }]
  })
}
