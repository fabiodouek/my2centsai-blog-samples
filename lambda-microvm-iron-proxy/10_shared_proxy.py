"""Test 9: two MicroVMs, one proxy. MicroVM B is started with no placeholders at all. The placeholder strings
aren't secret (they're in the Terraform output and this repo), so B can type one in and get the same GitHub
access as A. And the audit log has no field that tells A's requests from B's: every connection arrives from
the NLB's own addresses."""

import time

from common import audit_line, banner, check, is_request, proxy_audit, run, start_log, summary, tf

start_log("10_shared_proxy")
t = tf()
since = int(time.time() * 1000)
a = run("10-a")
b = run("10-b", placeholders=False)

banner("A: has the placeholder")
login_a = a.sh("gh api user --jq .login 2>&1 | tail -1")
print("A env GH_TOKEN:", a.sh("echo ${GH_TOKEN:-unset}"), "| gh api user:", login_a)

banner("B: started with no placeholders")
print("B env GH_TOKEN:", b.sh("echo ${GH_TOKEN:-unset}"))
print("B gh api user, no token:", b.sh("gh api user --jq .login 2>&1 | tail -1"))
gh = t["placeholders"]["github"]
login_b = b.sh(f"GH_TOKEN={gh} gh api user --jq .login 2>&1 | tail -1")
print("B with the placeholder typed in:", login_b)
print("B curl with the placeholder:", b.sh(f"curl -s https://api.github.com/user -H 'Authorization: token {gh}' | grep -o '\"login\": \"[^\"]*'"))
check("B gets the same GitHub identity as A by typing the placeholder", login_b == login_a and " " not in login_a, f"A={login_a} B={login_b}")

banner("The audit log: can you tell A from B?")
recs = [r for r in proxy_audit(since, "api.github.com") if is_request(r)]
for line in recs:
    print(audit_line(line))
print("audit fields:", sorted({k for r in recs for k in r["audit"]}))
addrs = sorted({r["audit"].get("remote_addr", "?").rsplit(":", 1)[0] for r in recs})
print("client addresses seen:", addrs)
check("every request comes from an NLB address (no per-MicroVM attribution)", addrs and set(addrs) <= set(t["nlb_ips"]), str(addrs))
a.terminate()
b.terminate()
summary()
