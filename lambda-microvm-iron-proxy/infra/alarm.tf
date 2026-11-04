# The misconfiguration alarm. If the DNS Firewall stops overriding a name, the Resolver goes back to recursive
# resolution at once and DNS becomes a channel out again (12_dns_firewall.py shows it for a disassociation).
# Mutation protection covers only the association. The rules, the domain lists and any other rule group aren't
# protected, and one call to any of them can let an ALLOW match first: a new ALLOW rule, `*.` imported into the
# allow list, an ALLOW-all rule group associated at a higher priority. So the rule matches every DNS Firewall
# write, plus the query-log disassociation. An EventBridge rule on the CloudTrail events sends each one to a log
# group (readable evidence) and to an SNS topic with no subscriptions (wire it to your pager). Refused calls
# arrive too, with an errorCode.

resource "aws_cloudwatch_log_group" "alarm" {
  # EventBridge can only target log groups under /aws/events/
  name              = "/aws/events/${local.name}-dns-firewall-alarm"
  retention_in_days = 7
}

resource "aws_sns_topic" "alarm" {
  name = "${local.name}-dns-firewall-alarm"
}

resource "aws_sns_topic_policy" "alarm" {
  arn = aws_sns_topic.alarm.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "EventBridgePublish"
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sns:Publish"
      Resource  = aws_sns_topic.alarm.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.alarm.arn } }
    }]
  })
}

resource "aws_cloudwatch_event_rule" "alarm" {
  name        = "${local.name}-dns-firewall-changed"
  description = "DNS Firewall protection on the sandbox VPC was removed or weakened"
  event_pattern = jsonencode({
    source        = ["aws.route53resolver"]
    "detail-type" = ["AWS API Call via CloudTrail"]
    detail = {
      eventSource = ["route53resolver.amazonaws.com"]
      eventName = [
        # rules
        "CreateFirewallRule",
        "UpdateFirewallRule",
        "DeleteFirewallRule",
        # rule groups
        "CreateFirewallRuleGroup",
        "DeleteFirewallRuleGroup",
        # domain lists
        "CreateFirewallDomainList",
        "UpdateFirewallDomains",
        "ImportFirewallDomains",
        "DeleteFirewallDomainList",
        # associations with a VPC
        "AssociateFirewallRuleGroup",
        "UpdateFirewallRuleGroupAssociation",
        "DisassociateFirewallRuleGroup",
        # the VPC's fail-open setting
        "UpdateFirewallConfig",
        # the query log
        "DisassociateResolverQueryLogConfig",
      ]
    }
  })
}

resource "aws_cloudwatch_event_target" "alarm_logs" {
  rule = aws_cloudwatch_event_rule.alarm.name
  arn  = local.alarm_log_arn
}

resource "aws_cloudwatch_event_target" "alarm_sns" {
  rule = aws_cloudwatch_event_rule.alarm.name
  arn  = aws_sns_topic.alarm.arn
}
