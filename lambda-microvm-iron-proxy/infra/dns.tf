# DNS in the sandbox VPC. Every name a sandbox looks up comes back as a CNAME to proxy.sandbox.internal,
# whose A record holds both NLB IPs, and nothing recurses to the internet.
#
#   private zone  sandbox.internal (sandbox VPC only): proxy A -> both NLB IPs, TTL 60
#   DNS Firewall  priority 100  ALLOW proxy.sandbox.internal.
#                 priority 200  BLOCK *  -> OVERRIDE CNAME proxy.sandbox.internal., TTL 60
#   fail mode     closed (FirewallFailOpen=DISABLED)
#   protection    mutation protection on the association (var.firewall_mutation_protection)
#   query log     every query the sandbox VPC's resolver answers (not cached ones), with the firewall action

resource "aws_route53_zone" "sandbox" {
  name          = "sandbox.internal"
  comment       = "${local.name}: the proxy name every sandbox lookup is rewritten to"
  force_destroy = true
  vpc {
    vpc_id = aws_vpc.sandbox.id
  }
}

resource "aws_route53_record" "proxy" {
  zone_id = aws_route53_zone.sandbox.zone_id
  name    = "proxy.sandbox.internal"
  type    = "A"
  ttl     = 60
  records = local.nlb_ips
}

# ---------- DNS Firewall ----------
resource "aws_route53_resolver_firewall_domain_list" "proxy" {
  name    = "${local.name}-proxy-name"
  domains = ["proxy.sandbox.internal."]
}

resource "aws_route53_resolver_firewall_domain_list" "everything" {
  name = "${local.name}-everything"
  # "*" matches every name; AWS stores it as "*.", so write it that way to avoid a permanent diff
  domains = ["*."]
}

resource "aws_route53_resolver_firewall_rule_group" "g" {
  name = "${local.name}-override"
}

resource "aws_route53_resolver_firewall_rule" "allow_proxy" {
  name                    = "allow-proxy-name"
  action                  = "ALLOW"
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.proxy.id
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.g.id
  priority                = 100
}

resource "aws_route53_resolver_firewall_rule" "override_all" {
  name                    = "everything-to-proxy"
  action                  = "BLOCK"
  block_response          = "OVERRIDE"
  block_override_dns_type = "CNAME"
  block_override_domain   = "proxy.sandbox.internal."
  block_override_ttl      = 60
  firewall_domain_list_id = aws_route53_resolver_firewall_domain_list.everything.id
  firewall_rule_group_id  = aws_route53_resolver_firewall_rule_group.g.id
  priority                = 200
}

resource "aws_route53_resolver_firewall_rule_group_association" "sandbox" {
  name                   = "${local.name}-sandbox"
  firewall_rule_group_id = aws_route53_resolver_firewall_rule_group.g.id
  vpc_id                 = aws_vpc.sandbox.id
  priority               = 101
  # With ENABLED, DisassociateFirewallRuleGroup fails (RSLVR-02307) until protection is switched off first
  mutation_protection = var.firewall_mutation_protection
  depends_on          = [aws_route53_resolver_firewall_rule.allow_proxy, aws_route53_resolver_firewall_rule.override_all]
}

# If DNS Firewall itself fails, queries are blocked rather than answered (the default, made explicit)
resource "aws_route53_resolver_firewall_config" "sandbox" {
  resource_id        = aws_vpc.sandbox.id
  firewall_fail_open = "DISABLED"
}

# ---------- Resolver query logging for the sandbox VPC ----------
resource "aws_cloudwatch_log_group" "queries" {
  name              = "/${local.name}/resolver-queries"
  retention_in_days = 7
}

locals {
  queries_log_arn = "arn:aws:logs:${var.region}:${local.account}:log-group:${aws_cloudwatch_log_group.queries.name}"
  alarm_log_arn   = "arn:aws:logs:${var.region}:${local.account}:log-group:${aws_cloudwatch_log_group.alarm.name}"
}

resource "aws_route53_resolver_query_log_config" "sandbox" {
  name            = "${local.name}-sandbox"
  destination_arn = local.queries_log_arn
  depends_on      = [aws_cloudwatch_log_resource_policy.logs]
}

resource "aws_route53_resolver_query_log_config_association" "sandbox" {
  resolver_query_log_config_id = aws_route53_resolver_query_log_config.sandbox.id
  resource_id                  = aws_vpc.sandbox.id
}

# One resource policy for both log groups that AWS services write to: the Resolver query log
# (CloudWatch Logs vended delivery) and the DNS Firewall alarm (EventBridge, alarm.tf).
resource "aws_cloudwatch_log_resource_policy" "logs" {
  policy_name = "${local.name}-logs"
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "Route53ResolverQueryLogs"
        Effect    = "Allow"
        Principal = { Service = "delivery.logs.amazonaws.com" }
        Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource  = "${local.queries_log_arn}:*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.account }
          ArnLike      = { "aws:SourceArn" = "arn:aws:logs:${var.region}:${local.account}:*" }
        }
      },
      {
        Sid       = "EventBridgeAlarm"
        Effect    = "Allow"
        Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
        Action    = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource  = "${local.alarm_log_arn}:*"
      },
    ]
  })
}
