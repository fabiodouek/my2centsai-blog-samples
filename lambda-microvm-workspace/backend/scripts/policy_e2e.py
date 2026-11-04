"""End-to-end check of the workspace on part 2's shared iron-proxy (two VPCs, transparent, control_plane=true).

An admin (ADMIN_EMAIL, password in ADMIN_PASSWORD_FILE) works through the API exactly as the browser does. Two
sandboxes, A and B, both on part 2's connector. Inside them, the in-VM agent's /exec runs dig, curl, gh, git, the AWS
CLI and Claude Code; each policy change is timed from the API call to the first request in the sandbox that shows it.

  1. transparent: no proxy variables; every name resolves (CNAME proxy.sandbox.internal) to iron-proxy's NLB; curl,
     gh, git, the AWS CLI and Claude Code work; a raw IP is unreachable
  2. allowlist: example.com added -> allowed in A and in B (the proxy is shared); removed
  3. credentials: a custom credential (made-up value) -> the real value is swapped in; demo revoked by reload ->
     the placeholder goes out unswapped, for A and B; the deny statement names the proxy's identities; restored
  4. AWS: read-only with no session policy -> session my2cents-ip-read-only/proxy-read-only-vN; a session policy
     (sts + s3 listing) -> ecs refused; put back
  5. feeds: /ws/events delivers both sandboxes' proxy lines and the sandbox VPC's DNS lines (to every user: shared)
  6. suspend and resume: timings through the API, and the sandbox works after resume

Policy changes reach every sandbox on the proxy, so take aws.lock (no one else's MicroVMs running) before running it;
everything is put back at the end (allowlist, scope, the test credential deleted, sandboxes terminated, the
throwaway user deleted). Values used for the test credential are made up; no real secret is printed.

  cd backend && ADMIN_EMAIL=you@example.com ADMIN_PASSWORD_FILE=~/pw.txt uv run python scripts/policy_e2e.py

Output (account ID, bucket names and the NAT address redacted) -> ../results/policy_e2e.log and .json."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

import boto3
import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
P2_INFRA = Path(os.environ.get("PART2_INFRA", Path.home() / "Code/lambda-microvm-iron-proxy/lambda-microvm-iron-proxy/infra"))


def outputs(d: Path) -> dict:
    return {k: v["value"] for k, v in json.loads(subprocess.run(["terraform", f"-chdir={d}", "output", "-json"],
                                                                capture_output=True, text=True, check=True).stdout).items()}


OUT, P2 = outputs(ROOT.parent / "infra"), outputs(P2_INFRA)
REGION = OUT.get("region", "us-east-1")
BASE = OUT["url"].rstrip("/")
ACCOUNT = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
BUCKETS = [b["Name"] for b in boto3.client("s3", region_name=REGION).list_buckets()["Buckets"]]
idp = boto3.client("cognito-idp", region_name=REGION)
mvm = boto3.client("lambda-microvms", region_name=REGION)
sm = boto3.client("secretsmanager", region_name=REGION)
(ROOT.parent / "results").mkdir(exist_ok=True)
LOG = open(ROOT.parent / "results" / "policy_e2e.log", "w")
T0 = time.perf_counter()
RESULTS: dict = {"timings": {}, "checks": {}, "outputs": {}}
NLB = sorted(P2["nlb_ips"])
NAT = P2.get("nat_public_ip") or "0.0.0.0"
REPO = os.environ.get("GITHUB_REPO", "fabiodouek/my2centsai-sandbox-test")

CRED, ENV = "e2e-httpbin", "E2E_TEST_KEY"
FAKE_VALUE = "e2e-made-up-value-" + secrets.token_hex(4)
CURL = "curl -s -o /dev/null -w '%{http_code}' --max-time 10"


def redact(s) -> str:
    s = str(s).replace(ACCOUNT, "<acct>").replace(NAT, "<nat-ip>")
    for b in sorted(BUCKETS, key=len, reverse=True):
        s = s.replace(b, "<bucket>")
    return s


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
        self.id, self.name = item["id"], item["name"]
        self.endpoint = mvm.get_microvm(microvmIdentifier=self.id)["endpoint"]
        self.tok = mvm.create_microvm_auth_token(microvmIdentifier=self.id, expirationInMinutes=60,
                                                 allowedPorts=[{"port": 8080}])["authToken"]["X-aws-proxy-auth"]
        self.http = httpx.Client(timeout=150)

    def ready(self, timeout: float = 90) -> float:
        t, end = time.time(), time.time() + timeout
        while time.time() < end:
            try:
                if self.http.get(f"https://{self.endpoint}/health", headers={"X-aws-proxy-auth": self.tok}, timeout=10).status_code == 200:
                    return round(time.time() - t, 2)
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        raise TimeoutError(f"{self.name}: agent not answering")

    def sh(self, cmd: str, timeout: int = 60) -> str:
        r = self.http.post(f"https://{self.endpoint}/exec", headers={"X-aws-proxy-auth": self.tok},
                           json={"cmd": cmd, "timeout": timeout})
        r.raise_for_status()
        j = r.json()
        return (j.get("stdout", "") + j.get("stderr", "")).strip()


def wait_until(label: str, probe, ok, t_start: float, timeout: float = 60, every: float = 0.5) -> float | None:
    """Run probe until ok(result); log each distinct result; return seconds from t_start (wall clock)."""
    last = None
    while time.time() - t_start < timeout:
        try:
            v = probe()
        except Exception as e:
            v = f"error: {type(e).__name__}"
        if v != last:
            log(f"    t+{time.time() - t_start:5.1f}s  {label}: {v}")
            last = v
        if ok(v):
            dt = round(time.time() - t_start, 2)
            RESULTS["timings"][label] = dt
            return dt
        time.sleep(every)
    log(f"    gave up after {timeout} s: {label}")
    RESULTS["timings"][label] = None
    return None


def applied(api: httpx.Client, label: str, t_start: float) -> dict:
    while time.time() - t_start < 60:
        st = api.get("/api/policy/status").json()
        if st.get("applied"):
            log(f"    status: applied (write -> reload {st.get('latency_s')} s; seen by the API after "
                f"{time.time() - t_start:.2f} s) config v{st['config']['version']}, scope v{st['scope']['version']}, "
                f"session {st['scope'].get('session_name')}")
            RESULTS["timings"][f"{label} (status latency_s)"] = st.get("latency_s")
            return st
        if st.get("error") and not st.get("pending"):
            log("    status error:", st["error"])
            return st
        time.sleep(0.3)
    log("    status: not applied after 60 s")
    return {}


def create(api: httpx.Client, name: str) -> Sandbox:
    t = time.time()
    r = api.post("/api/sandboxes", json={"name": name, "idle_minutes": 20, "max_hours": 1})
    r.raise_for_status()
    item = r.json()
    api_s = round(time.time() - t, 2)
    sb = Sandbox(item)
    agent = sb.ready()
    RESULTS["timings"][f"create {name}: API"] = api_s
    RESULTS["timings"][f"create {name}: agent answering after the API returned"] = agent
    log(f"  {name}: {item['id']} created in {api_s} s (API), agent answering {agent} s later")
    return sb


async def feed_sample(tok: str, seconds: float) -> list[dict]:
    url = BASE.replace("https://", "wss://") + "/ws/events"
    got = []
    async with websockets.connect(url, open_timeout=20) as ws:
        await ws.send(json.dumps({"type": "auth", "token": tok}))
        await ws.send(json.dumps({"type": "feeds", "on": True}))
        end = time.time() + seconds
        while time.time() < end:
            try:
                m = json.loads(await asyncio.wait_for(ws.recv(), max(0.1, end - time.time())))
            except asyncio.TimeoutError:
                break
            if m.get("type") in ("proxy", "dns"):
                got.append(m)
        await ws.send(json.dumps({"type": "feeds", "on": False}))
    return got


def main() -> int:
    admin_email = os.environ["ADMIN_EMAIL"]
    admin_pw = Path(os.path.expanduser(os.environ["ADMIN_PASSWORD_FILE"])).read_text().strip()
    other = f"e2e-other-{secrets.token_hex(3)}@example.com"
    other_pw = "Aa1!" + secrets.token_urlsafe(18)
    pool = OUT["user_pool_id"]
    admin_tok = token(admin_email, admin_pw)
    api = httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {admin_tok}"}, timeout=90)
    if not api.get("/api/me").json().get("is_admin"):
        log("the admin user isn't in the admins group")
        return 1
    cfg = api.get("/api/config").json()
    pol = api.get("/api/policy").json()
    log(f"workspace: proxy_mode {cfg.get('proxy_mode')}, control plane {pol.get('enabled')}, config v{pol.get('config_version')}")
    scope_before = api.get("/api/policy/aws/scope").json()
    hosts_before = {h["host"] for h in api.get("/api/policy/allowlist").json()["hosts"] if not h.get("managed")}
    made: list[Sandbox] = []
    revoked = False
    try:
        idp.admin_create_user(UserPoolId=pool, Username=other, MessageAction="SUPPRESS",
                              UserAttributes=[{"Name": "email", "Value": other}, {"Name": "email_verified", "Value": "true"}])
        idp.admin_set_user_password(UserPoolId=pool, Username=other, Password=other_pw, Permanent=True)

        log("\n=== 0. two sandboxes on part 2's connector")
        a = create(api, "e2e-a")
        b = create(api, "e2e-b")
        made += [a, b]

        log("\n=== 1. transparent interception, no proxy settings")
        env = a.sh("env | grep -i -E '^(https?|no|all)_proxy=' || echo none")
        check("no proxy variables", env == "none", env)
        RESULTS["outputs"]["resolv.conf"] = a.sh("cat /etc/resolv.conf")
        ans = a.sh("dig +short api.github.com")
        ips = sorted(x for x in ans.split() if re.match(r"^\d+\.\d+\.\d+\.\d+$", x))
        check("every name -> CNAME proxy.sandbox.internal -> iron-proxy's NLB",
              ips == NLB and "proxy.sandbox.internal." in ans, ans.replace("\n", " "))
        rnd = f"e2e-{secrets.token_hex(4)}.example.com"
        ans = a.sh(f"dig +short {rnd}")
        check("a random name too (nothing recurses to the internet)", sorted(x for x in ans.split() if x[0].isdigit()) == NLB,
              ans.replace("\n", " "))
        RESULTS["outputs"]["dig"] = a.sh("dig api.github.com | sed -n '/ANSWER SECTION/,/^$/p'")
        check("curl https://api.github.com/zen", a.sh(f"{CURL} https://api.github.com/zen") == "200")
        code = a.sh("curl -s -o /dev/null -w '%{http_code}' --max-time 5 https://1.1.1.1/ || true")
        check("a raw IP is unreachable", code in ("000", ""), code)
        login = a.sh("gh api user --jq .login")
        check("gh api user (GitHub token swapped in by iron-proxy)", bool(login) and " " not in login and "401" not in login, login)
        head = a.sh(f"git ls-remote https://github.com/{REPO}.git HEAD | cut -c1-7")
        check("git ls-remote through iron-proxy", bool(re.match(r"^[0-9a-f]{7}$", head)), head)
        arn = a.sh("aws sts get-caller-identity --query Arn --output text")
        check("aws sts get-caller-identity (re-signed by iron-proxy)", "assumed-role/my2cents-ip-" in arn and "/proxy-" in arn, arn)
        t = time.time()
        out = a.sh("cd /root/work && timeout 120 claude -p 'Reply with the single word: transparent' --max-turns 1 2>&1 | tail -3", 150)
        RESULTS["timings"]["claude -p"] = round(time.time() - t, 2)
        check("Claude Code answers (claude -p)", "transparent" in out.lower(), out[-120:])

        log("\n=== 2. allowlist (shared: every sandbox)")
        probe = lambda sb: sb.sh(f"{CURL} https://example.com/")  # noqa: E731
        check("example.com refused before (A and B)", probe(a) == "403" and probe(b) == "403")
        t = time.time()
        r = api.post("/api/policy/allowlist", json={"host": "example.com"})
        log(f"  POST /api/policy/allowlist -> {r.status_code}")
        applied(api, "allowlist add", t)
        dt = wait_until("A: example.com allowed", lambda: probe(a), lambda v: v == "200", t)
        check("allowed in A", dt is not None, f"{dt} s")
        check("allowed in B too (one proxy)", probe(b) == "200")
        t = time.time()
        api.delete("/api/policy/allowlist/example.com")
        dt = wait_until("A: example.com refused again", lambda: probe(a), lambda v: v == "403", t)
        check("removed", dt is not None, f"{dt} s")

        log("\n=== 3. credentials")
        t = time.time()
        r = api.post("/api/policy/credentials", json={"name": CRED, "env": ENV, "hosts": ["httpbin.org"],
                                                      "headers": ["X-Test-Key"], "value": FAKE_VALUE})
        log(f"  POST /api/policy/credentials -> {r.status_code}")
        ph = r.json()["credential"]["placeholder"]
        applied(api, "credential create", t)
        hdr = lambda sb: sb.sh(f"curl -s --max-time 10 -H 'X-Test-Key: {ph}' https://httpbin.org/headers | jq -r '.headers[\"X-Test-Key\"]'")  # noqa: E731
        dt = wait_until("A: the made-up value swapped in", lambda: hdr(a), lambda v: v == FAKE_VALUE, t)
        check("custom credential swapped in (A)", dt is not None, f"{dt} s")
        check("and in B", hdr(b) == FAKE_VALUE)

        def demo(sb):   # part 2's demo value is a real secret: only say which kind of value httpbin received
            v = sb.sh("curl -s --max-time 10 \"https://httpbin.org/anything?t=$DEMO_TOKEN\" | jq -r .args.t")
            return "placeholder" if v == "demo-placeholder-token" else ("empty" if v in ("", "null") else "swapped")
        check("demo swapped before", demo(a) == "swapped" and demo(b) == "swapped")
        t = time.time()
        r = api.post("/api/credentials/demo/revoke")
        revoked = True
        log(f"  POST /api/credentials/demo/revoke -> {r.status_code}: method {r.json().get('method')}")
        pol_doc = json.loads(sm.get_resource_policy(SecretId=P2["secret_arns"]["demo"]).get("ResourcePolicy") or "{}")
        deny = next((st for st in pol_doc.get("Statement", []) if st.get("Sid") == "My2centsWorkspaceRevoke"), {})
        principals = deny.get("Principal", {}).get("AWS", [])
        principals = [p.split("/")[-1] for p in (principals if isinstance(principals, list) else [principals])]
        RESULTS["outputs"]["revoke deny principals"] = principals
        check("deny names the task role and the four sandbox roles", len(principals) == 5, ", ".join(principals))
        applied(api, "revoke demo", t)
        dt = wait_until("A: demo placeholder goes out unswapped", lambda: demo(a), lambda v: v == "placeholder", t)
        check("revoked by reload, in seconds", dt is not None and dt < 15, f"{dt} s")
        check("B too", demo(b) == "placeholder")
        t = time.time()
        api.post("/api/credentials/demo/restore")
        revoked = False
        dt = wait_until("A: demo real value again", lambda: demo(a), lambda v: v == "swapped", t)
        check("restored", dt is not None, f"{dt} s")

        log("\n=== 4. AWS access")
        arn = lambda sb: sb.sh("aws sts get-caller-identity --query Arn --output text")  # noqa: E731
        t = time.time()
        r = api.put("/api/policy/aws/scope", json={"role": "read-only", "session_policy": False, "actions": []})
        log(f"  PUT /api/policy/aws/scope (read-only, no session policy) -> {r.status_code}")
        applied(api, "role read-only", t)
        dt = wait_until("A: signed as read-only", lambda: arn(a), lambda v: "my2cents-ip-read-only/proxy-read-only-v" in v, t)
        check("session my2cents-ip-read-only/proxy-read-only-vN", dt is not None, arn(a))
        t = time.time()
        r = api.put("/api/policy/aws/scope", json={"role": "read-only", "session_policy": True,
                                                   "actions": ["sts:GetCallerIdentity", "s3:ListAllMyBuckets"]})
        log(f"  PUT /api/policy/aws/scope (session policy sts + s3 listing) -> {r.status_code}, "
            f"{r.json().get('preview', {}).get('chars')} chars")
        st = applied(api, "session policy", t)
        RESULTS["outputs"]["session token"] = {k: (st.get("scope") or {}).get(k) for k in
                                               ("session_name", "session_token_utilization", "session_token_size")}
        wait_until("A: s3 listing allowed", lambda: a.sh("aws s3api list-buckets --query 'length(Buckets)' --output text"),
                   lambda v: v.isdigit(), t)
        ecs = a.sh("aws ecs list-clusters 2>&1 | head -c 300")
        check("ecs refused (outside the session policy)", "AccessDenied" in ecs or "403" in ecs, ecs)

        log("\n=== 5. feeds (shared: every sandbox, every user)")
        loop = asyncio.new_event_loop()
        task = loop.create_task(feed_sample(admin_tok, 50))
        other_task = loop.create_task(feed_sample(token(other, other_pw), 50))

        async def traffic():
            await asyncio.sleep(10)
            await asyncio.to_thread(a.sh, f"{CURL} https://httpbin.org/anything/e2e-from-a; getent hosts e2e-a-only.example")
            await asyncio.to_thread(b.sh, f"{CURL} https://httpbin.org/anything/e2e-from-b; getent hosts e2e-b-only.example")
        loop.run_until_complete(asyncio.gather(task, other_task, traffic()))
        evs, other_evs = task.result(), other_task.result()
        paths = {e.get("path") for e in evs if e.get("type") == "proxy"}
        names = {e.get("query") for e in evs if e.get("type") == "dns"}
        check("proxy lines from both sandboxes", {"/anything/e2e-from-a", "/anything/e2e-from-b"} <= paths, f"{len(evs)} events")
        check("DNS lines from the sandbox VPC", {"e2e-a-only.example.", "e2e-b-only.example."} <= names,
              f"{len([e for e in evs if e.get('type') == 'dns'])} dns events")
        check("a second user sees the same feed (one proxy)", any(e.get("path") == "/anything/e2e-from-a" for e in other_evs),
              f"{len(other_evs)} events")
        RESULTS["outputs"]["dns sample"] = [{k: e.get(k) for k in ("query", "qtype", "rcode", "srcaddr")}
                                            for e in evs if e.get("type") == "dns"][:8]

        log("\n=== 6. suspend and resume")
        for verb, want in (("suspend", "SUSPENDED"), ("resume", "RUNNING")):
            t = time.time()
            r = api.post(f"/api/sandboxes/{a.id}/{verb}")
            RESULTS["timings"][f"{verb} (API returns {r.json().get('state')})"] = round(time.time() - t, 2)
            check(f"{verb} -> {want}", r.json().get("state") == want, f"{r.json().get('state')} in {time.time() - t:.2f} s")
        t = time.time()
        a.ready()
        check("works after resume", a.sh(f"{CURL} https://api.github.com/zen") == "200", f"agent back {time.time() - t:.2f} s")
    finally:
        log("\n=== cleanup")
        for sb in made:
            try:
                log(f"  terminate {sb.name}:", api.delete(f"/api/sandboxes/{sb.id}").status_code)
            except Exception as e:
                log(f"  terminate {sb.name}: {e}")
        try:
            r = api.put("/api/policy/aws/scope", json={"role": scope_before["role"], "session_policy": scope_before["session_policy"],
                                                       "actions": scope_before["actions"]})
            log("  scope put back:", r.status_code)
            for h in {h["host"] for h in api.get("/api/policy/allowlist").json()["hosts"] if not h.get("managed")} - hosts_before:
                log(f"  allowlist {h}:", api.delete(f"/api/policy/allowlist/{h}").status_code)
            if revoked:
                log("  restore demo:", api.post("/api/credentials/demo/restore").status_code)
            log("  delete credential:", api.delete(f"/api/policy/credentials/{CRED}").status_code)
        except Exception as e:
            log("  policy cleanup:", e)
        try:
            idp.admin_delete_user(UserPoolId=pool, Username=other)
        except Exception:
            pass
        (ROOT.parent / "results" / "policy_e2e.json").write_text(redact(json.dumps(RESULTS, indent=1)))
    failed = [k for k, v in RESULTS["checks"].items() if not v["ok"]]
    log(f"\n{len(RESULTS['checks']) - len(failed)} passed, {len(failed)} failed" + (f": {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
