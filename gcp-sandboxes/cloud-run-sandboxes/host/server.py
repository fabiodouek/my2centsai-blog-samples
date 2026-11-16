"""A tiny command endpoint for the Cloud Run host.

POST /run?timeout=SECONDS with a shell script as the body. The script's output (stdout and
stderr together) streams back line by line, followed by a last line "[exit CODE, SECONDS s]".
GET / answers "ok".

WARNING: this is a remote-code-execution endpoint by design. Deploy it ONLY
with --no-allow-unauthenticated (IAM-only) and delete it when you are done.
"""
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, "ok\n")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/run":
            return self._send(404, "not found\n")
        timeout = int(parse_qs(url.query).get("timeout", ["600"])[0])
        script = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()  # no Content-Length: the body is the stream, closed at the end
        t0 = time.time()
        p = subprocess.Popen(["bash", "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        timer = threading.Timer(timeout, p.kill)
        timer.start()
        try:
            for line in iter(p.stdout.readline, b""):
                self.wfile.write(line)
                self.wfile.flush()
            p.wait()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client went away; the script keeps running to the end
        finally:
            timer.cancel()
        try:
            self.wfile.write(("[exit %d, %.1f s]\n" % (p.returncode, time.time() - t0)).encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
