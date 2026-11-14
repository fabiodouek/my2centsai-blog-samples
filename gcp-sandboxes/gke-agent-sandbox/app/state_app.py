"""State probe for Pod Snapshots (Test 4). Runs as the sandbox's main process.

Keeps four kinds of state and reports them on http://127.0.0.1:8000/state:
  counter     an integer that increases ten times a second (process memory)
  conn        an open TCP connection to www.google.com:80, kept alive (external connection)
  emptydir    a file in an emptyDir volume  (/scratch)
  pvc         a file on a PersistentVolumeClaim (/data)
GET /write?kind=emptydir|pvc&tag=x appends a marker line to that volume, so the test can write AFTER a snapshot.
"""
import http.server, json, os, socket, threading, time, urllib.parse

BOOT = time.time()
state = {"counter": 0}
sock = socket.create_connection(("www.google.com", 80), timeout=10)  # held open on purpose: an external connection
lock = threading.Lock()


def tick():
    while True:
        state["counter"] += 1
        time.sleep(0.1)


def conn_status():
    """Send a keep-alive HTTP/1.1 request on the held connection: 'open' if a reply comes back, else the error name."""
    with lock:
        try:
            sock.settimeout(2)
            sock.send(b"HEAD / HTTP/1.1\r\nHost: www.google.com\r\n\r\n")
            data = sock.recv(4096)
            return "open" if data else "closed-by-peer"
        except Exception as e:
            return type(e).__name__


def keepalive():
    while True:  # Google closes idle connections after a short time: touch it every 5 s so only a restore can break it
        conn_status()
        time.sleep(5)


def read(path):
    try:
        return open(path).read().strip().splitlines()
    except FileNotFoundError:
        return None


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(urllib.parse.unquote(self.path))  # the API server's pod proxy encodes the "?"
        if url.path.endswith("/write"):
            q = urllib.parse.parse_qs(url.query)
            path = {"emptydir": "/scratch/marker.txt", "pvc": "/data/marker.txt"}[q["kind"][0]]
            if not os.path.isdir(os.path.dirname(path)):
                return self.reply({"ok": False, "why": "no such volume"})
            with open(path, "a") as f:
                f.write(f"{q.get('tag', ['w'])[0]}@counter={state['counter']}\n")
            return self.reply({"ok": True})
        self.reply({"counter": state["counter"], "uptime_s": round(time.time() - BOOT, 1), "pid": os.getpid(),
                    "boot": int(BOOT), "conn": conn_status(), "emptydir": read("/scratch/marker.txt"),
                    "pvc": read("/data/marker.txt")})


threading.Thread(target=tick, daemon=True).start()
threading.Thread(target=keepalive, daemon=True).start()
http.server.ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
