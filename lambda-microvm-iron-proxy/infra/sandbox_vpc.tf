# The sandbox VPC: nothing but the MicroVM egress connector. No internet gateway, no NAT. The only routes
# besides `local` are the two NLB /32s, to the peering connection. Its DNS is in dns.tf.

resource "aws_vpc" "sandbox" {
  cidr_block           = local.sandbox_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${local.name}-sandbox" }
}

resource "aws_subnet" "connector" {
  count                = 2
  vpc_id               = aws_vpc.sandbox.id
  cidr_block           = "10.51.${count.index + 20}.0/24"
  availability_zone_id = var.az_ids[count.index]
  tags                 = { Name = "${local.name}-connector-${count.index}" }
}

resource "aws_route_table" "sandbox" {
  vpc_id = aws_vpc.sandbox.id
  tags   = { Name = "${local.name}-sandbox" }
}

resource "aws_route" "sandbox_to_nlb" {
  count                     = 2
  route_table_id            = aws_route_table.sandbox.id
  destination_cidr_block    = "${local.nlb_ips[count.index]}/32"
  vpc_peering_connection_id = aws_vpc_peering_connection.p.id
}

resource "aws_route_table_association" "connector" {
  count          = 2
  subnet_id      = aws_subnet.connector[count.index].id
  route_table_id = aws_route_table.sandbox.id
}

# ---------- Peering (same account, same region) ----------
# Non-transitive by construction: the sandbox VPC can't use the proxy VPC's NAT gateway or internet
# gateway. Remote DNS resolution over the peering stays off.
resource "aws_vpc_peering_connection" "p" {
  vpc_id      = aws_vpc.sandbox.id
  peer_vpc_id = aws_vpc.proxy.id
  auto_accept = true
  tags        = { Name = "${local.name}-sandbox-to-proxy" }
}

# ---------- The fence ----------
resource "aws_security_group" "connector" {
  name        = "${local.name}-connector"
  description = "MicroVM egress connector: may only reach the iron-proxy NLB in the peered proxy VPC"
  vpc_id      = aws_vpc.sandbox.id
}

# To the NLB's security group, referenced across the peering. No ingress rules: the MicroVM's own endpoint
# comes in through Lambda's managed ingress, not through this group.
resource "aws_vpc_security_group_egress_rule" "connector_to_nlb" {
  for_each                     = toset([for p in local.sandbox_ports : tostring(p)])
  security_group_id            = aws_security_group.connector.id
  referenced_security_group_id = aws_security_group.nlb.id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
  depends_on                   = [aws_vpc_peering_connection.p]
}

# ---------- The connector ----------
data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "operator" {
  name               = "${local.name}-connector-operator"
  assume_role_policy = data.aws_iam_policy_document.lambda_trust.json
}

resource "aws_iam_role_policy" "operator" {
  role = aws_iam_role.operator.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "CreateENI", Effect = "Allow", Action = "ec2:CreateNetworkInterface",
      Resource = ["arn:aws:ec2:*:*:network-interface/*", "arn:aws:ec2:*:*:subnet/*", "arn:aws:ec2:*:*:security-group/*"] },
      { Sid = "TagENI", Effect = "Allow", Action = "ec2:CreateTags", Resource = "arn:aws:ec2:*:*:network-interface/*",
      Condition = { StringEquals = { "ec2:ManagedResourceOperator" = "network-connectors.lambda.amazonaws.com" } } },
      { Sid    = "ManageENI", Effect = "Allow",
        Action = ["ec2:DeleteNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups", "ec2:DescribeVpcs"],
      Resource = "*" },
    ]
  })
}

resource "aws_lambdacore_network_connector" "sandbox" {
  name          = "${local.name}-sandbox"
  operator_role = aws_iam_role.operator.arn
  configuration {
    vpc_egress_configuration {
      subnet_ids                        = aws_subnet.connector[*].id
      security_group_ids                = [aws_security_group.connector.id]
      network_protocol                  = "IPv4"
      associated_compute_resource_types = ["MicroVm"]
    }
  }
  timeouts {
    create = "15m"
    delete = "20m"
  }
  # The DNS Firewall association is here so no MicroVM can start on the connector before every name is
  # rewritten to the proxy
  depends_on = [aws_iam_role_policy.operator, aws_route_table_association.connector,
  aws_route53_resolver_firewall_rule_group_association.sandbox, aws_route53_resolver_firewall_config.sandbox]
}
