"""End-to-end: the addon inside a real mitmdump 11.0.2, with real HTTP clients and a fake AWS.

The unit tests drive the hooks through a model of mitmproxy's request path (harness.drive).
These tests check that model against the real proxy, for the cases where the timing matters:
when a streamed body's headers go upstream, what a kill and a local response do, and whether
a response streams. Nothing leaves the machine:

  client (http.client, through the proxy, TLS trusting the mitmproxy CA)
    -> mitmdump -s system.py -s sigv4_resign.py -s e2e_upstream.py
       (stream_large_bodies=1m, lazy connect; the sidecar's script order)
    -> fake AWS endpoint on 127.0.0.1 (TLS), which checks each request with verifier.py

system.py is OpenSandbox's own egress script (release-1.2.0), loaded ahead of the addon as the
sidecar loads it. It is found at $OSB_EGRESS_SYSTEM_PY, at /var/egress/mitmscripts/system.py
(inside opensandbox/egress:release-1.2.0), or in part 1's source checkout
(../../opensandbox-eks/.src/OpenSandbox/components/egress/mitmscripts/system.py). It reads the
Credential Vault from a Unix socket; a fake vault serves one customHeaders binding, so system.py
injects the three carriers itself, exactly as in the sidecar. Without system.py the client sends
the carriers itself and e2e_system_rule.py stands in for system.py's response-streaming rule.

Regular proxy mode, not transparent: mitmproxy takes the upstream from the CONNECT target and
the Host header here, so the upstream pin itself is covered by the unit test, not by this file.
"""

from __future__ import annotations

import datetime
import http.client
import http.server
import ipaddress
import json
import os
import re
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import harness as h
import verifier
from test_sigv4_resign import EMPTY_SHA, S3_BUCKET, SIGV4_QUERY, bedrock_converse, dynamodb_put, s3_put_chunked, sts_call

EGRESS = Path(__file__).resolve().parent.parent
ADDON = EGRESS / "sigv4_resign.py"
UPSTREAM_ADDON = Path(__file__).resolve().parent / "e2e_upstream.py"
SYSTEM_RULE_ADDON = Path(__file__).resolve().parent / "e2e_system_rule.py"
SYSTEM_PY = next(
    (
        Path(candidate)
        for candidate in (
            os.environ.get("OSB_EGRESS_SYSTEM_PY", ""),
            "/var/egress/mitmscripts/system.py",
            str(EGRESS.parent.parent / "opensandbox-eks/.src/OpenSandbox/components/egress/mitmscripts/system.py"),
        )
        if candidate and Path(candidate).is_file()
    ),
    None,
)
REGION = "us-east-1"
VAULT_ETAG = '"osb-e2e-1"'
VAULT = {  # the active-vault snapshot system.py fetches: one binding, the three carriers
    "revision": 1,
    "bindings": [
        {
            "name": "aws-sigv4",
            "match": {
                "schemes": ["https"],
                "hosts": [f"sts.{REGION}.amazonaws.com", f"s3.{REGION}.amazonaws.com", f"*.s3.{REGION}.amazonaws.com",
                          f"dynamodb.{REGION}.amazonaws.com", f"bedrock-runtime.{REGION}.amazonaws.com"],
                "methods": ["GET", "HEAD", "POST", "PUT", "DELETE"],
                "paths": ["/*"],
            },
            "headers": [
                {"name": name, "value": value}
                for name, value in zip(h.CARRIER_NAMES, (h.REAL_AKID, h.REAL_SECRET, h.REAL_TOKEN))
            ],
        }
    ],
    "redactions": [h.REAL_AKID, h.REAL_SECRET, h.REAL_TOKEN],
}
STREAM_PART_1 = b"\x00" * 64  # stands in for the first event-stream frame
STREAM_PART_2 = b"\x01" * 64


# --- fake Credential Vault (the private Unix socket system.py reads) -----------------------------


class VaultHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/credential-vault/_active":
            self.send_error(404)
            return
        if self.headers.get("If-None-Match") == VAULT_ETAG:
            self.send_response(304)
            self.send_header("ETag", VAULT_ETAG)
            self.end_headers()
            return
        body = json.dumps(VAULT).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("ETag", VAULT_ETAG)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def address_string(self):
        return "unix"

    def log_message(self, *args):
        pass


class VaultServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


# --- fake AWS endpoint -------------------------------------------------------------------------


class Endpoint(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, addr, context):
        super().__init__(addr, EndpointHandler)
        self.context = context
        self.received: list[dict] = []
        self.lock = threading.Lock()
        self.stream_release = threading.Event()
        self.stream_released_by_client: list[bool] = []

    def get_request(self):
        sock, addr = super().get_request()
        return self.context.wrap_socket(sock, server_side=True), addr


class EndpointHandler(socketserver.StreamRequestHandler):
    def handle(self):
        while self.one():
            pass

    def one(self) -> bool:
        line = self.rfile.readline()
        if not line:
            return False
        method, target, _ = line.decode().split(" ", 2)
        headers = []
        while (raw := self.rfile.readline().decode()) not in ("\r\n", "\n", ""):
            name, value = raw.split(":", 1)
            headers.append((name.strip(), value.strip()))
        lower = {n.lower(): v for n, v in headers}
        body = b""
        if "chunked" in lower.get("transfer-encoding", "").lower():
            while (size := int(self.rfile.readline().split(b";")[0], 16)) != 0:
                body += self.rfile.read(size)
                self.rfile.readline()
            while self.rfile.readline() not in (b"\r\n", b""):
                pass
        elif "content-length" in lower:
            body = self.rfile.read(int(lower["content-length"]))
        path, _, query = target.partition("?")
        result = verifier.verify(
            method, path, query, headers, body,
            access_key=h.REAL_AKID, secret_key=h.REAL_SECRET, session_token=h.REAL_TOKEN,
        )
        with self.server.lock:
            self.server.received.append({"method": method, "target": target, "headers": headers, "ok": result.ok, "detail": result.detail})
        if not result.ok:
            return self.send(403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>")
        if path.endswith("/echo-error"):
            return self.send(403, echo_error(lower))
        if path.endswith("/echo-error-chunked"):  # 7-byte chunks: the token spans many of them
            return self.send_chunked(403, echo_error(lower), 7)
        if path.endswith("/echo-error-chunked-big"):
            return self.send_chunked(403, echo_error(lower) + b" " * (2 * h.MIB), 64 * 1024)
        if path.endswith("/echo-error-big"):
            return self.send(403, echo_error(lower) + b" " * (2 * h.MIB))
        if path.endswith("/echo-error-event-stream"):  # Content-Length, but system.py streams it
            return self.send(403, echo_error(lower), "text/event-stream")
        if path.endswith("/converse-stream"):
            total = len(STREAM_PART_1) + len(STREAM_PART_2)
            self.wfile.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.amazon.eventstream\r\nContent-Length: {total}\r\n\r\n".encode()
                + STREAM_PART_1
            )
            self.wfile.flush()
            # The client sets the event once part 1 arrived, which it can only do if the proxy
            # streams the response. A proxy that buffers lets this time out instead.
            self.server.stream_released_by_client.append(self.server.stream_release.wait(timeout=5))
            self.wfile.write(STREAM_PART_2)
            self.wfile.flush()
            return True
        return self.send(200, b"<ok/>")

    def send(self, status: int, data: bytes, content_type: str = "application/xml") -> bool:
        try:
            self.wfile.write(f"HTTP/1.1 {status} X\r\nContent-Type: {content_type}\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
            self.wfile.flush()
        except OSError:  # the proxy killed the flow and closed the connection
            return False
        return True

    def send_chunked(self, status: int, data: bytes, chunk: int) -> bool:
        try:
            self.wfile.write(f"HTTP/1.1 {status} X\r\nContent-Type: application/xml\r\nTransfer-Encoding: chunked\r\n\r\n".encode())
            for i in range(0, len(data), chunk):
                part = data[i : i + chunk]
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:
            return False
        return True


def echo_error(lower: dict[str, str]) -> bytes:
    """What S3's SignatureDoesNotMatch carries: the key ID and the canonical request, with the
    x-amz-security-token line, as text and as hex bytes."""
    token = lower.get("x-amz-security-token", "")
    akid = lower.get("authorization", "").split("Credential=", 1)[-1].split("/", 1)[0]
    line = f"x-amz-security-token:{token}"
    hexed = " ".join(f"{b:02x}" for b in line.encode())
    return (
        f"<Error><Code>SignatureDoesNotMatch</Code><AWSAccessKeyId>{akid}</AWSAccessKeyId>"
        f"<CanonicalRequest>{line}</CanonicalRequest><CanonicalRequestBytes>{hexed}</CanonicalRequestBytes></Error>"
    ).encode()


def _self_signed(tmp: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake-aws.local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp / "endpoint.crt", tmp / "endpoint.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- fixtures ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("e2e")
    cert, key = _self_signed(tmp)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    endpoint = Endpoint(("127.0.0.1", 0), context)
    threading.Thread(target=endpoint.serve_forever, daemon=True).start()

    proxy_port = _free_port()
    confdir = tmp / "mitmproxy"
    log_path = tmp / "mitmdump.log"
    # Block-buffered stdout, as in the sidecar (mitmdump writes to a pipe there, to a file here):
    # the addon's own flush is what makes its lines show up at once.
    env = {**os.environ, "OSB_E2E_UPSTREAM_PORT": str(endpoint.server_address[1])}
    env.pop("PYTHONUNBUFFERED", None)
    vault = None
    if SYSTEM_PY is not None:
        sock_dir = Path(tempfile.mkdtemp(prefix="osbv", dir="/tmp"))  # AF_UNIX paths are short
        vault = VaultServer(str(sock_dir / "active.sock"), VaultHandler)
        threading.Thread(target=vault.serve_forever, daemon=True).start()
        env["OPENSANDBOX_CREDENTIAL_PROXY_SOCKET"] = str(sock_dir / "active.sock")
        scripts = [str(SYSTEM_PY), str(ADDON), str(UPSTREAM_ADDON)]
    else:
        scripts = [str(SYSTEM_RULE_ADDON), str(ADDON), str(UPSTREAM_ADDON)]
    # The venv's mitmdump sits next to its python; the egress image has it on PATH (/usr/local/bin).
    sibling = Path(sys.executable).parent / "mitmdump"
    mitmdump = sibling if sibling.exists() else Path(shutil.which("mitmdump") or "mitmdump")
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            [
                str(mitmdump), "--listen-host", "127.0.0.1", "--listen-port", str(proxy_port),
                "--set", f"confdir={confdir}", "--set", "connection_strategy=lazy",
                "--set", "stream_large_bodies=1m", "--set", "flow_detail=0", "--ssl-insecure",
                *[arg for script in scripts for arg in ("-s", script)],
            ],
            stdout=log, stderr=subprocess.STDOUT, env=env,
        )
    ca = confdir / "mitmproxy-ca-cert.pem"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", proxy_port), timeout=0.5).close()
            if ca.exists():
                break
        except OSError:
            pass
        time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail("mitmdump did not start:\n" + log_path.read_text())
    yield {"endpoint": endpoint, "proxy_port": proxy_port, "ca": ca, "log": log_path, "system_py": SYSTEM_PY}
    proc.terminate()
    proc.wait(timeout=10)
    endpoint.shutdown()
    if vault is not None:
        vault.shutdown()
        shutil.rmtree(sock_dir, ignore_errors=True)


def send(stack, build, sni: str | None = None, read_stream: bool = False):
    """Send the request `build()` describes through the proxy.

    Returns (status, response headers, upstream requests this call caused, first stream part)."""
    flow, body = build()
    req = flow.request
    headers = list(req.headers.items(multi=True))
    if stack["system_py"] is not None:  # the real system.py injects the carriers from the vault
        headers = [(k, v) for k, v in headers if k.lower() not in h.CARRIER_NAMES]
    stack["last_body"], stack["last_complete"] = b"", False
    chunked = any(k.lower() == "transfer-encoding" for k, _ in headers)
    endpoint = stack["endpoint"]
    before = len(endpoint.received)
    context = ssl.create_default_context(cafile=str(stack["ca"]))
    conn = http.client.HTTPSConnection("127.0.0.1", stack["proxy_port"], context=context, timeout=15)
    conn.set_tunnel(sni or flow.client_conn.sni, 443)  # the TLS SNI is the tunnel host
    status, response_headers, first = "closed", {}, None
    try:
        conn.putrequest(req.method, req.path, skip_host=True, skip_accept_encoding=True)
        for name, value in headers:
            conn.putheader(name, value)
        if chunked:
            conn.endheaders()
            for i in range(0, len(body), 64 * 1024):
                part = body[i : i + 64 * 1024]
                conn.send(f"{len(part):x}\r\n".encode() + part + b"\r\n")
            conn.send(b"0\r\n\r\n")
        else:
            conn.endheaders(message_body=body)
        response = conn.getresponse()
        status, response_headers = response.status, dict(response.getheaders())
        if read_stream:
            first = response.read(len(STREAM_PART_1))
            endpoint.stream_release.set()
        try:
            stack["last_body"], stack["last_complete"] = response.read(), True
        except http.client.IncompleteRead as exc:  # killed after the headers went out
            stack["last_body"] = exc.partial
    except (http.client.HTTPException, OSError):
        pass  # the proxy closed the connection: the flow was killed
    finally:
        conn.close()
    time.sleep(0.05)
    return status, response_headers, endpoint.received[before:], first


# --- tests -------------------------------------------------------------------------------------


def test_e2e_log_line_reaches_the_log_after_one_request(stack):
    """First test on purpose: after a single request, with mitmdump's stdout block-buffered, the
    re-sign line is already in its output, and in the shape the egress process forwards
    (`[time] credential proxy: ...`, like system.py's own lines). Part 4 saw 0 lines after one
    request before the flush."""
    send(stack, sts_call)
    line = re.compile(r"^\[\d\d:\d\d:\d\d\.\d{3}\] credential proxy: \[sigv4\] re-signed POST sts\.us-east-1\.amazonaws\.com/ ", re.M)
    assert line.search(stack["log"].read_text(errors="replace")) is not None


def test_e2e_sts_resigned_and_verified(stack):
    status, _, upstream, _ = send(stack, sts_call)
    assert (status, [r["ok"] for r in upstream]) == (200, [True])


def test_e2e_s3_streamed_upload_verified(stack):
    """5 MiB aws-chunked PUT: signed in requestheaders, streamed, accepted by the endpoint."""
    status, _, upstream, _ = send(stack, s3_put_chunked)
    assert (status, [r["ok"] for r in upstream]) == (200, [True])


@pytest.mark.parametrize(
    "build",
    [lambda: dynamodb_put(size=2 * h.MIB), lambda: dynamodb_put(size=2 * h.MIB, chunked=True)],
    ids=["content-length", "chunked"],
)
def test_e2e_large_non_s3_body_never_reaches_aws(stack, build):
    status, _, upstream, _ = send(stack, build)
    assert (status, upstream) == ("closed", [])


def test_e2e_host_spoof_never_reaches_aws(stack):
    status, _, upstream, _ = send(stack, sts_call, sni=f"s3.us-east-1.amazonaws.com")
    assert (status, upstream) == ("closed", [])


def test_e2e_vending_refused_locally(stack):
    build = lambda: sts_call("AssumeRole", "&RoleArn=arn%3Aaws%3Aiam%3A%3A123456789012%3Arole%2Fx&RoleSessionName=s")
    status, headers, upstream, _ = send(stack, build)
    assert (status, headers.get("x-osb-egress-refused"), upstream) == (403, "credential-vending", [])


def test_e2e_refusal_of_a_streamed_body_is_a_kill(stack):
    """A 5 MiB signed-chunk PUT: refused, and as a kill, since a local response would crash the flow."""
    build = lambda: h.client_flow(
        "PUT", S3_BUCKET, "/sc.bin", service="s3", body=b"x" * (5 * h.MIB),
        content_sha="STREAMING-AWS4-HMAC-SHA256-PAYLOAD",
    )
    status, _, upstream, _ = send(stack, build)
    assert (status, upstream) == ("closed", [])


def test_e2e_presigned_forwarded_unchanged(stack):
    """A presigned GET reaches the endpoint exactly as sent: same query, no carriers, no Authorization."""
    target = f"/k?{SIGV4_QUERY}"
    _, _, upstream, _ = send(stack, lambda: h.client_flow("GET", S3_BUCKET, target, service="s3", sign=False))
    seen = [
        (r["target"], sorted(n.lower() for n, _ in r["headers"] if n.lower() in h.CARRIER_NAMES or n.lower() == "authorization"))
        for r in upstream
    ]
    assert seen == [(target, [])]


def test_e2e_error_body_redacted(stack):
    """An AWS error echoing the real key ID and token reaches the client with both scrubbed."""
    build = lambda: h.client_flow("GET", S3_BUCKET, "/echo-error", service="s3", content_sha=EMPTY_SHA)
    status, _, upstream, _ = send(stack, build)
    body = stack["last_body"]
    forms = (h.REAL_TOKEN.encode(), h.REAL_AKID.encode(), " ".join(f"{b:02x}" for b in h.REAL_TOKEN.encode()).encode())
    assert (status, [r["ok"] for r in upstream], [f for f in forms if f in body], body.count(b"[REDACTED]")) == (
        403, [True], [], 3
    )


def leaked(body: bytes) -> list[str]:
    forms = {
        "token": h.REAL_TOKEN.encode(),
        "key-id": h.REAL_AKID.encode(),
        "token-hex": " ".join(f"{b:02x}" for b in h.REAL_TOKEN.encode()).encode(),
    }
    return [name for name, form in forms.items() if form in body]


def test_e2e_chunked_error_body_redacted(stack):
    """Review N-1: system.py streams this chunked 403 (its responseheaders runs first); the client
    still gets it whole and scrubbed."""
    build = lambda: h.client_flow("GET", S3_BUCKET, "/echo-error-chunked", service="s3", content_sha=EMPTY_SHA)
    status, _, upstream, _ = send(stack, build)
    body = stack["last_body"]
    assert (status, [r["ok"] for r in upstream], leaked(body), body.count(b"[REDACTED]"), stack["last_complete"]) == (
        403, [True], [], 3, True
    )


def test_e2e_streamed_content_length_error_body_redacted(stack):
    """system.py streams any text/event-stream response; the addon undoes that for a signed 4xx
    with a Content-Length up to 1 MiB, buffers it and scrubs it."""
    build = lambda: h.client_flow("GET", S3_BUCKET, "/echo-error-event-stream", service="s3", content_sha=EMPTY_SHA)
    status, _, upstream, _ = send(stack, build)
    body = stack["last_body"]
    assert (status, [r["ok"] for r in upstream], leaked(body), body.count(b"[REDACTED]")) == (403, [True], [], 3)


def test_e2e_chunked_error_body_over_1mib_killed(stack):
    """Too large to hold: the status goes out, no byte of the body does, and the flow is killed."""
    build = lambda: h.client_flow("GET", S3_BUCKET, "/echo-error-chunked-big", service="s3", content_sha=EMPTY_SHA)
    status, _, upstream, _ = send(stack, build)
    assert (status, [r["ok"] for r in upstream], stack["last_body"], stack["last_complete"]) == (403, [True], b"", False)


def test_e2e_content_length_error_body_over_1mib_killed(stack):
    """A Content-Length over 1 MiB: killed before the status line, nothing reaches the client."""
    build = lambda: h.client_flow("GET", S3_BUCKET, "/echo-error-big", service="s3", content_sha=EMPTY_SHA)
    status, _, upstream, _ = send(stack, build)
    assert (status, [r["ok"] for r in upstream], stack["last_body"]) == ("closed", [True], b"")


def test_e2e_carriers_injected_by_the_real_vault(stack):
    """With OpenSandbox's system.py loaded, the carriers came from the vault, not the client."""
    if stack["system_py"] is None:
        pytest.skip("OpenSandbox system.py is not available here (see the module docstring)")
    assert "credential proxy: applied binding=aws-sigv4" in stack["log"].read_text(errors="replace")


def test_e2e_eventstream_response_streamed(stack):
    """ConverseStream: the first frame reaches the client while the endpoint holds the rest back."""

    def build():
        flow, body = bedrock_converse()
        flow.request.path = flow.request.path.replace("/converse", "/converse-stream")
        return flow, body

    _, _, upstream, first = send(stack, build, read_stream=True)
    released = stack["endpoint"].stream_released_by_client
    assert ([r["ok"] for r in upstream], first, released) == ([True], STREAM_PART_1, [True])


def test_e2e_log_shows_resign_and_no_secret(stack):
    text = stack["log"].read_text(errors="replace")
    leaked = [s for s in (h.REAL_SECRET, h.REAL_TOKEN, h.REAL_AKID) if s in text]
    assert ("credential proxy: [sigv4] re-signed" in text, "Traceback" in text, leaked) == (True, False, [])
