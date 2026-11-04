"""Suspend/resume latency as the browser sees it, against the deployed workspace (CloudFront -> ALB ->
Fargate). A throwaway Cognito user creates one sandbox, opens /ws/events and /ws/terminal through
CloudFront, then times N suspend/resume cycles:

  suspend: POST time and returned state; lifecycle SUSPENDED on /ws/events; terminal status 'suspended'
  resume:  POST time and returned state; lifecycle RUNNING; terminal re-'attached' (same socket); a command echo

Also checks SANDBOX_NAME inside the sandbox and the pricing text in /api/config. Terminates the sandbox and
deletes the user at the end. Output (account redacted) -> ../results/backend_latency.log

  cd backend && uv run python scripts/measure_deployed.py [cycles]"""

from __future__ import annotations

import asyncio
import json
import secrets
import statistics
import sys
import time
from pathlib import Path

import boto3
import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
OUT = {k: (v["value"] if isinstance(v, dict) and "value" in v else v)
       for k, v in json.loads((ROOT.parent / "results" / "infra_outputs.json").read_text()).items()}
REGION = OUT.get("region", "us-east-1")
ACCOUNT = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
BASE = OUT["url"].rstrip("/")
WSB = BASE.replace("https://", "wss://")
idp = boto3.client("cognito-idp", region_name=REGION)
LOG = open(ROOT.parent / "results" / "backend_latency.log", "w")
T0 = time.perf_counter()


def log(*a):
    line = " ".join(str(x) for x in a).replace(ACCOUNT, "<acct>")
    line = f"[{time.perf_counter() - T0:7.2f}s] {line}"
    print(line, flush=True)
    LOG.write(line + "\n")
    LOG.flush()


class Feed:
    def __init__(self):
        self.events: list[tuple[float, dict]] = []

    async def run(self, ws):
        try:
            async for m in ws:
                self.events.append((time.perf_counter(), json.loads(m)))
        except websockets.ConnectionClosed:
            pass

    async def wait(self, pred, after: float, timeout: float = 60) -> float:
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            for t, e in self.events:
                if t >= after and pred(e):
                    return t
            await asyncio.sleep(0.02)
        raise TimeoutError("event not seen")


class Term(Feed):
    def __init__(self):
        super().__init__()
        self.out = ""

    async def run(self, ws):
        try:
            async for m in ws:
                if isinstance(m, bytes):
                    self.out += m.decode(errors="replace")
                else:
                    self.events.append((time.perf_counter(), json.loads(m)))
        except websockets.ConnectionClosed:
            pass

    def status(self, state):
        return lambda e: e.get("type") == "status" and e.get("state") == state

    async def expect(self, ws, cmd: str, want: str, timeout=20):
        mark = len(self.out)
        await ws.send((cmd + "\r").encode())
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            if want in self.out[mark:]:
                return True
            await asyncio.sleep(0.05)
        return False


def med(xs):
    return f"median {statistics.median(xs):.2f}s (min {min(xs):.2f}, max {max(xs):.2f}, n={len(xs)})" if xs else "n/a"


async def main(cycles: int) -> int:
    email = f"latency-{secrets.token_hex(3)}@example.com"
    password = "Aa1!" + secrets.token_urlsafe(18)
    pool, client = OUT["user_pool_id"], OUT["client_id"]
    idp.admin_create_user(UserPoolId=pool, Username=email, MessageAction="SUPPRESS",
                          UserAttributes=[{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": "true"}])
    vm_id = None
    h = None
    try:
        idp.admin_set_user_password(UserPoolId=pool, Username=email, Password=password, Permanent=True)
        tok = idp.initiate_auth(ClientId=client, AuthFlow="USER_PASSWORD_AUTH",
                                AuthParameters={"USERNAME": email, "PASSWORD": password})["AuthenticationResult"]["IdToken"]
        h = httpx.AsyncClient(base_url=BASE, headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        log(f"deployed workspace {BASE} | throwaway user signed in")
        cfg = (await h.get("/api/config")).json()
        log("pricing text from /api/config:", json.dumps({k: cfg["pricing"][k] for k in ("source", "as_of", "note")}))

        ev_ws = await websockets.connect(f"{WSB}/ws/events", max_size=None, open_timeout=20)
        await ev_ws.send(json.dumps({"type": "auth", "token": tok}))
        feed = Feed()
        asyncio.create_task(feed.run(ev_ws))

        name = f"latency {secrets.token_hex(2)}"
        for _ in range(10):
            r = await h.post("/api/sandboxes", json={"name": name, "idle_minutes": 15, "max_hours": 1})
            if r.status_code != 409:
                break
            log("quota full; retrying in 60 s")
            await asyncio.sleep(60)
        r.raise_for_status()
        vm_id = r.json()["id"]
        t = time.perf_counter()
        await feed.wait(lambda e: e.get("type") == "lifecycle" and e.get("id") == vm_id and e.get("state") == "RUNNING", t - 5)
        log(f"created {vm_id} ('{name}'); RUNNING")

        term_ws = await websockets.connect(f"{WSB}/ws/terminal/{vm_id}", max_size=None, open_timeout=20)
        await term_ws.send(json.dumps({"type": "auth", "token": tok}))
        await term_ws.send(json.dumps({"type": "resize", "rows": 30, "cols": 120}))
        term = Term()
        asyncio.create_task(term.run(term_ws))
        t = time.perf_counter()
        await term.wait(term.status("attached"), t - 1)
        log(f"terminal attached via CloudFront in {time.perf_counter() - t:.2f}s")
        await asyncio.sleep(1.5)
        ok = await term.expect(term_ws, 'echo "name=[$SANDBOX_NAME]"', f"name=[{name}]")
        log(f"SANDBOX_NAME inside the sandbox: {'ok' if ok else 'NOT SEEN'} ('{name}')")

        rows = {k: [] for k in ("s_post", "s_event", "s_term", "r_post", "r_event", "r_term")}
        for i in range(1, cycles + 1):
            t = time.perf_counter()
            r = await h.post(f"/api/sandboxes/{vm_id}/suspend")
            post = time.perf_counter() - t
            s_ev = await feed.wait(lambda e: e.get("type") == "lifecycle" and e.get("id") == vm_id and e.get("state") == "SUSPENDED", t)
            s_tm = await term.wait(term.status("suspended"), t)
            rows["s_post"].append(post); rows["s_event"].append(s_ev - t); rows["s_term"].append(s_tm - t)
            log(f"cycle {i} suspend: POST {post:.2f}s -> state {r.json()['state']}; lifecycle SUSPENDED +{s_ev - t:.2f}s; "
                f"terminal 'suspended' +{s_tm - t:.2f}s")
            await asyncio.sleep(3)
            t = time.perf_counter()
            r = await h.post(f"/api/sandboxes/{vm_id}/resume")
            post = time.perf_counter() - t
            r_ev = await feed.wait(lambda e: e.get("type") == "lifecycle" and e.get("id") == vm_id and e.get("state") == "RUNNING", t)
            r_tm = await term.wait(term.status("attached"), t)
            rows["r_post"].append(post); rows["r_event"].append(r_ev - t); rows["r_term"].append(r_tm - t)
            ok = await term.expect(term_ws, f"echo cycle-$(({i}*7))", f"cycle-{i * 7}")
            log(f"cycle {i} resume:  POST {post:.2f}s -> state {r.json()['state']}; lifecycle RUNNING +{r_ev - t:.2f}s; "
                f"terminal re-attached +{r_tm - t:.2f}s; command after reattach {'ok' if ok else 'FAILED'}")
            await asyncio.sleep(2)

        log("\nsummary (seconds from the click, i.e. from sending the POST):")
        log("  suspend POST returns:          ", med(rows["s_post"]))
        log("  lifecycle SUSPENDED (state pill):", med(rows["s_event"]))
        log("  terminal shows 'suspended':    ", med(rows["s_term"]))
        log("  resume POST returns:           ", med(rows["r_post"]))
        log("  lifecycle RUNNING (state pill):", med(rows["r_event"]))
        log("  terminal re-attached:          ", med(rows["r_term"]))
        await term_ws.close()
        await ev_ws.close()
        return 0
    finally:
        if vm_id and h:
            r = await h.delete(f"/api/sandboxes/{vm_id}")
            log(f"terminate -> {r.status_code} {r.json().get('state')}")
        if h:
            await h.aclose()
        idp.admin_delete_user(UserPoolId=pool, Username=email)
        log("throwaway user deleted")


if __name__ == "__main__":
    sys.exit(asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)))
