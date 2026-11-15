"""Shared helpers: configuration, the Cloudflare API, and a tiny MCP client over Streamable HTTP."""
import json
import os
import socket
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
RESULTS = HERE / "results"
STATE = HERE / ".state.json"  # ids and service-token secrets created by 02_create_portal.py (gitignored)


def _load_env() -> None:
    """Read .env next to this file, or the shared experiments file, without overriding real env vars."""
    for p in (HERE / ".env", Path.home() / ".config/my2centsai/cloudflare-experiments.env"):
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_env()
TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
ACCOUNT = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
DOMAIN = os.environ.get("LAB_DOMAIN", "my2centsai-experiments.co.uk")
WORKERS_SUB = os.environ.get("WORKERS_SUBDOMAIN", "my2centsai-experiments")
PORTAL_ID = "demo"
PORTAL_HOST = f"mcp-portal-demo.{DOMAIN}"
NOTES_URL = f"https://notes-mcp.{WORKERS_SUB}.workers.dev/mcp"
TICKETS_URL = f"https://tickets-mcp.{WORKERS_SUB}.workers.dev/mcp"
PORTAL_URL = f"https://{PORTAL_HOST}/mcp"
API = "https://api.cloudflare.com/client/v4"


_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_with_doh(host, *args, **kwargs):
    """A name that was looked up before its DNS record existed can stay cached as NXDOMAIN on the local resolver
    (the zone's negative TTL is 30 minutes). For hostnames in the lab domain, fall back to DNS over HTTPS."""
    try:
        return _getaddrinfo(host, *args, **kwargs)
    except socket.gaierror:
        if not (isinstance(host, str) and host.endswith(DOMAIN)):
            raise
        r = httpx.get("https://1.1.1.1/dns-query", params={"name": host, "type": "A"}, headers={"accept": "application/dns-json"}, timeout=10)
        ips = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
        if not ips:
            raise
        return _getaddrinfo(ips[0], *args, **kwargs)


socket.getaddrinfo = _getaddrinfo_with_doh


class CfError(RuntimeError):
    pass


def cf(method: str, path: str, body=None, params=None, ok_codes=(200, 201)):
    """Call the Cloudflare API under /accounts/{id}. Returns (status, parsed JSON)."""
    if not TOKEN or not ACCOUNT:
        raise SystemExit("Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID (see .env.example)")
    url = path if path.startswith("http") else f"{API}/accounts/{ACCOUNT}/{path.lstrip('/')}"
    r = httpx.request(method, url, json=body, params=params, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)
    try:
        data = r.json()
    except ValueError:
        data = {"raw": r.text[:300]}
    return r.status_code, data


def cf_ok(method, path, body=None, params=None):
    status, data = cf(method, path, body, params)
    if status >= 300 or data.get("success") is False:
        raise CfError(f"{method} {path} -> {status} {json.dumps(data.get('errors') or data)[:400]}")
    return data.get("result")


def load_state() -> dict:
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(state: dict) -> None:
    STATE.write_text(json.dumps(state, indent=1))
    STATE.chmod(0o600)


def save_result(name: str, data) -> None:
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / name).write_text(json.dumps(data, indent=1) + "\n")


class Mcp:
    """Minimal MCP client: one JSON-RPC POST per call to a Streamable HTTP endpoint. Stateless servers need no session."""

    def __init__(self, url: str, headers: dict | None = None, protocol: str = "2025-06-18"):
        self.url, self.protocol = url, protocol
        self.headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                        "MCP-Protocol-Version": protocol, **(headers or {})}
        self._id = 0
        self.retries_1014 = 0
        self.client = httpx.Client(timeout=90)

    def rpc(self, method: str, params: dict | None = None, timeout: float | None = None):
        """Returns (http_status, elapsed_ms, parsed_message_or_text, response_headers)."""
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, **({"params": params} if params is not None else {})}
        t = time.perf_counter()
        for attempt in range(8):
            r = self.client.post(self.url, json=body, headers=self.headers, timeout=timeout or 90)
            # Now and then the edge answers a portal call with error 1014 ("CNAME cross-user banned"), even
            # after the portal has worked. Wait a moment and send the same request again, and count it.
            if r.status_code == 403 and "error-1014" in r.text:
                self.retries_1014 += 1
                time.sleep(2)
                continue
            break
        ms = (time.perf_counter() - t) * 1000
        text = r.text
        msg = None
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    msg = json.loads(line[5:].strip())
                except ValueError:
                    pass
        if msg is None:
            try:
                msg = r.json()
            except ValueError:
                msg = text[:400]
        return r.status_code, ms, msg, r.headers

    def initialize(self):
        """initialize, then notifications/initialized. A portal answers 2025-06-18 clients with a session id
        that every later request must carry; a stateless Worker sends none."""
        out = self.rpc("initialize", {"protocolVersion": self.protocol, "capabilities": {},
                                      "clientInfo": {"name": "portal-demo", "version": "1.0"}})
        sid = out[3].get("mcp-session-id")
        if sid:
            self.headers["Mcp-Session-Id"] = sid
        if out[0] == 200:
            self.client.post(self.url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=self.headers)
        return out

    def tools(self) -> list[dict]:
        status, _, msg, _ = self.rpc("tools/list")
        return (msg or {}).get("result", {}).get("tools", []) if isinstance(msg, dict) else []

    def call(self, name: str, arguments: dict | None = None, timeout: float | None = None):
        return self.rpc("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)


def svc_headers(state: dict, which: str = "agent") -> dict:
    t = state["service_tokens"][which]
    return {"CF-Access-Client-Id": t["client_id"], "CF-Access-Client-Secret": t["client_secret"]}
