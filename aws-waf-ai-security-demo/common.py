"""Shared helpers for the WAF AI Security demo scripts."""
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
PROJECT = "waf-ai-security"
PREFIX = "waf-ai-demo"
TAGS = {"project": PROJECT}
STATE_FILE = Path(__file__).with_name(".state.json")
NS = "awswaf:managed:aws:ai-security:"
MCP_VERSION = "2025-11-25"

BOLD, DIM, RED, GREEN, YELLOW, CYAN, RESET = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"


def client(name):
    return boto3.client(name, region_name=REGION)


def account_id():
    return client("sts").get_caller_identity()["Account"]


def load_state():
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(**kv):
    s = load_state()
    s.update(kv)
    STATE_FILE.write_text(json.dumps(s, indent=2))
    return s


def mask(text):
    """Hide the account id in anything we print."""
    s = load_state()
    acct = s.get("account")
    return text.replace(acct, "<account>") if acct else text


def say(msg=""):
    print(mask(msg), flush=True)


def head(msg):
    say(f"\n{BOLD}{CYAN}== {msg}{RESET}")


def http(method, url, body=None, headers=None, timeout=30):
    """Returns (status, response text, seconds). Never raises on HTTP errors."""
    data = None
    h = {"User-Agent": "waf-ai-demo/1.0"}
    if body is not None:
        data = body if isinstance(body, bytes) else (body if isinstance(body, str) else json.dumps(body)).encode()
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace"), time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), time.perf_counter() - t0


# ---------- request builders: each returns (method, path, headers, body) ----------

def mcp(method, params=None, version=MCP_VERSION, headers=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = dict(params)
    h = {"Accept": "application/json, text/event-stream"}
    if version:
        h["MCP-Protocol-Version"] = version
    if version and version >= "2026-07-28":
        # This revision is stateless: every request repeats the version in _meta
        # and mirrors the method and the tool name in headers.
        body.setdefault("params", {})["_meta"] = {"io.modelcontextprotocol/protocolVersion": version}
        h["Mcp-Method"] = method
        if method == "tools/call" and params:
            h["Mcp-Name"] = params.get("name", "")
    h.update(headers or {})
    return "POST", "/mcp", h, body


def mcp_call(tool, args, **kw):
    return mcp("tools/call", {"name": tool, "arguments": args}, **kw)


def mcp_init(version=MCP_VERSION):
    return mcp("initialize", {"protocolVersion": version, "capabilities": {},
                              "clientInfo": {"name": "demo-client", "version": "1.0"}}, version=version)


def a2a_send(text, role="ROLE_USER", rest=False, drop=None):
    msg = {"messageId": str(uuid.uuid4()), "role": role, "parts": [{"text": text}]}
    for k in drop or []:
        msg.pop(k, None)
    h = {"A2A-Version": "1.0"}
    if rest:
        return "POST", "/a2a/message:send", h, {"message": msg}
    return "POST", "/a2a", h, {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": msg}}


def chat(prompt, system=None):
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    return "POST", "/v1/chat/completions", {"Authorization": "Bearer demo-key"}, {"model": "demo-model", "messages": messages}


def send(base, req, tag=None, timeout=30):
    method, path, headers, body = req
    headers = dict(headers)
    if tag:
        headers["x-demo-id"] = tag
    return http(method, base + path, body, headers, timeout)


def new_tag():
    return uuid.uuid4().hex[:12]


# ---------- WAF logs ----------

def waf_log_records(log_group, tags, wait=180, start_ms=None):
    """Find the WAF log record of each request by its x-demo-id header.

    WAF delivers logs to CloudWatch Logs with a delay, so this polls.
    Returns {tag: record}.
    """
    logs = client("logs")
    want, found = set(tags), {}
    start_ms = start_ms or int((time.time() - 900) * 1000)
    deadline = time.time() + wait
    while True:
        kw = dict(logGroupName=log_group, startTime=start_ms)
        while True:
            page = logs.filter_log_events(**kw)
            for ev in page.get("events", []):
                rec = json.loads(ev["message"])
                for hd in rec.get("httpRequest", {}).get("headers", []):
                    if hd["name"].lower() == "x-demo-id" and hd["value"] in want:
                        found[hd["value"]] = rec
            if not page.get("nextToken"):
                break
            kw["nextToken"] = page["nextToken"]
        if want <= set(found) or time.time() > deadline:
            return found
        time.sleep(10)


def labels_of(rec):
    return sorted(l["name"].replace(NS, "") for l in rec.get("labels", []) if l["name"].startswith(NS))


def verdict(rec):
    """(action, terminating rule inside the AI Security group or '')"""
    rule = ""
    for g in rec.get("ruleGroupList", []):
        t = g.get("terminatingRule")
        if t:
            rule = t.get("ruleId", "")
    return rec.get("action", "?"), rule
