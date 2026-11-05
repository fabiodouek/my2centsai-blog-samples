"""The four sandbox roles, the Secrets Manager guard and builder's guardrails, from inside one sandbox on the
deployed workspace (part 2's shared iron-proxy, control_plane=true).

As the admin (ADMIN_EMAIL, password in ADMIN_PASSWORD_FILE), the proxy is switched through the four roles with no
session policy (each role's full permissions). The proxy is shared: this changes AWS access for every sandbox on it,
so run it with aws.lock held (no one else's MicroVMs running) and it puts the scope back as it found it. In each, the AWS CLI inside the sandbox (through its own iron-proxy,
re-signed as the role) runs the same probes; the expected answer per role is checked:

  view-only    lists buckets; can't read an object or a DynamoDB item
  read-only    reads the demo object and the demo table; ssm:GetParameter is denied (the guard on every role)
  data-reader  reads the demo bucket and my2cents-demo-items (3 items); can't list buckets
  builder      creates and deletes an SQS queue and a bucket; refused: a security group in the sandbox VPC, a record in
               the sandbox.internal zone, the DNS Firewall association, writing to the control bucket, an S3 bucket
               in another Region through the us-east-1 endpoint, iron-proxy's ECS service, the stack's log groups,
               sts:AssumeRole, and another Region (by iron-proxy, before IAM)

The Secrets Manager guard, as builder (the widest role): the Secrets Manager endpoint is refused; a request signed
for secretsmanager but sent to an allowed host is refused by aws_auth; a secretsmanager target signed as sts reaches
STS, which doesn't know it; a Secrets Manager Host header on an allowed name is refused. No secret value is printed:
a response that held one would be reported as LEAK.

Take aws.lock (it runs one MicroVM and changes the shared proxy's role). Everything is put back: the queue and bucket
deleted, the sandbox terminated, the scope restored.

  cd backend && ADMIN_EMAIL=you@example.com ADMIN_PASSWORD_FILE=~/pw.txt uv run python scripts/guards_e2e.py

Output (account ID and bucket names redacted) -> ../results/guards_e2e.log and ../results/guards_e2e.json."""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import boto3
import httpx

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT.parent / "infra"
P2_INFRA = Path(os.environ.get("PART2_INFRA", Path.home() / "Code/lambda-microvm-iron-proxy/lambda-microvm-iron-proxy/infra"))


def outputs(d: Path) -> dict:
    return {k: v["value"] for k, v in json.loads(subprocess.run(["terraform", f"-chdir={d}", "output", "-json"],
                                                                capture_output=True, text=True, check=True).stdout).items()}


OUT, P2 = outputs(INFRA), outputs(P2_INFRA)
REGION = OUT.get("region", "us-east-1")
APPLY_TIMEOUT_S = 3 * float(P2.get("control_poll_s") or 30)
BASE = OUT["url"].rstrip("/")
ACCOUNT = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
BUCKETS = [b["Name"] for b in boto3.client("s3", region_name=REGION).list_buckets()["Buckets"]]
idp = boto3.client("cognito-idp", region_name=REGION)
mvm = boto3.client("lambda-microvms", region_name=REGION)
(ROOT.parent / "results").mkdir(exist_ok=True)
LOG = open(ROOT.parent / "results" / "guards_e2e.log", "w")
T0 = time.perf_counter()
RESULTS: dict = {"checks": {}, "timings": {}}
DEMO_BUCKET = P2["demo_bucket"]
DEMO_SECRET = P2["secret_arns"]["demo"]


def redact(s) -> str:
    s = str(s).replace(ACCOUNT, "<acct>")
    for b in sorted(BUCKETS + [P2["control_bucket"], DEMO_BUCKET], key=len, reverse=True):
        s = s.replace(b, "<bucket>")
    return s.replace(DEMO_SECRET, "<demo secret>")


def log(*a) -> None:
    line = f"[{time.strftime('%H:%M:%S', time.gmtime())}Z {time.perf_counter() - T0:7.1f}s] " + redact(" ".join(str(x) for x in a))
    print(line, flush=True)
    LOG.write(line + "\n")
    LOG.flush()


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS["checks"][name] = {"ok": bool(ok), "detail": redact(detail)[:400]}
    log(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({redact(detail)[:300]})" if detail else ""))
    return ok


def token(email: str, password: str) -> str:
    return idp.initiate_auth(ClientId=OUT["client_id"], AuthFlow="USER_PASSWORD_AUTH",
                             AuthParameters={"USERNAME": email, "PASSWORD": password})["AuthenticationResult"]["IdToken"]


class Sandbox:
    def __init__(self, item: dict):
        self.id = item["id"]
        self.endpoint = mvm.get_microvm(microvmIdentifier=self.id)["endpoint"]
        self.tok = mvm.create_microvm_auth_token(microvmIdentifier=self.id, expirationInMinutes=60,
                                                 allowedPorts=[{"port": 8080}])["authToken"]["X-aws-proxy-auth"]
        self.http = httpx.Client(timeout=120)

    def ready(self) -> None:
        for _ in range(180):
            try:
                if self.http.get(f"https://{self.endpoint}/health", headers={"X-aws-proxy-auth": self.tok}, timeout=10).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise TimeoutError("agent not answering")

    def sh(self, cmd: str, timeout: int = 60) -> tuple[int, str]:
        r = self.http.post(f"https://{self.endpoint}/exec", headers={"X-aws-proxy-auth": self.tok},
                           json={"cmd": cmd, "timeout": timeout})
        r.raise_for_status()
        j = r.json()
        out = (j.get("stdout", "") + j.get("stderr", "")).strip()
        if "SecretString" in out:
            out = "LEAK: a SecretString came back (not printed)"
        return j.get("rc", -1), out


def err_code(out: str) -> str:
    """The AWS CLI's error code, or the start of the output."""
    if "LEAK" in out:
        return out
    for marker in ("(AccessDenied", "(AccessDeniedException", "(UnauthorizedOperation", "(InvalidAction",
                   "(ValidationError", "(UnknownOperation"):
        if marker in out:
            return marker.strip("(")
    return out.replace("\n", " ")[:160]


DENIED = ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation", "not authorized", "explicit deny")


def denied(out: str) -> bool:
    return any(d in out for d in DENIED)


def refused_by_proxy(out: str) -> bool:
    """iron-proxy's 403 (allowlist, aws_auth) reaches the CLI as a 403 it can't parse, or a connection error."""
    return ("403" in out or "Forbidden" in out or "invalid XML" in out.lower() or "Could not connect" in out
            or "Unable to parse" in out) and not denied(out)


FORGE = r'''
import json, sys, urllib.request, urllib.error
import botocore.auth, botocore.awsrequest, botocore.credentials
creds = botocore.credentials.Credentials("AKIAPLACEHOLDER00000", "placeholder-secret-not-real")
def send(url, service, target, host=None):
    body = json.dumps({"SecretId": sys.argv[1]})
    h = {"Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": target}
    if host:
        h["Host"] = host
    req = botocore.awsrequest.AWSRequest(method="POST", url=url, data=body, headers=h)
    botocore.auth.SigV4Auth(creds, service, "us-east-1").add_auth(req)
    p = req.prepare()
    r = urllib.request.Request(p.url, data=p.body.encode() if isinstance(p.body, str) else p.body, headers=dict(p.headers), method="POST")
    try:
        resp = urllib.request.urlopen(r, timeout=15)
        code, text = resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        code, text = e.code, e.read().decode(errors="replace")
    except Exception as e:
        code, text = 0, type(e).__name__ + ": " + str(e)
    leak = "SecretString" in text
    print(json.dumps({"code": code, "leak": leak, "text": "" if leak else text[:200]}))
case = sys.argv[2]
if case == "endpoint":
    send("https://secretsmanager.us-east-1.amazonaws.com/", "secretsmanager", "secretsmanager.GetSecretValue")
elif case == "s3-scope":
    send("https://secretsmanager.us-east-1.amazonaws.com/", "s3", "secretsmanager.GetSecretValue")
elif case == "forged-scope":
    send("https://sts.us-east-1.amazonaws.com/", "secretsmanager", "secretsmanager.GetSecretValue")
elif case == "sts-scope":
    send("https://sts.us-east-1.amazonaws.com/", "sts", "secretsmanager.GetSecretValue")
elif case == "host-header":
    send("https://sts.us-east-1.amazonaws.com/", "secretsmanager", "secretsmanager.GetSecretValue",
         host="secretsmanager.us-east-1.amazonaws.com")
'''


def main() -> int:
    admin_pw = Path(os.path.expanduser(os.environ["ADMIN_PASSWORD_FILE"])).read_text().strip()
    api = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {token(os.environ['ADMIN_EMAIL'], admin_pw)}"}, timeout=90)
    queue = f"e2e-builder-{secrets.token_hex(3)}"
    loc_bucket = f"e2e-loc-{secrets.token_hex(4)}"
    vm = None
    before = api.get("/api/policy/aws/scope").json()
    try:
        r = api.post("/api/sandboxes", json={"name": "e2e-guards", "idle_minutes": 20, "max_hours": 1})
        r.raise_for_status()
        item = r.json()
        vm = Sandbox(item)
        vm.ready()
        B = "/api/policy"
        log(f"sandbox {vm.id}")
        vm.sh(f"echo {base64.b64encode(FORGE.encode()).decode()} | base64 -d > /tmp/forge.py")

        def role(name: str) -> None:
            t = time.time()
            r = api.put(f"{B}/aws/scope", json={"role": name, "session_policy": False, "actions": []})
            r.raise_for_status()
            # part 2's sidecar checks the bucket every 30 s (control_poll_s): allow three of its intervals
            while time.time() - t < APPLY_TIMEOUT_S:
                st = api.get(f"{B}/status").json()
                if st.get("applied"):
                    break
                time.sleep(1.0)
            # wait until the sandbox's calls are signed as the new role
            for _ in range(60):
                rc, arn = vm.sh("aws sts get-caller-identity --query Arn --output text")
                if f"my2cents-ip-{name}/" in arn:
                    break
                time.sleep(0.5)
            RESULTS["timings"][f"role {name} (PUT -> sandbox signed as it)"] = round(time.time() - t, 2)
            log(f"\n=== {name}: {arn} ({time.time() - t:.1f} s after the PUT)")
            check(f"{name}: session my2cents-ip-{name}/proxy-{name}-v*", f"my2cents-ip-{name}/proxy-{name}-v" in arn, arn)

        obj = f"aws s3api get-object --bucket {DEMO_BUCKET} --key reports/q3-summary.txt /tmp/o.txt --query ContentLength --output text"
        scan = "aws dynamodb scan --table-name my2cents-demo-items --query Count --output text"
        lst = "aws s3api list-buckets --query 'length(Buckets)' --output text"
        ssm = "aws ssm get-parameter --name /my2cents-ws/origin-secret --with-decryption --query Parameter.Name --output text"

        role("view-only")
        rc, out = vm.sh(lst)
        check("view-only: lists buckets", rc == 0 and out.isdigit(), out)
        rc, out = vm.sh(obj)
        check("view-only: can't read an object", rc != 0 and denied(out), err_code(out))
        rc, out = vm.sh(scan)
        check("view-only: can't scan a table", rc != 0 and denied(out), err_code(out))

        role("read-only")
        rc, out = vm.sh(obj)
        check("read-only: reads the demo object", rc == 0 and out.strip().isdigit(), out)
        rc, out = vm.sh(scan)
        check("read-only: scans my2cents-demo-items", rc == 0 and out.strip() == "3", out)
        rc, out = vm.sh(ssm)
        check("read-only: ssm:GetParameter denied (every role's guard)", rc != 0 and denied(out), err_code(out))

        role("data-reader")
        rc, out = vm.sh(obj)
        check("data-reader: reads the demo object", rc == 0 and out.strip().isdigit(), out)
        rc, out = vm.sh(scan)
        check("data-reader: scans my2cents-demo-items (3 items)", rc == 0 and out.strip() == "3", out)
        rc, out = vm.sh(lst)
        check("data-reader: can't list buckets", rc != 0 and denied(out), err_code(out))

        role("builder")
        rc, out = vm.sh(f"aws sqs create-queue --queue-name {queue} --query QueueUrl --output text")
        ok = check("builder: creates an SQS queue", rc == 0 and queue in out, out)
        if ok:
            rc, out = vm.sh(f"aws sqs delete-queue --queue-url {out.strip()} && echo deleted")
            check("builder: deletes it", rc == 0 and "deleted" in out, out)
        sandbox_vpc = P2["sandbox_vpc_id"]
        zone = P2["sandbox_zone_id"]
        assoc = P2["firewall_association_id"]
        probes = [
            ("a security group in the sandbox VPC",
             f"aws ec2 create-security-group --group-name e2e-x --description x --vpc-id {sandbox_vpc}"),
            ("a record in the sandbox.internal zone",
             f"aws route53 change-resource-record-sets --hosted-zone-id {zone} --change-batch "
             "'{\"Changes\":[{\"Action\":\"UPSERT\",\"ResourceRecordSet\":{\"Name\":\"e2e.sandbox.internal\",\"Type\":\"TXT\",\"TTL\":60,\"ResourceRecords\":[{\"Value\":\"\\\"x\\\"\"}]}}]}'"),
            ("the DNS Firewall association",
             f"aws route53resolver update-firewall-rule-group-association --firewall-rule-group-association-id {assoc} --mutation-protection DISABLED"),
            # Body-less writes: they reach IAM whatever happens to uploads through the proxy (probed below)
            ("deleting from the control bucket",
             f"aws s3api delete-object --bucket {P2['control_bucket']} --key e2e-guard-nonexistent.txt"),
            ("uploading into the control bucket",
             f"echo x > /tmp/x && aws s3api put-object --bucket {P2['control_bucket']} --key e2e-guard.txt --body /tmp/x"),
            ("an S3 bucket in eu-west-1 through the us-east-1 endpoint",
             f"aws s3api create-bucket --bucket {loc_bucket} --create-bucket-configuration LocationConstraint=eu-west-1"),
            ("part 2's NLB",
             f"aws elbv2 modify-load-balancer-attributes --load-balancer-arn {P2['nlb_arn']} "
             "--attributes Key=deletion_protection.enabled,Value=false"),
            ("iron-proxy's ECS service",
             f"aws ecs update-service --cluster {P2['ecs_cluster']} --service {P2['ecs_service']} --desired-count 1"),
            ("the stack's log groups", f"aws logs put-retention-policy --log-group-name {OUT['api_log_group']} --retention-in-days 7"),
            ("sts:AssumeRole", f"aws sts assume-role --role-arn {P2['sandbox_roles']['read-only']['arn']} --role-session-name e2e"),
        ]
        for label, cmd in probes:
            rc, out = vm.sh(cmd)
            check(f"builder: refused {label}", rc != 0 and denied(out), err_code(out))
        # Uploads through iron-proxy: the AWS CLI's default PutObject (a streaming body with a trailing checksum) and
        # the same with checksums only when required, into a bucket builder makes and removes
        own = f"e2e-builder-{secrets.token_hex(4)}"
        rc, out = vm.sh(f"aws s3api create-bucket --bucket {own} --query Location --output text")
        if check("builder: creates its own bucket", rc == 0, out):
            for mode, envs in [("the sandbox's env (when_required)", ""),
                               ("the SDK default (when_supported: aws-chunked)", "AWS_REQUEST_CHECKSUM_CALCULATION=when_supported ")]:
                rc, out = vm.sh(f"echo hello > /tmp/h && {envs}aws s3api put-object --bucket {own} --key h.txt --body /tmp/h --query ETag --output text")
                RESULTS.setdefault("uploads", {})[mode] = {"rc": rc, "out": redact(out)[:200]}
                log(f"  upload through iron-proxy ({mode}): rc={rc} {err_code(out)}")
            check("builder: uploads work with the sandbox's env",
                  RESULTS["uploads"]["the sandbox's env (when_required)"]["rc"] == 0,
                  RESULTS["uploads"]["the sandbox's env (when_required)"]["out"])
            vm.sh(f"aws s3 rm s3://{own}/h.txt; aws s3api delete-bucket --bucket {own}")
        # Not a guard check: CopyObject doesn't get through iron-proxy 0.55.0's re-signing (S3 answers NotImplemented,
        # "A header you provided implies functionality that is not implemented"), so it never reaches IAM
        rc, out = vm.sh(f"aws s3api copy-object --bucket {P2['control_bucket']} --key e2e-guard.txt "
                        f"--copy-source {P2['control_bucket']}/{P2['control_keys']['scope']}")
        RESULTS["copy_object"] = {"rc": rc, "out": redact(out)[:200]}
        log(f"  copy-object through iron-proxy: rc={rc} {err_code(out)}")
        rc, out = vm.sh("aws sqs list-queues --region us-west-2")
        check("builder: another Region is refused (by iron-proxy, before IAM)", rc != 0 and refused_by_proxy(out), err_code(out))

        log("\n=== the Secrets Manager guard (as builder)")
        rc, out = vm.sh(f"aws secretsmanager get-secret-value --secret-id {DEMO_SECRET} --query Name --output text")
        check("the CLI to Secrets Manager is refused", rc != 0 and "LEAK" not in out and not out.startswith("my2cents"), err_code(out))
        for case, label in [("endpoint", "the Secrets Manager endpoint"),
                            ("s3-scope", "the Secrets Manager endpoint, signed with an s3 credential scope"),
                            ("forged-scope", "a secretsmanager-signed request sent to an allowed host (sts)"),
                            ("sts-scope", "a secretsmanager target signed as sts"),
                            ("host-header", "a Secrets Manager Host header on an allowed name")]:
            rc, out = vm.sh(f"python3 /tmp/forge.py {DEMO_SECRET} {case}")
            try:
                res = json.loads(out.splitlines()[-1])
            except (ValueError, IndexError):
                res = {"code": None, "leak": "LEAK" in out, "text": out[:200]}
            RESULTS.setdefault("forged", {})[case] = {k: redact(v) for k, v in res.items()}
            check(f"secrets guard: {label} -> no secret", not res.get("leak") and res.get("code") != 200,
                  f"HTTP {res.get('code')} {str(res.get('text'))[:160]}")
    finally:
        r = api.put("/api/policy/aws/scope", json={"role": before["role"], "session_policy": before["session_policy"],
                                                   "actions": before["actions"]})
        log(f"\ncleanup: scope put back ({before['role']}, session policy {before['session_policy']}): {r.status_code}")
        if vm:
            log("cleanup: terminate", api.delete(f"/api/sandboxes/{vm.id}").status_code)
        s3 = boto3.client("s3", region_name=REGION)
        for bkt, key in [(P2["control_bucket"], "e2e-guard.txt")]:
            try:
                s3.head_object(Bucket=bkt, Key=key)
                s3.delete_object(Bucket=bkt, Key=key)
                log("cleanup: removed an object the guard should have refused:", key)
            except Exception:
                pass
        try:
            boto3.client("s3", region_name="eu-west-1").delete_bucket(Bucket=loc_bucket)
            log("cleanup: removed a bucket the guard should have refused")
        except Exception:
            pass
        (ROOT.parent / "results" / "guards_e2e.json").write_text(redact(json.dumps(RESULTS, indent=1)))
    failed = [k for k, v in RESULTS["checks"].items() if not v["ok"]]
    log(f"\n{len(RESULTS['checks']) - len(failed)} passed, {len(failed)} failed" + (f": {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
