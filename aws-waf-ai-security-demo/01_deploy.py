"""Deploy the sample estate: three Lambda-backed AI endpoints that nobody registered.

  API Gateway REST API  /mcp  /a2a  /.well-known/agent-card.json  /v1/chat/completions
  Application Load Balancer (HTTP, your IP only) with the same paths

Nothing here has AWS WAF in front of it yet.
"""
import io
import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from botocore.exceptions import ClientError

from common import PREFIX, REGION, TAGS, account_id, client, head, save_state, say

HERE = Path(__file__).parent
FUNCS = {"mcp": "mcp_server", "a2a": "a2a_agent", "llm": "llm_stub"}
TAG_LIST = [{"Key": k, "Value": v} for k, v in TAGS.items()]


def zip_of(module):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HERE / "lambdas" / f"{module}.py", f"{module}.py")
    return buf.getvalue()


def lambda_role(iam):
    name = f"{PREFIX}-lambda"
    trust = {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"}, "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust), Tags=TAG_LIST)["Role"]["Arn"]
        iam.attach_role_policy(RoleName=name, PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
        time.sleep(10)  # let the role propagate
    except ClientError as e:
        if e.response["Error"]["Code"] != "EntityAlreadyExists":
            raise
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
    return arn


def functions(lam, role):
    arns = {}
    for key, module in FUNCS.items():
        name = f"{PREFIX}-{key}"
        for attempt in range(6):
            try:
                arns[key] = lam.create_function(
                    FunctionName=name, Runtime="python3.13", Role=role, Handler=f"{module}.handler",
                    Code={"ZipFile": zip_of(module)}, Timeout=10, MemorySize=256, Tags=TAGS)["FunctionArn"]
                break
            except ClientError as e:
                code = e.response["Error"]["Code"]
                if code == "ResourceConflictException":
                    lam.update_function_code(FunctionName=name, ZipFile=zip_of(module))
                    arns[key] = lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]
                    break
                if code == "InvalidParameterValueException" and attempt < 5:  # role not ready yet
                    time.sleep(5)
                    continue
                raise
        lam.get_waiter("function_active_v2").wait(FunctionName=name)
        say(f"  lambda  {name}")
    return arns


def rest_api(apigw, lam, arns, acct):
    api = apigw.create_rest_api(name=f"{PREFIX}-estate", description="WAF AI Security demo: unregistered AI endpoints",
                                endpointConfiguration={"types": ["REGIONAL"]}, tags=TAGS)["id"]
    root = [r for r in apigw.get_resources(restApiId=api)["items"] if r["path"] == "/"][0]["id"]

    def res(parent, part):
        return apigw.create_resource(restApiId=api, parentId=parent, pathPart=part)["id"]

    def method(rid, verb, key):
        apigw.put_method(restApiId=api, resourceId=rid, httpMethod=verb, authorizationType="NONE")
        uri = f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/{arns[key]}/invocations"
        apigw.put_integration(restApiId=api, resourceId=rid, httpMethod=verb, type="AWS_PROXY", integrationHttpMethod="POST", uri=uri)

    method(res(root, "mcp"), "POST", "mcp")
    a2a = res(root, "a2a")
    method(a2a, "POST", "a2a")
    method(res(a2a, "message:send"), "POST", "a2a")
    method(res(res(root, ".well-known"), "agent-card.json"), "GET", "a2a")
    method(res(res(res(root, "v1"), "chat"), "completions"), "POST", "llm")
    for key in FUNCS:
        lam.add_permission(FunctionName=f"{PREFIX}-{key}", StatementId=f"apigw-{api}", Action="lambda:InvokeFunction",
                           Principal="apigateway.amazonaws.com", SourceArn=f"arn:aws:execute-api:{REGION}:{acct}:{api}/*")
    apigw.create_deployment(restApiId=api, stageName="prod")
    say(f"  rest api  {PREFIX}-estate  stage prod")
    return api


def alb(ec2, elb, lam, arns):
    my_ip = urllib.request.urlopen("https://checkip.amazonaws.com", timeout=10).read().decode().strip()
    vpc = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"][0]["VpcId"]
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc]}, {"Name": "default-for-az", "Values": ["true"]}])["Subnets"]
    subnet_ids = [s["SubnetId"] for s in sorted(subnets, key=lambda s: s["AvailabilityZone"])][:3]
    sg = ec2.create_security_group(GroupName=f"{PREFIX}-alb", Description="WAF AI Security demo ALB: HTTP from one IP", VpcId=vpc,
                                   TagSpecifications=[{"ResourceType": "security-group", "Tags": TAG_LIST}])["GroupId"]
    ec2.authorize_security_group_ingress(GroupId=sg, IpPermissions=[
        {"IpProtocol": "tcp", "FromPort": 80, "ToPort": 80, "IpRanges": [{"CidrIp": f"{my_ip}/32", "Description": "demo client"}]}])
    lb = elb.create_load_balancer(Name=f"{PREFIX}-alb", Subnets=subnet_ids, SecurityGroups=[sg], Scheme="internet-facing",
                                  Type="application", Tags=TAG_LIST)["LoadBalancers"][0]
    tgs = {}
    for key in FUNCS:
        tg = elb.create_target_group(Name=f"{PREFIX}-{key}", TargetType="lambda", Tags=TAG_LIST)["TargetGroups"][0]["TargetGroupArn"]
        lam.add_permission(FunctionName=f"{PREFIX}-{key}", StatementId="alb-invoke", Action="lambda:InvokeFunction",
                           Principal="elasticloadbalancing.amazonaws.com", SourceArn=tg)
        elb.register_targets(TargetGroupArn=tg, Targets=[{"Id": arns[key]}])
        tgs[key] = tg
    listener = elb.create_listener(LoadBalancerArn=lb["LoadBalancerArn"], Protocol="HTTP", Port=80, Tags=TAG_LIST, DefaultActions=[
        {"Type": "fixed-response", "FixedResponseConfig": {"StatusCode": "404", "ContentType": "text/plain", "MessageBody": "not found"}}])["Listeners"][0]["ListenerArn"]
    rules = [("mcp", ["/mcp"]), ("a2a", ["/a2a", "/a2a/*", "/.well-known/*"]), ("llm", ["/v1/*"])]
    for prio, (key, paths) in enumerate(rules, start=10):
        elb.create_rule(ListenerArn=listener, Priority=prio, Conditions=[{"Field": "path-pattern", "Values": paths}],
                        Actions=[{"Type": "forward", "TargetGroupArn": tgs[key]}], Tags=TAG_LIST)
    say(f"  alb  {PREFIX}-alb  (HTTP 80, open to this machine's IP only), waiting until it is active ...")
    elb.get_waiter("load_balancer_available").wait(LoadBalancerArns=[lb["LoadBalancerArn"]])
    return lb["LoadBalancerArn"], lb["DNSName"], sg


def main():
    if "--code-only" in sys.argv:  # re-upload the Lambda code after editing lambdas/
        for key, module in FUNCS.items():
            client("lambda").update_function_code(FunctionName=f"{PREFIX}-{key}", ZipFile=zip_of(module))
            say(f"  updated  {PREFIX}-{key}")
        return
    acct = account_id()
    save_state(account=acct, region=REGION)
    lam = client("lambda")
    head("Lambda functions (the three services)")
    arns = functions(lam, lambda_role(client("iam")))
    head("Front door 1: API Gateway REST API")
    api = rest_api(client("apigateway"), lam, arns, acct)
    api_url = f"https://{api}.execute-api.{REGION}.amazonaws.com/prod"
    save_state(api_id=api, api_url=api_url, api_arn=f"arn:aws:apigateway:{REGION}::/restapis/{api}/stages/prod", created=int(time.time()))
    head("Front door 2: Application Load Balancer")
    lb_arn, dns, sg = alb(client("ec2"), client("elbv2"), lam, arns)
    save_state(alb_arn=lb_arn, alb_url=f"http://{dns}", alb_sg=sg)
    head("Deployed")
    say(f"  REST API  {api_url}\n  ALB       http://{dns}")
    say("  paths     POST /mcp   POST /a2a   GET /.well-known/agent-card.json   POST /v1/chat/completions")


if __name__ == "__main__":
    main()
