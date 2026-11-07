"""Test harness: build sandbox requests and drive them through mitmproxy's request path.

Two parts:

1. `client_flow()` builds the flow the egress sidecar sees for one request from the sandbox:
   the SDK signed it with the placeholder key, then the Credential Vault (system.py, which runs
   before the addon) injected the three carrier headers. In transparent mode the flow's host is
   the IP the sandbox dialed; the Host header and the TLS SNI are what the sandbox chose.

2. `drive()` replays mitmproxy 11.0.2's HTTP request path around the addon's hooks, so a test
   sees what would actually go upstream, and when. Modelled on
   mitmproxy/proxy/layers/http/__init__.py (11.0.2):
     - before `requestheaders`, a Content-Length above stream_large_bodies (1m in the egress
       config.yaml) sets `request.stream = True` (check_body_size, the "early" case);
     - if the request is streamed after `requestheaders`, its headers go upstream right away and
       `request` fires only after the whole body has been sent (start_request_stream);
       a local response on a streamed flow raises NotImplementedError;
     - otherwise the body is buffered; a chunked (or HTTP/2, no Content-Length) body that grows
       past the threshold switches to streaming mid-upload and sends the headers then, with no
       further hook (check_body_size, the "late" case);
     - right after `requestheaders`, mitmproxy answers `Expect: 100-continue` itself and removes
       the header, so it never reaches AWS;
     - a buffered request runs `request`, then goes upstream, or the local response is sent;
     - a killed flow (after any hook) sends nothing upstream.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import time
import zlib
from dataclasses import dataclass, field

from mitmproxy import http
from mitmproxy.flow import Error
from mitmproxy.test import tflow

import verifier

MIB = 1024 * 1024
STREAM_LARGE_BODIES = MIB  # egress image: /var/lib/mitmproxy/.mitmproxy/config.yaml

# Placeholder credentials the sandbox holds. The key is deliberately not secret-shaped:
# it must not match the secret scan's AKIA[0-9A-Z]{16} (pre-flight ruling C6).
PLACEHOLDER_AKID = "AKIA_OSB_PLACEHOLDER"
PLACEHOLDER_SECRET = "osb-placeholder-secret-not-real"
PLACEHOLDER_TOKEN = "osb-placeholder-session-token"

# The "real" temporary credentials the vault injects. Test values only, not secret-shaped either.
REAL_AKID = "ASIA_OSB_TEST_ONLY"
REAL_SECRET = "osb-test-secret-not-a-real-key"
REAL_TOKEN = "osb-test/session+token=not-real"  # "/", "+", "=" like a real STS token

CARRIER_NAMES = (
    "x-osb-aws-access-key-id",
    "x-osb-aws-secret-access-key",
    "x-osb-aws-session-token",
)
DIALED_IP = "192.0.2.10"  # TEST-NET-1: the address the sandbox connected to
# Headers an SDK leaves out of SignedHeaders (botocore's list, plus framing headers).
CLIENT_UNSIGNED = {"user-agent", "expect", "x-amzn-trace-id", "content-length", "transfer-encoding"}


def amz_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def client_flow(
    method: str,
    host: str,
    path: str = "/",
    *,
    service: str,
    region: str = "us-east-1",
    headers: list[tuple[str, str]] | tuple = (),
    body: bytes = b"",
    content_sha: str | None = None,
    chunked: bool = False,
    http2: bool = False,
    tls: bool = True,
    sni: str | None | type(...) = ...,
    host_header: str | None = None,
    carriers: bool | dict = True,
    sign: bool = True,
    auth: str | None = None,
    placeholder_token: bool = False,
    client_unsigned: set[str] | None = None,
) -> tuple[http.HTTPFlow, bytes]:
    """Return (flow as the addon sees it in `requestheaders`, request body)."""
    amz_date = amz_now()
    hdrs: list[tuple[str, str]] = []
    if not http2:
        hdrs.append(("Host", host_header or host))
    hdrs.append(("X-Amz-Date", amz_date))
    hdrs.append(("User-Agent", "aws-cli/2.39.3 test"))
    hdrs.extend(headers)
    if content_sha is not None:
        hdrs.append(("X-Amz-Content-SHA256", content_sha))
    if placeholder_token:
        hdrs.append(("X-Amz-Security-Token", PLACEHOLDER_TOKEN))
    if chunked:
        if not http2:
            hdrs.append(("Transfer-Encoding", "chunked"))
    elif body or method in ("POST", "PUT"):
        hdrs.append(("Content-Length", str(len(body))))

    if auth is not None:
        hdrs.append(("Authorization", auth))
    elif sign:
        unsigned = CLIENT_UNSIGNED if client_unsigned is None else client_unsigned
        hdrs.append(("Authorization", _placeholder_signature(method, host_header or host, path, hdrs, body, content_sha, service, region, amz_date, unsigned)))

    if carriers:
        values = carriers if isinstance(carriers, dict) else {
            CARRIER_NAMES[0]: REAL_AKID,
            CARRIER_NAMES[1]: REAL_SECRET,
            CARRIER_NAMES[2]: REAL_TOKEN,
        }
        hdrs.extend((name, value) for name, value in values.items())

    request = http.Request(
        host=DIALED_IP,
        port=443 if tls else 80,
        method=method.encode(),
        scheme=b"https" if tls else b"http",
        authority=(host_header or host).encode() if http2 else b"",
        path=path.encode(),
        http_version=b"HTTP/2.0" if http2 else b"HTTP/1.1",
        headers=http.Headers([(k.encode(), v.encode()) for k, v in hdrs]),
        content=None,  # headers only: the body has not arrived when requestheaders fires
        trailers=None,
        timestamp_start=time.time(),
        timestamp_end=None,
    )
    client = tflow.tclient_conn()
    client.sni = (host if tls else None) if sni is ... else sni
    client.alpn = b"h2" if http2 else b"http/1.1"
    server = tflow.tserver_conn()
    server.address = (DIALED_IP, request.port)
    return tflow.tflow(client_conn=client, server_conn=server, req=request), body


def _placeholder_signature(method, host, path, hdrs, body, content_sha, service, region, amz_date, unsigned) -> str:
    """What the SDK in the sandbox sends: a valid SigV4 signature made with the placeholder key."""
    names = sorted({k.lower() for k, _ in hdrs} - unsigned | {"host"})
    raw_path, _, raw_query = path.partition("?")
    sign_headers = [(k, v) for k, v in hdrs] + ([] if any(k.lower() == "host" for k, _ in hdrs) else [("host", host)])
    payload = content_sha or hashlib.sha256(body).hexdigest()
    creq = verifier.build_canonical_request(method, raw_path, raw_query, sign_headers, names, payload, service)
    sig = verifier.signature(PLACEHOLDER_SECRET, amz_date, region, service, creq)
    return (
        f"AWS4-HMAC-SHA256 Credential={PLACEHOLDER_AKID}/{amz_date[:8]}/{region}/{service}/aws4_request, "
        f"SignedHeaders={';'.join(names)}, Signature={sig}"
    )


def aws_chunked(data: bytes, chunk: int = 64 * 1024) -> bytes:
    """An aws-chunked body with unsigned chunks and a CRC32 trailer (what boto3 1.36+ sends)."""
    out = bytearray()
    for i in range(0, len(data), chunk):
        part = data[i : i + chunk]
        out += f"{len(part):x}\r\n".encode() + part + b"\r\n"
    crc = base64.b64encode(zlib.crc32(data).to_bytes(4, "big")).decode()
    out += b"0\r\n" + f"x-amz-checksum-crc32:{crc}\r\n".encode() + b"\r\n"
    return bytes(out)


@dataclass
class Sent:
    """What mitmproxy sends upstream for the request."""

    method: str
    scheme: str
    host: str  # the upstream mitmproxy connects to
    port: int
    path: str
    query: str
    headers: list[tuple[str, str]]
    body: bytes

    def header_names(self) -> set[str]:
        return {k.lower() for k, _ in self.headers}


@dataclass
class Outcome:
    kind: str  # "forwarded", "killed", "refused" (local response) or "error" (mitmproxy raises)
    sent: Sent | None
    status: int | None
    flow: http.HTTPFlow
    events: list[str] = field(default_factory=list)


def killed(flow: http.HTTPFlow) -> bool:
    return flow.error is not None and flow.error.msg == Error.KILLED_MESSAGE


def _snapshot(flow: http.HTTPFlow, body: bytes) -> Sent:
    req = flow.request
    headers = [(k, v) for k, v in req.headers.items(multi=True)]
    if not any(k.lower() == "host" for k, _ in headers) and req.authority:
        headers.insert(0, ("host", req.authority))  # an HTTP/1 upstream gets :authority as Host
    raw_path, _, query = req.path.partition("?")
    return Sent(req.method, req.scheme, req.host, req.port, raw_path, query, headers, body)


def drive(addon, flow: http.HTTPFlow, body: bytes, threshold: int = STREAM_LARGE_BODIES) -> Outcome:
    """Run the addon's request hooks the way mitmproxy 11.0.2 does; see the module docstring."""
    req = flow.request
    has_body = bool(body)
    events: list[str] = []

    content_length = req.headers.get("content-length")
    if has_body and content_length is not None and int(content_length) > threshold:
        req.stream = True  # check_body_size, early case: before the requestheaders hook

    addon.requestheaders(flow)
    events.append("requestheaders")
    if killed(flow):
        return Outcome("killed", None, None, flow, events)
    if req.headers.get("expect", "").lower() == "100-continue":
        req.headers.pop("expect")  # mitmproxy answers 100 Continue itself and drops the header

    if req.stream and has_body:
        if flow.response is not None:
            return Outcome("error", None, None, flow, events)  # start_request_stream raises
        sent = _snapshot(flow, body)  # headers go upstream now, body streams after them
        events.append("sent")
        addon.request(flow)  # fires once the body has been streamed: too late to change headers
        events.append("request")
        return Outcome("forwarded", sent, None, flow, events)

    if has_body and len(body) > threshold:
        # check_body_size, late case: buffered bytes crossed the threshold mid-upload. It measures
        # the buffer whatever the Content-Length says, so a body forced back to buffering flips too.
        req.stream = True
        if flow.response is not None:
            return Outcome("error", None, None, flow, events)
        sent = _snapshot(flow, body)
        events.append("sent")
        addon.request(flow)
        events.append("request")
        return Outcome("forwarded", sent, None, flow, events)

    req.data.content = body
    addon.request(flow)
    events.append("request")
    if killed(flow):
        return Outcome("killed", None, None, flow, events)
    if flow.response is not None:
        addon.responseheaders(flow)  # mitmproxy emulates responseheaders for a local response
        return Outcome("refused", None, flow.response.status_code, flow, events)
    events.append("sent")
    return Outcome("forwarded", _snapshot(flow, req.raw_content or b""), None, flow, events)


def verify_sent(outcome: Outcome, **kwargs) -> verifier.Result:
    """Would AWS accept what went upstream? Checks the real credentials and no carriers."""
    if outcome.kind != "forwarded" or outcome.sent is None:
        return verifier.Result(False, f"nothing was forwarded: {outcome.kind} {outcome.status}")
    s = outcome.sent
    return verifier.verify(
        s.method,
        s.path,
        s.query,
        s.headers,
        s.body,
        access_key=REAL_AKID,
        secret_key=REAL_SECRET,
        session_token=REAL_TOKEN,
        **kwargs,
    )


# --- the response path ------------------------------------------------------------------------


@dataclass
class ResponseOutcome:
    """What the sandbox gets back.

    kind: "sent" (buffered, then sent whole), "streamed" (headers, then the body as it came
    through), "killed" (nothing at all), "killed-after-headers" (status and headers, then
    `delivered`, then the connection closes), or "ended-by-empty-chunk" (a chunked response in
    which an empty data event went out: mitmproxy's HTTP/1 layer writes it as `0\\r\\n\\r\\n`, the
    terminator, so the client takes `delivered` as the whole body)."""

    kind: str
    status: int | None
    delivered: bytes  # body bytes as on the wire to the sandbox (still content-encoded)
    flow: http.HTTPFlow


def system_py_responseheaders(flow: http.HTTPFlow) -> None:
    """OpenSandbox 1.2.0 system.py's responseheaders streaming rule (system.py:1910-1917),
    which runs before the addon's hook: every chunked or event-stream response is streamed."""
    content_type = flow.response.headers.get("content-type", "").lower()
    transfer_encoding = flow.response.headers.get("transfer-encoding", "").lower()
    if "text/event-stream" in content_type or "chunked" in transfer_encoding:
        flow.response.stream = True


def drive_response(
    addon,
    flow: http.HTTPFlow,
    status: int,
    headers: list[tuple[str, str]],
    body: bytes,
    *,
    system_py: bool = True,
    chunk: int = 64 * 1024,
    threshold: int = STREAM_LARGE_BODIES,
    parts: list[bytes] | None = None,
    http_version: str = "HTTP/1.1",
) -> ResponseOutcome:
    """Replay mitmproxy 11.0.2's response path around the addon's hooks
    (proxy/layers/http/__init__.py: state_wait_for_response_headers, start_response_stream,
    state_stream_response_body, state_consume_response_body, send_response, check_body_size):
      - a Content-Length over the threshold sets response.stream before responseheaders;
      - system.py's responseheaders runs first, then the addon's;
      - a killed flow sends nothing;
      - a streamed response sends its headers, then each chunk (through response.stream if it is
        callable, which is also called with b"" at the end), then `response` fires. A callable's
        return value is a bytes chunk or a list of them (state_stream_response_body); each one is
        a data event, and on a chunked response an empty one ends the body (_http1.py:248-253);
      - a buffered body that grows past the threshold switches to streaming mid-body and goes out
        raw, with no further hook until `response` (check_body_size measures the buffer, whatever
        the Content-Length says);
      - a buffered body runs `response`, then is sent whole, unless the flow was killed.
    `parts` gives the data events explicitly (an empty one is an HTTP/2 zero-length DATA frame)."""
    response = http.Response(
        http_version=http_version.encode(),
        status_code=status,
        reason=b"X",
        headers=http.Headers([(k.encode(), v.encode()) for k, v in headers]),
        content=None,
        trailers=None,
        timestamp_start=time.time(),
        timestamp_end=None,
    )
    flow.response = response
    length = response.headers.get("content-length")
    if body and length is not None and int(length) > threshold:
        response.stream = True
    if system_py:
        system_py_responseheaders(flow)
    addon.responseheaders(flow)
    if killed(flow):
        return ResponseOutcome("killed", None, b"", flow)

    if parts is None:
        parts = [body[i : i + chunk] for i in range(0, len(body), chunk)]
    body = b"".join(parts)

    chunked = "chunked" in response.headers.get("transfer-encoding", "").lower()

    def streamed(rest: list[bytes], already: bytes = b"") -> ResponseOutcome:
        out = bytearray(already)
        events = [[part] if not callable(response.stream) else response.stream(part) for part in rest]
        if callable(response.stream):
            last = response.stream(b"")
            events.append([] if last == b"" else last)  # mitmproxy drops a bare b"" only at the end
        for event in events:
            for data in [event] if isinstance(event, bytes) else event:
                if chunked and data == b"":
                    return ResponseOutcome("ended-by-empty-chunk", status, bytes(out), flow)
                out += data
        addon.response(flow)
        return ResponseOutcome("killed-after-headers" if killed(flow) else "streamed", status, bytes(out), flow)

    if response.stream and body:
        return streamed(parts)
    buffered = bytearray()
    for i, part in enumerate(parts):
        buffered += part
        if len(buffered) > threshold:
            response.stream = True  # check_body_size, late case: the buffer goes out raw
            return streamed(parts[i + 1 :], bytes(buffered))
    response.data.content = bytes(buffered)
    addon.response(flow)
    if killed(flow):
        return ResponseOutcome("killed", None, b"", flow)
    return ResponseOutcome("sent", status, response.raw_content or b"", flow)
