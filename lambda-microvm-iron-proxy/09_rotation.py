"""Test 8: rotating and revoking under a running MicroVM, using the fake demo secret so the swapped value is
visible in httpbin's echo. Rotation (ttl 30s), revocation by denying the proxy access to the secret (the
cached value keeps being served), a forced proxy restart, and rotation across suspend/resume.
Restarts the proxy once."""

import json
import secrets
import time

from common import banner, check, mvm, redeploy_proxy, run, sm, start_log, summary, tf

start_log("09_rotation")
t = tf()
arn = t["secret_arns"]["demo"]
task_role = t["task_role_arn"]
vm = run("09-rotation", idle=None)


def seen() -> str:
    out = vm.sh("curl -s -m 20 https://httpbin.org/anything -H \"X-Token: $DEMO_TOKEN\"", timeout=40)
    try:
        return json.loads(out)["headers"].get("X-Token")
    except Exception:
        return out[:120]


def watch(until_value: str | None, limit: float = 120) -> str:
    start = time.time()
    v = None
    while time.time() - start < limit:
        v = seen()
        print(f"  t+{time.time() - start:5.1f}s  httpbin got {v}")
        if until_value is None or v == until_value:
            return v
        time.sleep(5)
    return v


banner("Baseline")
print("now:", seen())

banner("Rotate the secret value (ttl is 30 s)")
new = f"demo-ROTATED-{secrets.token_hex(4)}"
sm.put_secret_value(SecretId=arn, SecretString=new)
print("put_secret_value at", time.strftime("%H:%M:%S"), "->", new)
check("rotation picked up without touching the sandbox", watch(new) == new)

banner("Revoke: deny the proxy's role access to the secret")
policy = {"Version": "2012-10-17", "Statement": [{"Sid": "RevokeProxy", "Effect": "Deny", "Principal": {"AWS": task_role},
                                                  "Action": "secretsmanager:GetSecretValue", "Resource": "*"}]}
newer = f"demo-AFTER-REVOKE-{secrets.token_hex(4)}"
sm.put_resource_policy(SecretId=arn, ResourcePolicy=json.dumps(policy))
try:
    sm.put_secret_value(SecretId=arn, SecretString=newer)
    print("resource policy denies the task role; secret changed to", newer, "- sampling for 90 s")
    start, during = time.time(), []
    for _ in range(4):
        during.append(seen())
        print(f"  t+{time.time() - start:5.1f}s  httpbin got {during[-1]}")
        time.sleep(30)
    check("after revoking, the cached value keeps being injected", all(v == new for v in during), str(set(during)))

    banner("Force a new proxy task while access is denied")
    print(f"redeploy took {redeploy_proxy():.0f}s")
    v = seen()
    print("after restart, httpbin got:", v, "(the placeholder, if the proxy forwards it unswapped)")
    check("after a restart with no access, the proxy fails open (forwards the placeholder)", v == t["placeholders"]["demo"], v)
    print("status for a GitHub call meanwhile:", vm.sh("gh api user --jq .login 2>&1 | tail -1"))
finally:
    banner("Restore access")
    sm.delete_resource_policy(SecretId=arn)
check("access restored: the new value is injected", watch(newer, limit=180) == newer)

banner("Rotate while the MicroVM is suspended")
mvm.suspend_microvm(microvmIdentifier=vm.id)
vm.wait("SUSPENDED")
latest = f"demo-WHILE-SUSPENDED-{secrets.token_hex(4)}"
sm.put_secret_value(SecretId=arn, SecretString=latest)
time.sleep(40)
mvm.resume_microvm(microvmIdentifier=vm.id)
vm.wait("RUNNING")
v = seen()
print("after resume, httpbin got:", v)
check("a MicroVM suspended during a rotation uses the new value on resume", v == latest, v)
vm.terminate()
summary()
