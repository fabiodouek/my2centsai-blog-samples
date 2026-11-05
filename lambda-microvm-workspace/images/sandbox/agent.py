"""In-VM agent for the Lambda MicroVM workspace (part 3).

Part 2's agent plus what the workspace backend needs: VM vitals (/stats), Claude Code token
usage parsed from its transcripts (/claude-usage), and a /validate warm-up (VALIDATE_CMD).

The run hook payload carries this MicroVM's settings as JSON: {"env": {...}, "nameserver": null}.
The agent writes the env to /etc/sandbox.env (sourced before every /exec command and by /root/.bashrc,
so the shell, tmux and Claude Code see it too). Nothing secret is in the payload: only placeholder tokens,
the CA variables and labels. There are no proxy settings: the sandbox's VPC answers every name with its
iron-proxy (transparent interception). A "nameserver" is ignored because /etc/resolv.conf is read-only in
the MicroVM, and isn't needed: DNS Firewall in the VPC does the rewriting. Hooks never fail /run or /resume
(that would terminate the MicroVM); errors are logged and returned in the 200 response.

Lambda MicroVMs have no exec or file API, only an HTTPS endpoint that forwards to ports inside the
VM (and the shell, with SHELL_INGRESS), so this agent provides one:

  :8080  app      GET /health, GET /stats, GET /claude-usage, POST /exec, POST /exec/stream (SSE),
                  PUT/GET /files, GET /state, GET /events, GET /headers, POST /tcp/open, GET /tcp/check
  :9001  hooks    POST /aws/lambda-microvms/runtime/v1/{ready,validate,run,resume,suspend,terminate}

Stdlib only. Anyone holding an endpoint token for port 8080 can run any command as root: that's the
point of a sandbox, so guard the tokens (and never mint them for 9001).
"""

import glob
import http.client
import json
import os
import random
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1/"
WORKDIR = "/root/work"
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")

# Values created at import time run during the image build, so they are captured in the
# snapshot and every MicroVM started from the image sees the same ones.
BUILD_TIME = time.time()
BUILD_UUID = str(uuid.uuid4())
BUILD_PID = os.getpid()

STATE = {"events": [], "run_payload": None, "run_uuid": None, "counter": 0}
TCP = {"conn": None, "host": None, "opened_at": None}
WARM = {"lock": threading.Lock(), "thread": None, "done": threading.Event(), "result": None}


def log(msg, **kw):
    # stdout goes to CloudWatch when the MicroVM has an execution role with logs permissions
    print(json.dumps({"t": round(time.time(), 3), "msg": msg, **kw}), flush=True)


def counter():
    while True:
        STATE["counter"] += 1
        time.sleep(1)


def apply_settings(payload):
    """Write /etc/sandbox.env from the run hook payload. Never raises: a failing /run hook terminates
    the MicroVM, so errors are logged and returned instead.

    The payload's "nameserver" is ignored: /etc/resolv.conf is a read-only overlay in the MicroVM. The sandbox's
    VPC rewrites every name to its iron-proxy (DNS Firewall), so no proxy setting is needed here."""
    errors = []
    try:
        cfg = json.loads(payload or "{}")
        if not isinstance(cfg, dict):
            raise ValueError("payload is not a JSON object")
    except ValueError as e:
        errors.append(f"payload: {e}")
        cfg = {}
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    try:
        with open("/etc/sandbox.env", "w") as f:
            for k, v in env.items():
                f.write(f"export {k}={json.dumps(str(v))}\n")
    except OSError as e:
        errors.append(f"/etc/sandbox.env: {e}")
    if cfg.get("nameserver"):
        errors.append("nameserver ignored: /etc/resolv.conf is read-only (the VPC's DNS Firewall rewrites names instead)")
    log("settings applied", env_keys=sorted(env), errors=errors)
    return errors


def wrap(cmd):
    return f"[ -f /etc/sandbox.env ] && . /etc/sandbox.env; {cmd}"


def warm_up():
    """Run VALIDATE_CMD once (in a thread: it can take longer than Lambda waits for one /validate call).
    Always ends with `done` set, so /validate answers 200 even if the warm-up itself fails."""
    t0 = time.time()
    try:
        p = subprocess.run(["/bin/bash", "-c", os.environ["VALIDATE_CMD"]], capture_output=True, text=True,
                           timeout=280, cwd=WORKDIR)
        WARM["result"] = {"validate_rc": p.returncode, "validate_out": (p.stdout + p.stderr)[-600:]}
    except Exception as e:
        WARM["result"] = {"validate_rc": f"{type(e).__name__}: {e}"}
    finally:
        WARM["result"] = WARM["result"] or {}
        WARM["result"]["validate_cmd_s"] = round(time.time() - t0, 2)
        log("warm-up finished", **WARM["result"])
        WARM["done"].set()


# ---------- /stats ----------

def _cpu_sample():
    """(busy, total) jiffies from the aggregate cpu line of /proc/stat. guest/guest_nice are already
    counted in user/nice, so only the first 8 fields are summed."""
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:9]]
    idle = vals[3] + vals[4]  # idle + iowait
    total = sum(vals)
    return total - idle, total


def _meminfo():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, rest = line.partition(":")
            out[k] = int(rest.split()[0])  # kB
    return out


def stats():
    b1, t1 = _cpu_sample()
    time.sleep(0.2)
    b2, t2 = _cpu_sample()
    cpu_pct = round(100.0 * (b2 - b1) / (t2 - t1), 1) if t2 > t1 else 0.0
    mem = _meminfo()
    total_kb = mem.get("MemTotal", 0)
    avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
    fs = os.statvfs("/")
    disk_total = fs.f_blocks * fs.f_frsize
    disk_used = (fs.f_blocks - fs.f_bfree) * fs.f_frsize
    load1, load5, load15 = os.getloadavg()
    return {
        "cpu_pct": cpu_pct,
        "cpu_count": os.cpu_count(),
        "mem_total_mb": round(total_kb / 1024),
        "mem_used_mb": round((total_kb - avail_kb) / 1024),
        "mem_available_mb": round(avail_kb / 1024),
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "disk_used_mb": round(disk_used / 2**20),
        "disk_total_mb": round(disk_total / 2**20),
        "uptime_s": round(float(open("/proc/uptime").read().split()[0]), 1),
        "procs": sum(1 for d in os.listdir("/proc") if d.isdigit()),
        "ts": round(time.time(), 3),
    }


# ---------- /claude-usage ----------

TOKEN_FIELDS = (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
                ("cache_read_tokens", "cache_read_input_tokens"),
                ("cache_creation_tokens", "cache_creation_input_tokens"))
_FILE_CACHE = {}  # path -> ((mtime_ns, size), parsed)


def _is_prompt(rec):
    """A user record that is a typed prompt: not a tool result, meta/system injection, compact
    summary, slash-command echo or a subagent (sidechain) message."""
    if rec.get("isMeta") or rec.get("isSidechain") or rec.get("isCompactSummary"):
        return False
    content = (rec.get("message") or {}).get("content")
    if isinstance(content, str):
        return not content.lstrip().startswith(("<command-", "<local-command-"))
    if isinstance(content, list):
        return bool(content) and not any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content)
    return False


def _parse_transcript(path):
    """One transcript file -> {session_id: {...}}. Partial or corrupt lines (the file is being written
    while we read it) are skipped and counted."""
    project_dir = os.path.relpath(path, CLAUDE_PROJECTS).split(os.sep)[0]
    sessions, skipped = {}, 0
    with open(path, "rb") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(rec, dict):
                continue
            sid = rec.get("sessionId")
            if not sid:
                continue
            s = sessions.setdefault(sid, {"project": None, "project_dir": project_dir, "first_ts": None,
                                          "last_ts": None, "turns": 0, "msgs": {}, "last_model": None})
            if rec.get("cwd") and not rec.get("isSidechain"):
                s["project"] = rec["cwd"]
            ts = rec.get("timestamp")
            if isinstance(ts, str):
                s["first_ts"] = min(s["first_ts"] or ts, ts)
                s["last_ts"] = max(s["last_ts"] or ts, ts)
            kind = rec.get("type")
            if kind == "user" and _is_prompt(rec):
                s["turns"] += 1
            elif kind == "assistant":
                msg = rec.get("message") or {}
                usage, model = msg.get("usage"), msg.get("model")
                if not isinstance(usage, dict) or not model or model == "<synthetic>":
                    continue
                # A response with several content blocks is written as several lines that repeat the same
                # message id; keep the largest value seen for each counter instead of summing them.
                key = msg.get("id") or rec.get("requestId") or rec.get("uuid")
                prev = s["msgs"].get(key, {"model": model})
                for out_name, in_name in TOKEN_FIELDS:
                    prev[out_name] = max(prev.get(out_name, 0), int(usage.get(in_name) or 0))
                s["msgs"][key] = prev
                if not rec.get("isSidechain"):  # the session's model is the main thread's, not a subagent's
                    s["last_model"] = model
    return {"sessions": sessions, "skipped": skipped}


def claude_usage():
    files = sorted(glob.glob(os.path.join(CLAUDE_PROJECTS, "**", "*.jsonl"), recursive=True))
    merged, skipped = {}, 0
    for path in files:
        try:
            st = os.stat(path)
            sig = (st.st_mtime_ns, st.st_size)
            cached = _FILE_CACHE.get(path)
            if not cached or cached[0] != sig:
                cached = (sig, _parse_transcript(path))
                _FILE_CACHE[path] = cached
        except OSError:
            continue
        parsed = cached[1]
        skipped += parsed["skipped"]
        # Subagent transcripts live in their own files but carry the parent's sessionId: merge them.
        for sid, s in parsed["sessions"].items():
            m = merged.setdefault(sid, {"project": None, "project_dir": s["project_dir"], "first_ts": None,
                                        "last_ts": None, "turns": 0, "msgs": {}, "last_model": None})
            m["project"] = m["project"] or s["project"]
            for k, fn in (("first_ts", min), ("last_ts", max)):
                if s[k]:
                    m[k] = fn(m[k] or s[k], s[k])
            m["turns"] += s["turns"]
            m["msgs"].update(s["msgs"])
            m["last_model"] = s["last_model"] or m["last_model"]

    def zero():
        return {name: 0 for name, _ in TOKEN_FIELDS}

    sessions, totals, total_by_model = [], {**zero(), "turns": 0, "api_calls": 0}, {}
    for sid, m in merged.items():
        model = m["last_model"] or max((x["model"] for x in m["msgs"].values()), default=None,
                                       key=lambda name: sum(x["model"] == name for x in m["msgs"].values()))
        row = {"session_id": sid, "project": m["project"] or m["project_dir"], "model": model,
               "turns": m["turns"], "api_calls": len(m["msgs"]), **zero(), "models": {},
               "first_ts": m["first_ts"], "last_ts": m["last_ts"]}
        for msg in m["msgs"].values():
            per = row["models"].setdefault(msg["model"], {**zero(), "api_calls": 0})
            tm = total_by_model.setdefault(msg["model"], {**zero(), "api_calls": 0})
            per["api_calls"] += 1
            tm["api_calls"] += 1
            for name, _ in TOKEN_FIELDS:
                row[name] += msg[name]
                per[name] += msg[name]
                tm[name] += msg[name]
        for name, _ in TOKEN_FIELDS:
            totals[name] += row[name]
        totals["turns"] += row["turns"]
        totals["api_calls"] += row["api_calls"]
        sessions.append(row)
    sessions.sort(key=lambda r: r["last_ts"] or "", reverse=True)
    totals.update(sessions=len(sessions), models=total_by_model)
    return {"sessions": sessions, "totals": totals, "files": len(files), "skipped_lines": skipped}


# ---------- HTTP servers ----------

class Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def send_json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def log_message(self, fmt, *args):
        pass


class Hooks(Base):
    def do_POST(self):
        raw = self.body()
        if not self.path.startswith(HOOK_PREFIX):
            return self.send_json(404, {"error": "not a hook"})
        hook = self.path[len(HOOK_PREFIX):]
        # The peer address tells Lambda's own hook calls apart from ones forwarded through the endpoint
        event = {"hook": hook, "t": time.time(), "bytes": len(raw), "peer": self.client_address[0]}
        code = 200
        try:
            code = self.handle_hook(hook, raw, event)
        except Exception as e:
            # A non-200 (or a timeout) from /run or /resume terminates the MicroVM: report, don't fail
            event["error"] = f"{type(e).__name__}: {e}"
        STATE["events"].append(event)
        log("hook", **event)
        body = {"hook": hook, **({"error": event["error"]} if event.get("error") else {})}
        if code == 503:
            body["status"] = "warming"
        return self.send_json(code, body)

    def handle_hook(self, hook, raw, event):
        if hook == "run":
            body = json.loads(raw or b"{}")
            payload = body.get("runHookPayload")
            STATE["run_payload"] = payload
            STATE["microvm_id"] = body.get("microvmId")
            # Fresh randomness per MicroVM: the documented fix for snapshot-shared values
            STATE["run_uuid"] = str(uuid.UUID(bytes=os.urandom(16), version=4))
            event["payload_len"] = len(payload or "")
            errors = apply_settings(payload)
            if errors:
                event["error"] = "; ".join(errors)
        if hook == "validate" and os.environ.get("VALIDATE_CMD"):
            # Lambda records which parts of the snapshot the validation touches and prefetches them at run
            # time, so exercise whatever a real session needs first (claude, tmux, node...). Lambda gives up
            # on a single /validate call after ~10 s and calls again; a 503 asks it to retry. So the warm-up
            # runs in the background and every call waits a few seconds for it, answering 503 until it's done.
            with WARM["lock"]:
                if WARM["thread"] is None:
                    WARM["thread"] = threading.Thread(target=warm_up, daemon=True)
                    WARM["thread"].start()
            if not WARM["done"].wait(5):
                event["validate"] = "warming"
                return 503
            event.update(WARM["result"] or {})
        return 200


class App(Base):
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self.send_json(200, {"ok": True})
        if u.path == "/stats":
            return self.send_json(200, stats())
        if u.path == "/claude-usage":
            try:
                return self.send_json(200, claude_usage())
            except Exception as e:  # never let a weird transcript take the endpoint down
                return self.send_json(500, {"error": f"{type(e).__name__}: {e}"})
        if u.path == "/events":
            return self.send_json(200, {"events": STATE["events"]})
        if u.path == "/headers":
            return self.send_json(200, {"peer": self.client_address[0], "headers": dict(self.headers)})
        if u.path == "/state":
            return self.send_json(200, {
                "pid": os.getpid(),
                "build_pid": BUILD_PID,
                "build_time": BUILD_TIME,
                "now": time.time(),
                "uptime_s": float(open("/proc/uptime").read().split()[0]),
                "counter": STATE["counter"],
                "microvm_id": STATE.get("microvm_id"),
                "build_uuid": BUILD_UUID,
                "run_uuid": STATE["run_uuid"],
                # Generated per request: Python's `random` is seeded at import (in the snapshot),
                # uuid4 reads the kernel RNG.
                "random_random": random.random(),
                "uuid4_now": str(uuid.uuid4()),
                "boot_id": open("/proc/sys/kernel/random/boot_id").read().strip(),
                "run_payload_len": len(STATE["run_payload"] or ""),
            })
        if u.path == "/files":
            path = parse_qs(u.query)["path"][0]
            try:
                data = open(path, "rb").read()
            except FileNotFoundError:
                return self.send_json(404, {"error": "not found", "path": path})
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        if u.path == "/tcp/check":
            c = TCP["conn"]
            if c is None:
                return self.send_json(409, {"error": "no connection open"})
            try:
                c.request("HEAD", "/", headers={"Host": TCP["host"]})
                r = c.getresponse()
                r.read()
                return self.send_json(200, {"ok": True, "status": r.status, "age_s": round(time.time() - TCP["opened_at"], 1)})
            except Exception as e:
                return self.send_json(200, {"ok": False, "error": f"{type(e).__name__}: {e}"})
        return self.send_json(404, {"error": "not found"})

    def do_PUT(self):
        u = urlparse(self.path)
        if u.path != "/files":
            return self.send_json(404, {"error": "not found"})
        path = parse_qs(u.query)["path"][0]
        data = self.body()
        os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return self.send_json(200, {"path": path, "bytes": len(data)})

    def do_POST(self):
        raw = self.body()
        if self.path == "/exec":
            req = json.loads(raw or b"{}")
            if req.get("detach"):
                p = subprocess.Popen(["/bin/bash", "-c", wrap(req["cmd"])], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True, cwd=req.get("cwd", WORKDIR))
                return self.send_json(200, {"pid": p.pid})
            t0 = time.time()
            try:
                p = subprocess.run(["/bin/bash", "-c", wrap(req["cmd"])], capture_output=True, text=True,
                                   timeout=req.get("timeout", 120), cwd=req.get("cwd", WORKDIR))
                out = {"rc": p.returncode, "stdout": p.stdout, "stderr": p.stderr}
            except subprocess.TimeoutExpired as e:
                out = {"rc": None, "stdout": (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
                       "stderr": "timeout"}
            out["seconds"] = round(time.time() - t0, 3)
            return self.send_json(200, out)
        if self.path == "/exec/stream":
            # Server-sent events: one event per output line, then an `exit` event.
            req = json.loads(raw or b"{}")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            p = subprocess.Popen(["/bin/bash", "-c", wrap(req["cmd"])], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=req.get("cwd", WORKDIR))
            for line in p.stdout:
                self.wfile.write(f"data: {json.dumps(line.rstrip())}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(f"event: exit\ndata: {p.wait()}\n\n".encode())
            self.wfile.flush()
            self.close_connection = True
            return
        if self.path == "/tcp/open":
            req = json.loads(raw or b"{}")
            host = req.get("host", "example.com")
            c = http.client.HTTPSConnection(host, 443, timeout=10)
            c.request("HEAD", "/", headers={"Host": host})
            r = c.getresponse()
            r.read()
            TCP.update(conn=c, host=host, opened_at=time.time())
            return self.send_json(200, {"ok": True, "status": r.status})
        return self.send_json(404, {"error": "not found"})


def serve(port, handler):
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()


if __name__ == "__main__":
    os.makedirs(WORKDIR, exist_ok=True)
    threading.Thread(target=counter, daemon=True).start()
    threading.Thread(target=serve, args=(9001, Hooks), daemon=True).start()
    log("agent listening", app=8080, hooks=9001, build_uuid=BUILD_UUID)
    serve(8080, App)
