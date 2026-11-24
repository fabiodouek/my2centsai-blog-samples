"""Delete everything the demo created, then list what is left with the project tag.

Order: WAF associations and web ACLs, the ALB and its target groups, the REST API,
the Lambda functions, the IAM roles, the log groups, the API Portal discovery configuration (stopped first).

  python teardown.py              delete everything
  python teardown.py --keep-discovery   leave API Portal discovery on
"""
import sys
import time

from botocore.exceptions import ClientError

from common import PREFIX, PROJECT, STATE_FILE, client, head, load_state, say


def quiet(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if any(x in code for x in ("NotFound", "NoSuchEntity", "Nonexistent", "ResourceNotFoundException")):
            return None
        say(f"  ! {fn.__name__}: {code}")
        return None


def main():
    s = load_state()
    waf, elb, apigw, lam, iam, logs, ec2 = (client(n) for n in ("wafv2", "elbv2", "apigateway", "lambda", "iam", "logs", "ec2"))

    head("AWS WAF")
    for arn in (s.get("api_arn"), s.get("alb_arn")):
        if arn:
            quiet(waf.disassociate_web_acl, ResourceArn=arn)
    for a in waf.list_web_acls(Scope="REGIONAL")["WebACLs"]:
        if a["Name"].startswith(PREFIX):
            quiet(waf.delete_logging_configuration, ResourceArn=a["ARN"])
            for _ in range(12):  # the association takes a moment to go away
                try:
                    token = waf.get_web_acl(Name=a["Name"], Scope="REGIONAL", Id=a["Id"])["LockToken"]
                    waf.delete_web_acl(Name=a["Name"], Scope="REGIONAL", Id=a["Id"], LockToken=token)
                    say(f"  deleted web acl {a['Name']}")
                    break
                except ClientError as e:
                    if e.response["Error"]["Code"] not in ("WAFAssociatedItemException", "WAFOptimisticLockException"):
                        raise
                    time.sleep(10)

    head("Application Load Balancer")
    for lb in elb.describe_load_balancers()["LoadBalancers"]:
        if lb["LoadBalancerName"] == f"{PREFIX}-alb":
            for li in elb.describe_listeners(LoadBalancerArn=lb["LoadBalancerArn"])["Listeners"]:
                quiet(elb.delete_listener, ListenerArn=li["ListenerArn"])
            elb.delete_load_balancer(LoadBalancerArn=lb["LoadBalancerArn"])
            elb.get_waiter("load_balancers_deleted").wait(LoadBalancerArns=[lb["LoadBalancerArn"]])
            say(f"  deleted {lb['LoadBalancerName']}")
    for tg in elb.describe_target_groups()["TargetGroups"]:
        if tg["TargetGroupName"].startswith(PREFIX):
            quiet(elb.delete_target_group, TargetGroupArn=tg["TargetGroupArn"])
            say(f"  deleted target group {tg['TargetGroupName']}")
    for attempt in range(18):  # the security group is free once the ALB's network interfaces are gone
        sgs = ec2.describe_security_groups(Filters=[{"Name": "group-name", "Values": [f"{PREFIX}-alb"]}])["SecurityGroups"]
        if not sgs:
            break
        try:
            ec2.delete_security_group(GroupId=sgs[0]["GroupId"])
            say("  deleted security group")
            break
        except ClientError:
            time.sleep(10)

    head("API Gateway and Lambda")
    for api in apigw.get_rest_apis(limit=500)["items"]:
        if api["name"] == f"{PREFIX}-estate":
            apigw.delete_rest_api(restApiId=api["id"])
            say(f"  deleted rest api {api['name']}")
    for key in ("mcp", "a2a", "llm"):
        if quiet(lam.delete_function, FunctionName=f"{PREFIX}-{key}") is not None:
            say(f"  deleted lambda {PREFIX}-{key}")
        quiet(logs.delete_log_group, logGroupName=f"/aws/lambda/{PREFIX}-{key}")

    head("IAM roles and log groups")
    quiet(iam.delete_role_policy, RoleName=f"{PREFIX}-waf-bedrock", PolicyName="invoke-guardrail-checks")
    quiet(iam.delete_role, RoleName=f"{PREFIX}-waf-bedrock")
    quiet(iam.detach_role_policy, RoleName=f"{PREFIX}-lambda", PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    quiet(iam.delete_role, RoleName=f"{PREFIX}-lambda")
    quiet(logs.delete_log_group, logGroupName=f"aws-waf-logs-{PREFIX}")
    say("  done")

    if "--keep-discovery" not in sys.argv:
        head("API Portal discovery")
        ap = client("apiportal")
        try:  # a running configuration cannot be deleted: stop it first
            arn = ap.get_discovery_configuration()["discoveryConfiguration"]["arn"]
            quiet(ap.stop_discovery, id=arn)
            for _ in range(30):
                if all(x["status"] == "STOPPED" for x in ap.list_discovery_statuses()["items"]):
                    break
                time.sleep(10)
            ap.delete_discovery_configuration()
            say("  stopped discovery and deleted the configuration")
        except ClientError as e:
            say(f"  nothing to delete ({e.response['Error']['Code']})")
        time.sleep(5)
        try:
            cfg = ap.get_discovery_configuration()["discoveryConfiguration"]
            say(f"  configuration still there: status {cfg['status']}")
        except ClientError as e:
            say(f"  discovery configuration gone ({e.response['Error']['Code']})")
        quiet(iam.delete_service_linked_role, RoleName="AWSServiceRoleForAPIPortal")  # created when discovery was enabled
        st = ap.list_discovery_statuses()["items"]
        say("  statuses: " + (", ".join(f"{x['discoverySource']}={x['status']}" for x in st) or "none"))

    head(f"What is left with the tag project={PROJECT}")
    left = []
    for page in client("resourcegroupstaggingapi").get_paginator("get_resources").paginate(TagFilters=[{"Key": "project", "Values": [PROJECT]}]):
        left += [r["ResourceARN"] for r in page["ResourceTagMappingList"]]
    for arn in left:
        say(f"  {arn}")
    if not left:
        say("  nothing")
    else:
        say("  (the tagging API can list deleted resources for a few minutes; run this again to confirm)")
    if STATE_FILE.exists():
        STATE_FILE.unlink()


if __name__ == "__main__":
    main()
