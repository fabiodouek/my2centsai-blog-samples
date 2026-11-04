# Everything here is in part 2's proxy VPC. The ALB needs subnets in two AZs and part 2 has one public subnet
# (its first AZ, with the NAT gateway). Add one in the second AZ on part 2's public route table.
resource "aws_subnet" "public_b" {
  vpc_id               = local.p2.proxy_vpc_id
  cidr_block           = "10.50.1.0/24"
  availability_zone_id = local.p2.az_ids[1]
  tags                 = { Name = "${local.name}-public-b" }
}

resource "aws_route_table_association" "public_b" {
  subnet_id      = aws_subnet.public_b.id
  route_table_id = local.p2.public_route_table_id
}

# ---------- Security groups ----------

data "aws_ec2_managed_prefix_list" "cloudfront" {
  name = "com.amazonaws.global.cloudfront.origin-facing"
}

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Workspace ALB: HTTP only from CloudFront origin-facing addresses"
  vpc_id      = local.p2.proxy_vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cloudfront" {
  security_group_id = aws_security_group.alb.id
  prefix_list_id    = data.aws_ec2_managed_prefix_list.cloudfront.id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_to_api" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.api.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_security_group" "api" {
  name        = "${local.name}-api"
  description = "Workspace backend tasks: 8000 from the ALB; egress to AWS APIs and MicroVM endpoints via NAT"
  vpc_id      = local.p2.proxy_vpc_id
}

resource "aws_vpc_security_group_ingress_rule" "api_from_alb" {
  security_group_id            = aws_security_group.api.id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_vpc_security_group_egress_rule" "api_out" {
  security_group_id = aws_security_group.api.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ---------- ALB ----------

resource "aws_lb" "alb" {
  name                       = "${local.name}-alb"
  load_balancer_type         = "application"
  internal                   = false
  security_groups            = [aws_security_group.alb.id]
  subnets                    = [local.p2.public_subnet_id, aws_subnet.public_b.id]
  idle_timeout               = 3600 # terminal WebSockets stay open for a long time
  drop_invalid_header_fields = true
  depends_on                 = [aws_route_table_association.public_b]
}

resource "aws_lb_target_group" "api" {
  name                 = "${local.name}-api"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = local.p2.proxy_vpc_id
  deregistration_delay = 30
  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

# Anything that didn't come through CloudFront (no origin header) gets a 403
resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.alb.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Forbidden"
      status_code  = "403"
    }
  }
}

resource "aws_lb_listener_rule" "from_cloudfront" {
  listener_arn = aws_lb_listener.http.arn
  priority     = 1
  condition {
    http_header {
      http_header_name = "X-Origin-Verify"
      values           = [random_password.origin_secret.result]
    }
  }
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}
