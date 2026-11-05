# The proxy VPC: a public subnet with the NAT gateway, and two proxy subnets for iron-proxy and the
# internal NLB. It keeps normal Route 53 DNS: iron-proxy, ECS and Fargate need real answers.

resource "aws_vpc" "proxy" {
  cidr_block           = local.proxy_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = "${local.name}-proxy" }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.proxy.id
  tags   = { Name = "${local.name}-proxy" }
}

resource "aws_subnet" "public" {
  vpc_id               = aws_vpc.proxy.id
  cidr_block           = "10.50.0.0/24"
  availability_zone_id = var.az_ids[0]
  tags                 = { Name = "${local.name}-public" }
}

resource "aws_subnet" "proxy" {
  count                = 2
  vpc_id               = aws_vpc.proxy.id
  cidr_block           = "10.50.${count.index + 10}.0/24"
  availability_zone_id = var.az_ids[count.index]
  tags                 = { Name = "${local.name}-proxy-${count.index}" }
}

resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "${local.name}-nat" }
}

resource "aws_nat_gateway" "nat" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public.id
  depends_on    = [aws_internet_gateway.igw]
  tags          = { Name = local.name }
}

# Every route in this repo is a separate aws_route resource. A route table with inline `route` blocks
# owns all its routes, so mixing the two makes every apply delete the aws_route ones (the peering routes).
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.proxy.id
  tags   = { Name = "${local.name}-public" }
}

resource "aws_route" "public_igw" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.igw.id
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# The proxy subnets: the internet through the NAT, and a return route to the sandbox connector subnets
# (only those) over the peering connection.
resource "aws_route_table" "proxy" {
  vpc_id = aws_vpc.proxy.id
  tags   = { Name = "${local.name}-proxy" }
}

resource "aws_route" "proxy_nat" {
  route_table_id         = aws_route_table.proxy.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.nat.id
}

resource "aws_route" "proxy_to_connector" {
  count                     = 2
  route_table_id            = aws_route_table.proxy.id
  destination_cidr_block    = aws_subnet.connector[count.index].cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.p.id
}

resource "aws_route_table_association" "proxy" {
  count          = 2
  subnet_id      = aws_subnet.proxy[count.index].id
  route_table_id = aws_route_table.proxy.id
}

# ---------- Security groups in the proxy VPC ----------

resource "aws_security_group" "nlb" {
  name        = "${local.name}-nlb"
  description = "Internal NLB in front of iron-proxy: reachable only from the sandbox connector"
  vpc_id      = aws_vpc.proxy.id
}

# From the connector's security group in the peered sandbox VPC, referenced across the peering
resource "aws_vpc_security_group_ingress_rule" "nlb_from_connector" {
  for_each                     = toset([for p in local.sandbox_ports : tostring(p)])
  security_group_id            = aws_security_group.nlb.id
  referenced_security_group_id = aws_security_group.connector.id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.key)
  to_port                      = tonumber(each.key)
  depends_on                   = [aws_vpc_peering_connection.p]
}

resource "aws_vpc_security_group_egress_rule" "nlb_to_proxy" {
  security_group_id            = aws_security_group.nlb.id
  referenced_security_group_id = aws_security_group.proxy.id
  ip_protocol                  = "-1"
}

resource "aws_security_group" "proxy" {
  name        = "${local.name}-proxy"
  description = "iron-proxy tasks: traffic only from the NLB; egress to the internet via NAT"
  vpc_id      = aws_vpc.proxy.id
}

resource "aws_vpc_security_group_ingress_rule" "proxy_from_nlb" {
  security_group_id            = aws_security_group.proxy.id
  referenced_security_group_id = aws_security_group.nlb.id
  ip_protocol                  = "-1"
}

resource "aws_vpc_security_group_egress_rule" "proxy_out" {
  security_group_id = aws_security_group.proxy.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}
