"""Integration run against real AWS: the API under uvicorn on this machine (DEV_AUTH=1), one real MicroVM.

  cd backend && uv run python scripts/integration.py

Creates a sandbox through the API, bridges a terminal, reads vitals, watches /ws/events for iron-proxy
traffic the sandbox makes, suspends/resumes (the terminal WS must report it and reattach), reconnects,
checks whether opening the shell on a SUSPENDED MicroVM resumes it on its own, then terminates.
At most one MicroVM; it is always terminated. Output (account ID redacted) goes to
../results/backend_integration.log.

Settings come from part 2's Terraform outputs and part 3's (the DynamoDB table). IMAGE_ARN defaults to the
workspace image. Take aws.lock first (it runs one MicroVM)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import boto3
import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
WS_ROOT = ROOT.parent
P2_INFRA = Path(os.environ.get("PART2_INFRA", Path.home() / "Code/lambda-microvm-iron-proxy/lambda-microvm-iron-proxy/infra"))
LOG_PATH = Path(os.environ.get("INTEGRATION_LOG", WS_ROOT / "results" / "backend_integration.log"))
SERVER_LOG = LOG_PATH.with_name(LOG_PATH.stem + "_server.log")
PORT = int(os.environ.get("PORT", "8077"))
BASE = f"http://127.0.0.1:{PORT}"
WSBASE = f"ws://127.0.0.1:{PORT}"
REGION = os.environ.get("AWS_REGION", "us-east-1")

ACCOUNT = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
mvm = boto3.client("lambda-microvms", region_name=REGION)
T0 = time.perf_counter()
_log = open(LOG_PATH, "w")


def redact(s: str) -> str:
    return s.replace(ACCOUNT, "<acct>")


def log(*parts) -> None:
    line = redact(" ".join(str(p) for p in parts))
    stamp = f"[{time.perf_counter() - T0:7.2f}s] "
    print(stamp + line, flush=True)
    _log.write(stamp + line + "\n")
    _log.flush()


def banner(t: str) -> None:
    log(f"\n=== {t} " + "=" * max(0, 64 - len(t)))


def tf_outputs(chdir: Path) -> dict:
    out = subprocess.run(["terraform", f"-chdir={chdir}", "output", "-json"], capture_output=True, text=True, check=True).stdout
    return {k: v["value"] for k, v in json.loads(out).items()}


def short(d, n=400) -> str:
    return json.dumps(d, default=str)[:n]


# ---------------------------------------------------------------- server

def server_env() -> dict:
    p2 = tf_outputs(P2_INFRA)
    try:
        p3 = tf_outputs(WS_ROOT / "infra")
    except Exception:
        p3 = {}
    env = {k: v for k, v in os.environ.items() if k not in ("ORIGIN_SECRET",)}
    env.update(
        DEV_AUTH="1", AWS_REGION=REGION,
        IMAGE_ARN=os.environ.get("IMAGE_ARN") or f"arn:aws:lambda:{REGION}:{ACCOUNT}:microvm-image:my2cents-ws-sandbox",
        CONNECTOR_ARN=p2["connector_arn"], PLACEHOLDERS_JSON=json.dumps(p2["placeholders"]),
        SECRET_ARNS_JSON=json.dumps(p2["secret_arns"]),
        PROXY_LOG_GROUP_ARN=f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{p2['proxy_log_group']}",
        DNS_LOG_GROUP_ARN=f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:{p2['query_log_group']}",
        SANDBOX_CIDRS=",".join(p2["connector_subnet_cidrs"]), TASK_ROLE_ARN=p2["task_role_arn"],
        TABLE_NAME=os.environ.get("TABLE_NAME", p3.get("table") or ""),
    )
    log("image:", env["IMAGE_ARN"].split(":")[-1], "| table:", env["TABLE_NAME"] or "(in-memory)", "| transparent mode")
    return env


def start_server(env: dict) -> subprocess.Popen:
    f = open(SERVER_LOG, "w")
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1",
                          "--port", str(PORT), "--ws-ping-interval", "20"], cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            if httpx.get(f"{BASE}/healthz", timeout=1).status_code == 200:
                return p
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise SystemExit("server did not start; see " + str(SERVER_LOG))


# ---------------------------------------------------------------- clients

class Term:
    """A browser-like terminal client: auth, resize, binary keystrokes, collects output and statuses."""

    def __init__(self, vm_id: str, label: str):
        self.vm_id, self.label = vm_id, label
        self.out, self.statuses, self.closed = "", [], None
        self.mark = 0

    async def open(self, rows=30, cols=120):
        self.ws = await websockets.connect(f"{WSBASE}/ws/terminal/{self.vm_id}", max_size=None)
        await self.ws.send(json.dumps({"type": "auth", "token": "dev"}))
        await self.ws.send(json.dumps({"type": "resize", "rows": rows, "cols": cols}))
        self.task = asyncio.create_task(self._read())
        return self

    async def _read(self):
        try:
            async for m in self.ws:
                if isinstance(m, bytes):
                    self.out += m.decode(errors="replace")
                    continue
                j = json.loads(m)
                if j.get("type") == "status":
                    self.statuses.append((time.perf_counter(), j))
                    extra = {k: v for k, v in j.items() if k not in ("type", "state", "detail")}
                    log(f"  [{self.label} status] {j['state']:<12} {j.get('detail', '')} {short(extra) if extra else ''}")
        except websockets.ConnectionClosed as e:
            pass
        self.closed = (self.ws.close_code, self.ws.close_reason)
        log(f"  [{self.label}] websocket closed: {self.closed}")

    async def wait_state(self, state: str, after: int = 0, timeout: float = 60) -> float:
        start = time.perf_counter()
        while time.perf_counter() - start < timeout:
            for t, s in self.statuses[after:]:
                if s["state"] == state:
                    return t
            if self.closed:
                break
            await asyncio.sleep(0.05)
        raise TimeoutError(f"{self.label}: no '{state}' status (have {[s['state'] for _, s in self.statuses]})")

    async def run(self, cmd: str, expect: str, timeout: float = 30) -> str:
        self.mark = len(self.out)
        await self.ws.send((cmd + "\r").encode())
        start = time.perf_counter()
        while time.perf_counter() - start < timeout:
            if expect in self.out[self.mark:]:
                return self.out[self.mark:]
            await asyncio.sleep(0.05)
        raise TimeoutError(f"{self.label}: '{expect}' not seen; got {self.out[self.mark:][-500:]!r}")

    async def close(self):
        await self.ws.close()
        await asyncio.wait_for(self.task, 10)


class Events:
    def __init__(self):
        self.items: list[tuple[float, dict]] = []

    async def open(self):
        self.ws = await websockets.connect(f"{WSBASE}/ws/events", max_size=None)
        await self.ws.send(json.dumps({"type": "auth", "token": "dev"}))
        self.task = asyncio.create_task(self._read())
        return self

    async def _read(self):
        try:
            async for m in self.ws:
                j = json.loads(m)
                self.items.append((time.perf_counter(), j))
                if j["type"] == "status":
                    log("  [events] status:", short(j))
                elif j["type"] == "lifecycle":
                    log(f"  [events] lifecycle {j['id']} {j.get('prev')} -> {j['state']}{' (initial)' if j.get('initial') else ''}")
        except websockets.ConnectionClosed:
            pass

    def of(self, kind: str, since: float = 0.0) -> list[dict]:
        return [e for t, e in self.items if e.get("type") == kind and t >= since]

    async def close(self):
        await self.ws.close()
        self.task.cancel()


async def api(c: httpx.AsyncClient, method: str, path: str, quiet=False, **kw):
    t = time.perf_counter()
    r = await c.request(method, path, **kw)
    ms = (time.perf_counter() - t) * 1000
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
    if not quiet:
        log(f"{method} {path} -> {r.status_code} ({ms:.0f} ms) {short(body)}")
    return r.status_code, body


# ---------------------------------------------------------------- the run

async def main() -> int:
    banner("Start the API locally (DEV_AUTH=1, real AWS)")
    env = server_env()
    server = start_server(env)
    vm_id = None
    failures = []
    try:
        async with httpx.AsyncClient(base_url=BASE, headers={"Authorization": "Bearer dev"}, timeout=60) as c:
            await api(c, "GET", "/healthz")
            await api(c, "GET", "/api/me")
            code, cfg = await api(c, "GET", "/api/config", quiet=True)
            log("GET /api/config ->", code, short({k: v for k, v in cfg.items() if k != "pricing"}, 300),
                "| pricing models:", len(cfg["pricing"]["models"]), "| source:", cfg["pricing"]["source"])
            await api(c, "GET", "/api/quota")
            code, creds = await api(c, "GET", "/api/credentials")

            banner("Negative: WebSocket without an auth message closes 4401")
            ws = await websockets.connect(f"{WSBASE}/ws/events")
            t = time.perf_counter()
            try:
                await ws.recv()
            except websockets.ConnectionClosed:
                pass
            log(f"closed with {ws.close_code} '{ws.close_reason}' after {time.perf_counter() - t:.1f}s")

            banner("/ws/events subscriber")
            ev = await Events().open()
            await asyncio.sleep(3)

            banner("Create a sandbox (POST /api/sandboxes)")
            for attempt in range(10):
                t_create = time.perf_counter()
                code, sb = await api(c, "POST", "/api/sandboxes", json={"name": "backend-integration", "idle_minutes": 15, "max_hours": 1})
                if code != 409:
                    break
                log("quota full (other processes use MicroVMs); retrying in 60 s")
                await asyncio.sleep(60)
            assert code == 201, sb
            vm_id = sb["id"]
            while True:
                code, lst = await api(c, "GET", "/api/sandboxes", quiet=True)
                mine = [x for x in lst if x["id"] == vm_id][0]
                if mine["state"] == "RUNNING":
                    break
                if mine["state"] in ("TERMINATING", "TERMINATED"):
                    raise SystemExit(f"sandbox died: {mine.get('state_reason')}")
                await asyncio.sleep(0.25)
            log(f"RUNNING {time.perf_counter() - t_create:.2f}s after POST; list item: {short(mine)}")

            banner("Terminal: attach, run a command")
            term = await (Term(vm_id, "term1")).open()
            t_open = time.perf_counter()
            t_att = await term.wait_state("attached")
            log(f"attached {t_att - t_open:.2f}s after opening the WebSocket")
            await asyncio.sleep(1.0)
            tm = [l for l in term.out.splitlines() if "tmux" in l]
            log("tmux attach line(s) as the browser saw them:", repr(tm[-1][-160:]) if tm else "(none)")
            out = await term.run("echo hello-$((40+2))", "hello-42")
            log("echo hello: saw 'hello-42' in the bridged output")
            code, lst = await api(c, "GET", "/api/sandboxes", quiet=True)
            log("list while attached: attached =", [x["attached"] for x in lst if x["id"] == vm_id][0])

            banner("Vitals")
            for live in (0, 1):
                code, v = await api(c, "GET", f"/api/sandboxes/{vm_id}/vitals?live={live}", quiet=True)
                log(f"GET /api/sandboxes/{{id}}/vitals?live={live} -> {code}",
                    short({k: v.get(k) for k in ("state", "uptime_s", "remaining_s", "expires_at", "idle_policy",
                                                 "attached", "last_activity_at", "stats", "stats_error")}, 700))
            await api(c, "GET", f"/api/sandboxes/{vm_id}/claude-usage")

            banner("Proxy traffic from the sandbox -> /ws/events")
            since = time.perf_counter()
            # Transparent: no proxy settings; every name in part 2's sandbox VPC resolves to iron-proxy's NLB
            prefix, res = ". /etc/sandbox.env; dig +short httpbin.org | tr '\\n' ' '; echo; ", ""
            log("transparent mode: curl straight to the names; DNS Firewall sends them to iron-proxy")
            out = await term.run(
                prefix +
                f"curl -s -m 15 {res}-o /dev/null -w 'get=%{{http_code}}\\n' https://httpbin.org/get; "
                f"curl -s -m 15 {res}-o /dev/null -w 'demo=%{{http_code}}\\n' -H \"Authorization: Bearer $DEMO_TOKEN\" https://httpbin.org/anything; "
                f"curl -s {res}-m 10 -o /dev/null -w 'denied=%{{http_code}}\\n' https://example.com/; echo done-$((2*3))",
                "done-6", timeout=60)
            plain = re.sub(r"\x1b\[[0-9;?>]*[A-Za-z]", "", out)   # tmux redraws carry escape sequences
            for k in ("get", "demo", "denied"):
                m = re.findall(rf"\b{k}=(\d{{3}})", plain)
                log(f"  sandbox: {k}={m[-1] if m else '?'}")
            deadline = time.perf_counter() + 90
            got = []
            while time.perf_counter() < deadline:
                got = [e for e in ev.of("proxy", since) if e.get("host") in ("httpbin.org", "example.com")]
                if any(e["host"] == "httpbin.org" and e.get("path") == "/get" for e in got) and \
                   any(e["host"] == "httpbin.org" and e.get("secrets") for e in got):
                    break
                await asyncio.sleep(0.5)
            for e in got:
                log(f"  [events] proxy {e['ts']} {e['action']:<6} {e['status']} {e['method']} {e['host']}{e['path']} "
                    f"secrets={e['secrets']} rejected_by={e.get('rejected_by')}")
            first = min((t for t, e in ev.items if t >= since and e.get("type") == "proxy" and e.get("host") == "httpbin.org"), default=None)
            if first:
                log(f"first httpbin.org event arrived {first - since:.1f}s after the curl command was sent")
            if not any(e.get("path") == "/get" for e in got):
                failures.append("no proxy event for httpbin.org/get")
            dns = ev.of("dns", since)
            log(f"dns events since the curl: {len(dns)}; sample: {short(dns[:3])}")
            await asyncio.sleep(2)
            code, creds = await api(c, "GET", "/api/credentials")

            banner("Suspend via REST: the terminal WS must report it")
            idx = len(term.statuses)
            t = time.perf_counter()
            await api(c, "POST", f"/api/sandboxes/{vm_id}/suspend")
            t_s = await term.wait_state("suspended", after=idx, timeout=60)
            log(f"terminal reported 'suspended' {t_s - t:.2f}s after the suspend call")
            await asyncio.sleep(4)
            code, v = await api(c, "GET", f"/api/sandboxes/{vm_id}/vitals?live=1", quiet=True)
            log("vitals live=1 while suspended: state", v["state"], "| stats_error:", v["stats_error"])
            code, v = await api(c, "GET", f"/api/sandboxes/{vm_id}/vitals?live=0", quiet=True)
            log("state still", v["state"], "(the live=1 call didn't wake it)")

            banner("Resume via REST: the same terminal WS reattaches by itself")
            idx = len(term.statuses)
            t = time.perf_counter()
            await api(c, "POST", f"/api/sandboxes/{vm_id}/resume")
            t_a = await term.wait_state("attached", after=idx, timeout=60)
            log(f"terminal re-attached {t_a - t:.2f}s after the resume call")
            await term.run("echo again-$((1+1))", "again-2")
            log("echo after resume: saw 'again-2'")

            banner("Reconnect: close the terminal, open a new one")
            await term.close()
            term2 = await Term(vm_id, "term2").open(rows=40, cols=160)
            t_open = time.perf_counter()
            await term2.wait_state("attached")
            log(f"attached {time.perf_counter() - t_open:.2f}s after opening")
            await term2.run("echo third-$((3*1))", "third-3")
            log("echo on the new connection: saw 'third-3'")
            await term2.close()

            banner("Does opening the shell on a SUSPENDED MicroVM resume it? (raw, no resume call)")
            await api(c, "POST", f"/api/sandboxes/{vm_id}/suspend")
            for _ in range(80):
                if mvm.get_microvm(microvmIdentifier=vm_id)["state"] == "SUSPENDED":
                    break
                await asyncio.sleep(0.25)
            log("state:", mvm.get_microvm(microvmIdentifier=vm_id)["state"])
            endpoint = mvm.get_microvm(microvmIdentifier=vm_id)["endpoint"]
            tok = mvm.create_microvm_shell_auth_token(microvmIdentifier=vm_id, expirationInMinutes=5)["authToken"]["X-aws-proxy-auth"]
            t = time.perf_counter()
            try:
                async with websockets.connect(f"wss://{endpoint}/shell", additional_headers={"X-aws-proxy-auth": tok},
                                              open_timeout=20, max_size=None) as raw:
                    first = await asyncio.wait_for(raw.recv(), 15)
                    log(f"raw shell connect on SUSPENDED: handshake ok, first frame after {time.perf_counter() - t:.2f}s: {str(first)[:80]}")
            except Exception as e:
                log(f"raw shell connect on SUSPENDED failed after {time.perf_counter() - t:.2f}s: {type(e).__name__}: {str(e)[:160]}")
            await asyncio.sleep(1)
            log("state after the raw shell attempt:", mvm.get_microvm(microvmIdentifier=vm_id)["state"])
            if mvm.get_microvm(microvmIdentifier=vm_id)["state"] == "RUNNING":
                await api(c, "POST", f"/api/sandboxes/{vm_id}/suspend")
                for _ in range(80):
                    if mvm.get_microvm(microvmIdentifier=vm_id)["state"] == "SUSPENDED":
                        break
                    await asyncio.sleep(0.25)

            banner("Open the terminal through the API on a SUSPENDED sandbox")
            log("state:", mvm.get_microvm(microvmIdentifier=vm_id)["state"])
            term3 = await Term(vm_id, "term3").open()
            t_open = time.perf_counter()
            await term3.wait_state("attached", timeout=60)
            log(f"attached {time.perf_counter() - t_open:.2f}s after opening (backend resumed it)")
            await term3.run("echo fourth-$((2+2))", "fourth-4")
            log("echo: saw 'fourth-4'")

            banner("Shell exit is reported as 'exited'")
            # With tmux the first exit ends the tmux shell and drops back to the outer bash; the second ends that.
            for _ in range(3):
                await term3.ws.send(b"exit\r")
                try:
                    await term3.wait_state("exited", timeout=3)
                    break
                except TimeoutError:
                    pass
            await asyncio.sleep(0.5)
            log("terminal close:", term3.closed)

            banner("Terminate (DELETE /api/sandboxes/{id})")
            since = time.perf_counter()
            await api(c, "DELETE", f"/api/sandboxes/{vm_id}")
            for _ in range(120):
                code, lst = await api(c, "GET", "/api/sandboxes", quiet=True)
                st = [x["state"] for x in lst if x["id"] == vm_id]
                if st and st[0] == "TERMINATED":
                    break
                await asyncio.sleep(1)
            log("list after terminate:", short([{k: x[k] for k in ("id", "state")} for x in lst]))
            vm_id_done = vm_id
            vm_id = None
            await asyncio.sleep(6)
            lc = [f"{e.get('prev')}->{e['state']}" for e in ev.of("lifecycle") if e["id"] == vm_id_done]
            log("lifecycle events seen on /ws/events for this sandbox:", lc)
            await api(c, "GET", "/api/quota")
            await ev.close()

            banner("Summary")
            log("proxy events received in total:", len(ev.of("proxy")), "| dns events:", len(ev.of("dns")),
                "| feed source:", [e.get("source") for e in ev.of("status")])
            log("failures:", failures or "none")
            return 1 if failures else 0
    finally:
        if vm_id:
            try:
                mvm.terminate_microvm(microvmIdentifier=vm_id)
                log("cleanup: terminated", vm_id)
            except Exception as e:
                log("cleanup terminate:", type(e).__name__, str(e)[:120])
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
        if env.get("TABLE_NAME"):
            tbl = boto3.resource("dynamodb", region_name=REGION).Table(env["TABLE_NAME"])
            for it in tbl.query(KeyConditionExpression=boto3.dynamodb.conditions.Key("sub").eq("dev-user-0000"))["Items"]:
                tbl.delete_item(Key={"sub": it["sub"], "id": it["id"]})
            log("cleanup: removed dev-user items from", env["TABLE_NAME"])
        srv = redact(SERVER_LOG.read_text())
        SERVER_LOG.write_text(srv)
        log("\n--- server log (redacted), selected lines ---")
        for line in srv.splitlines():
            if any(k in line for k in ("events:", "live tail", "polling", "terminal", "run microvm", "api:", "WARNING", "ERROR", "Traceback")):
                log("  " + line[:220])


if __name__ == "__main__":
    import boto3.dynamodb.conditions  # noqa: F401
    sys.exit(asyncio.run(main()))
