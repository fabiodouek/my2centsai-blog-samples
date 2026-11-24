"""Put AWS WAF with the AI Security managed rule group in front of both front doors.

  python 04_protect.py --mode count      first rollout: the four blocking rules only count
  python 04_protect.py --mode enforce    the rule group's default actions (four rules block)

  --guardrails both|request|disable      what is sent to Amazon Bedrock Guardrails (default both)
  --fail-closed                          block when the Bedrock call fails (default: let it through)
  --no-scope-down                        evaluate every path, not only the AI paths
  --break-role / --fix-role              remove / restore the role's bedrock:InvokeGuardrailChecks permission
  --detach / --attach                    take the web ACLs off the front doors / put them back

Creates on first run: an IAM role AWS WAF assumes to call Bedrock, a CloudWatch log group
for WAF logs, and one web ACL per front door (REST API stage, ALB).
"""
import argparse
import json
import time

from botocore.exceptions import ClientError

from common import PREFIX, REGION, TAGS, account_id, client, head, load_state, save_state, say

TAG_LIST = [{"Key": k, "Value": v} for k, v in TAGS.items()]
ROLE = f"{PREFIX}-waf-bedrock"
LOG_GROUP = f"aws-waf-logs-{PREFIX}"  # WAF requires the aws-waf-logs- prefix
BLOCKING = ["Compliance_BaselineMCP", "Compliance_BaselineA2A",
            "Request_GuardrailsPromptAttackHighSeverity", "Response_GuardrailsPromptAttackHighSeverity"]
POLICY = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "bedrock:InvokeGuardrailChecks", "Resource": "*"}]}


def role(iam, acct):
    """The role AWS WAF assumes. There is no guardrail to create: this role is the whole Bedrock setup."""
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "wafv2.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": acct},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:wafv2:{REGION}:{acct}:regional/webacl/{PREFIX}-*/*"}}}]}
    try:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust), Tags=TAG_LIST,
                              Description="Assumed by AWS WAF AI Security to call Bedrock InvokeGuardrailChecks")["Role"]["Arn"]
        iam.put_role_policy(RoleName=ROLE, PolicyName="invoke-guardrail-checks", PolicyDocument=json.dumps(POLICY))
        say(f"  iam role  {ROLE}  (trusts wafv2.amazonaws.com, allows bedrock:InvokeGuardrailChecks)")
        time.sleep(10)
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
    return arn


def log_group(logs, acct):
    try:
        logs.create_log_group(logGroupName=LOG_GROUP, tags=TAGS)
        logs.put_retention_policy(logGroupName=LOG_GROUP, retentionInDays=7)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise
    return f"arn:aws:logs:{REGION}:{acct}:log-group:{LOG_GROUP}"


def ai_security_rule(role_arn, mode, guardrails, fail_closed, scope_down):
    if guardrails == "disable":
        cfg = {"Disable": {}}
    elif guardrails == "request":
        cfg = {"ProtectRequestOnly": {"RoleArn": role_arn}}
    else:
        cfg = {"ProtectRequestAndResponse": {"RoleArn": role_arn}}
    stmt = {"VendorName": "AWS", "Name": "AWSManagedRulesAISecurityRuleSet",
            # This block is mandatory: without it CreateWebACL rejects the rule group.
            "ManagedRuleGroupConfigs": [{"AWSManagedRulesAISecurityRuleSet": {"BedrockGuardrails": cfg}}]}
    overrides = []
    if mode == "count":
        overrides += [{"Name": n, "ActionToUse": {"Count": {}}} for n in BLOCKING]
    if fail_closed:
        overrides.append({"Name": "Guardrails_CallFailed", "ActionToUse": {"Block": {}}})
    if overrides:
        stmt["RuleActionOverrides"] = overrides
    if scope_down:  # only pay for, and only inspect, the AI paths
        def has(p):
            return {"ByteMatchStatement": {"SearchString": p.encode(), "FieldToMatch": {"UriPath": {}},
                                           "TextTransformations": [{"Priority": 0, "Type": "NONE"}], "PositionalConstraint": "CONTAINS"}}
        stmt["ScopeDownStatement"] = {"OrStatement": {"Statements": [has("/mcp"), has("/a2a"), has("/v1/")]}}
    return {"Name": "AI-Security", "Priority": 100, "Statement": {"ManagedRuleGroupStatement": stmt},
            "OverrideAction": {"None": {}},
            "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": "AI-Security"}}


def web_acl(waf, name, rule, log_arn, resource_arn):
    vis = {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": name}
    existing = [a for a in waf.list_web_acls(Scope="REGIONAL")["WebACLs"] if a["Name"] == name]
    if existing:
        a = existing[0]
        for attempt in range(5):
            try:
                token = waf.get_web_acl(Name=name, Scope="REGIONAL", Id=a["Id"])["LockToken"]
                waf.update_web_acl(Name=name, Scope="REGIONAL", Id=a["Id"], DefaultAction={"Allow": {}}, Rules=[rule],
                                   VisibilityConfig=vis, LockToken=token)
                break
            except waf.exceptions.WAFOptimisticLockException:
                time.sleep(2)
        say(f"  web acl  {name}  updated")
        return a["ARN"]
    arn = waf.create_web_acl(Name=name, Scope="REGIONAL", DefaultAction={"Allow": {}}, Rules=[rule], VisibilityConfig=vis,
                             Description="WAF AI Security demo", Tags=TAG_LIST)["Summary"]["ARN"]
    waf.put_logging_configuration(LoggingConfiguration={"ResourceArn": arn, "LogDestinationConfigs": [log_arn]})
    for attempt in range(40):  # a new web ACL takes a minute or two before it can be associated
        try:
            waf.associate_web_acl(WebACLArn=arn, ResourceArn=resource_arn)
            break
        except (waf.exceptions.WAFUnavailableEntityException, waf.exceptions.WAFNonexistentItemException):
            time.sleep(5)
    else:
        raise SystemExit(f"could not associate {name}")
    say(f"  web acl  {name}  created, logging on, associated")
    return arn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["count", "enforce"], default="count")
    ap.add_argument("--guardrails", choices=["both", "request", "disable"], default="both")
    ap.add_argument("--fail-closed", action="store_true")
    ap.add_argument("--no-scope-down", action="store_true")
    ap.add_argument("--break-role", action="store_true")
    ap.add_argument("--fix-role", action="store_true")
    ap.add_argument("--detach", action="store_true", help="take the web ACLs off both front doors (keeps them)")
    ap.add_argument("--attach", action="store_true", help="put the web ACLs back")
    ap.add_argument("--settle", type=int, default=60, help="seconds to wait for the change to reach the edge")
    a = ap.parse_args()
    s = load_state()
    acct = account_id()
    iam = client("iam")

    if a.detach or a.attach:
        waf = client("wafv2")
        head("Web ACL associations")
        for key, res in (("apigw", s["api_arn"]), ("alb", s["alb_arn"])):
            if a.detach:
                waf.disassociate_web_acl(ResourceArn=res)
            else:
                waf.associate_web_acl(WebACLArn=s["web_acls"][key], ResourceArn=res)
            say(f"  {PREFIX}-{key}  {'detached' if a.detach else 'attached'}")
        if a.settle:
            say(f"  waiting {a.settle}s for the change to propagate ...")
            time.sleep(a.settle)
        return

    if a.break_role or a.fix_role:
        head("IAM role for Bedrock")
        if a.break_role:
            # Swap in a policy that looks plausible but is the wrong action. WAF accepts the role anyway.
            wrong = {"Version": "2012-10-17", "Statement": [
                {"Effect": "Allow", "Action": "bedrock:ApplyGuardrail", "Resource": "*"}]}
            iam.put_role_policy(RoleName=ROLE, PolicyName="invoke-guardrail-checks", PolicyDocument=json.dumps(wrong))
            say("  policy now allows bedrock:ApplyGuardrail only (the wrong action)")
        else:
            iam.put_role_policy(RoleName=ROLE, PolicyName="invoke-guardrail-checks", PolicyDocument=json.dumps(POLICY))
            say("  policy allows bedrock:InvokeGuardrailChecks again")
        return

    head(f"AWS WAF AI Security: mode={a.mode}  guardrails={a.guardrails}  fail_closed={a.fail_closed}  scope_down={not a.no_scope_down}")
    role_arn = role(iam, acct)
    log_arn = log_group(client("logs"), acct)
    rule = ai_security_rule(role_arn, a.mode, a.guardrails, a.fail_closed, not a.no_scope_down)
    waf = client("wafv2")
    acls = {"apigw": web_acl(waf, f"{PREFIX}-apigw", rule, log_arn, s["api_arn"]),
            "alb": web_acl(waf, f"{PREFIX}-alb", rule, log_arn, s["alb_arn"])}
    save_state(web_acls=acls, log_group=LOG_GROUP, role_arn=role_arn, mode=a.mode, guardrails=a.guardrails)
    blocking = "none (all four overridden to Count)" if a.mode == "count" else ", ".join(BLOCKING)
    say(f"  blocking rules: {blocking}")
    if a.fail_closed:
        say("  Guardrails_CallFailed: Block")
    if a.settle:
        say(f"  waiting {a.settle}s for the change to propagate ...")
        time.sleep(a.settle)


if __name__ == "__main__":
    main()
