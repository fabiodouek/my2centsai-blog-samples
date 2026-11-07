"""SigV4 re-signing addon for the OpenSandbox egress sidecar.

Part 5 of "OpenSandbox on Amazon EKS". The sandbox never holds AWS credentials:

  1. A trusted broker assumes the sandbox's IAM role and writes the temporary credentials into
     the sandbox's Credential Vault, with a customHeaders binding on the AWS hosts.
  2. For a request to a bound host, the vault (system.py, which mitmproxy runs before this addon)
     adds the credentials as three carrier headers (CARRIERS).
  3. The SDK in the sandbox signed the request with a placeholder key. This addon removes the
     carriers, drops the placeholder signature and signs the request again with the real
     temporary credentials.

Rules, all fail closed (when in doubt the request does not leave the sidecar):
  - The carriers are removed from every flow, before anything else, so they never go upstream.
    All three must be present, once each, or the request is refused.
  - A request with a second Host, Authorization, x-amz-content-sha256 or X-Amz-Target header, or
    a comma in X-Amz-Target, is refused (403): a server may act on a value the addon did not check.
  - The vault matches bindings on the Host header, which the sandbox controls (#1758). A flow
    with carriers must have Host == TLS SNI and an SNI ending in .amazonaws.com, else it is
    killed; the upstream is then pinned to the SNI hostname, not the IP the sandbox dialed.
  - Presigned (query-string auth) requests are forwarded as they came, minus the carriers, and
    never re-signed: a URL presigned on the trusted side is already authorized, and one presigned
    in the sandbox carries the placeholder key, which AWS rejects.
  - Signed-chunk uploads and SigV4a: 501. A Bearer request on a SigV4 binding: 403. A credential-vending call (sts:AssumeRole*, GetSessionToken,
    GetFederationToken, s3express:CreateSession, ecr/codeartifact GetAuthorizationToken, ...):
    403, before anything is signed. A refusal is a local response with that status and an
    `x-osb-egress-refused: <reason>` header; when the body is being streamed, mitmproxy cannot
    answer locally, so the flow is killed instead.
  - S3 declares its payload hash in x-amz-content-sha256, so S3 is signed in `requestheaders`
    and streamed. Every other service is signed in `request`, from the SHA-256 of the buffered
    body. mitmproxy streams a body over stream_large_bodies (1m in the egress image) and sends its
    headers before `request` fires, so a non-S3 request whose body may stream is killed.
  - Any exception on a flow that carried carriers kills the flow.
  - Event-stream responses (Bedrock ConverseStream) are streamed to the sandbox as they arrive.
  - AWS error bodies can echo the real key ID and session token (S3 SignatureDoesNotMatch). On
    flows this addon signed, every 4xx/5xx body is held back and has them replaced by [REDACTED]
    before any of it reaches the sandbox, even though system.py streams chunked responses: a
    Content-Length body up to 1 MiB is buffered, a length-less (chunked) one goes through a
    buffering stream callable. A body that cannot be scrubbed (over 1 MiB, undecodable) is never
    passed on: the flow is killed, with a `killed ... reason=error-body-...` line.

Region and service come from the client's own credential scope, not from the hostname
(bedrock-runtime.* signs as "bedrock", sts.amazonaws.com as us-east-1).

Log lines, never with a secret. The egress process copies mitmdump's output into the sidecar's
log only for lines that start with `credential proxy:` (system.py's own prefix), and mitmdump's
stdout to it is block-buffered; so every line starts with `credential proxy: [sigv4]`, is logged
at WARNING, and is flushed at once:
  credential proxy: [sigv4] re-signed <METHOD> <host><path> service=<s> region=<r> key=<4 chars>... payload=<p> hook=<h>
  credential proxy: [sigv4] refused <status> <METHOD> <host><path> reason=<reason> [service=<s>] [header=<name>]
  credential proxy: [sigv4] killed [<status>] <METHOD> <host><path> reason=<reason> [service=<s>] [sni=<sni>] [error=<Type>]
  credential proxy: [sigv4] unsigned <METHOD> <host><path> reason=no-authorization
  credential proxy: [sigv4] presigned <METHOD> <host><path> reason=query-auth-forwarded-unchanged
  credential proxy: [sigv4] redacted <status> <METHOD> <host><path> reason=credential-echo-in-error-body

Standard library only, plus the mitmproxy that loads it (11.0.2 on Debian 12's python3 3.11 in
opensandbox/egress:release-1.2.0). Baked into a derived egress image and enabled with
OPENSANDBOX_EGRESS_MITMPROXY_SCRIPT; it cannot be set from a sandbox create request.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import logging
import re
import sys
import time
import urllib.parse
from typing import NamedTuple

from mitmproxy import http
from mitmproxy.flow import Error
from mitmproxy.net import encoding as content_encoding

CARRIERS = (
    "x-osb-aws-access-key-id",
    "x-osb-aws-secret-access-key",
    "x-osb-aws-session-token",
)

SNI_SUFFIX = ".amazonaws.com"
S3_SERVICES = frozenset({"s3", "s3-object-lambda"})
S3_PAYLOAD_LITERALS = frozenset({"UNSIGNED-PAYLOAD", "STREAMING-UNSIGNED-PAYLOAD-TRAILER"})
SIGNED_CHUNK_PREFIXES = ("STREAMING-AWS4-HMAC-SHA256-PAYLOAD", "STREAMING-AWS4-ECDSA-P256-SHA256-PAYLOAD")
HEX64 = re.compile(r"[0-9a-f]{64}")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# Query-string authentication: SigV4 presigned URLs and SigV2 (boto3's default S3 presign).
QUERY_AUTH_PARAMS = frozenset({"x-amz-signature", "x-amz-credential", "awsaccesskeyid", "signature"})

# Credential vending. STS is an allowlist: every other STS action returns credentials or tokens,
# and an STS call whose action cannot be found is refused. The rest is a blocklist. This is
# defense in depth: the sandbox role and the session policy carry an explicit Deny as well.
STS_ALLOWED_ACTIONS = frozenset({"getcalleridentity", "getaccesskeyinfo", "decodeauthorizationmessage"})
VENDING_ACTIONS = {
    "s3express": frozenset({"createsession"}),
    "ecr": frozenset({"getauthorizationtoken"}),
    "ecr-public": frozenset({"getauthorizationtoken"}),
    "codeartifact": frozenset({"getauthorizationtoken"}),
    "iam": frozenset({"createaccesskey", "createservicespecificcredential", "resetservicespecificcredential"}),
    "redshift": frozenset({"getclustercredentials", "getclustercredentialswithiam"}),
    "redshift-serverless": frozenset({"getcredentials"}),
}
VENDING_PATHS = {"codeartifact": ("/v1/authorization-token",)}  # REST-JSON: the action is the path
RPC_V2_PATH = re.compile(r"/service/[^/]+/operation/([^/?]+)")  # smithy rpcv2-cbor
# Headers the addon decides on: a request carrying any of them twice is refused, since a server
# may read the first value while a folded "A, B" read sees the last.
SINGLE_VALUE_HEADERS = ("host", "authorization", "x-amz-content-sha256", "x-amz-target")

AUTH_RE = re.compile(
    r"AWS4-HMAC-SHA256\s+Credential=(?P<akid>[^/\s,]+)/(?P<date>\d{8})/(?P<region>[a-z0-9-]+)/"
    r"(?P<service>[a-z0-9-]+)/aws4_request\s*,\s*SignedHeaders=(?P<signed>[a-z0-9!#$%&'*+.^_`|~;-]+)"
    r"\s*,\s*Signature=[0-9a-f]{64}"
)

# Replaced on every re-signed request.
DROPPED = ("authorization", "x-amz-date", "x-amz-security-token")
# Never signed: mitmproxy or the hop to AWS may change them after this addon runs
# (mitmproxy answers and removes `Expect: 100-continue` right after requestheaders).
UNSIGNABLE = frozenset(
    {
        "authorization",
        "connection",
        "expect",
        "keep-alive",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

PENDING = "osb_sigv4_pending"  # flow.metadata key: (Creds, scope) waiting for the body
REDACT = "osb_sigv4_redact"  # flow.metadata key: real credential values to scrub from error bodies
MAX_REDACT_BODY = 1024 * 1024  # = stream_large_bodies (1m) in the egress image's config.yaml
# Escapes a serializer may apply to the non-alphanumeric characters of an STS token or key.
JSON_ESCAPES = {"/": ("/", "\\/", "\\u002f", "\\u002F"), "+": ("+", "\\u002b", "\\u002B"), "=": ("=", "\\u003d", "\\u003D")}
XML_ESCAPES = {
    "/": ("/", "&#47;", "&#x2f;", "&#x2F;"),
    "+": ("+", "&#43;", "&#x2b;", "&#x2B;"),
    "=": ("=", "&#61;", "&#x3d;", "&#x3D;"),
}

# The root logger is what mitmproxy 11.0.2's deprecated ctx.log.warn() calls; it goes to the
# sidecar's log at WARNING, the level system.py's own warnings use.
LOG = logging.getLogger()
LOG_PREFIX = "credential proxy: [sigv4]"  # the egress process only forwards `credential proxy:` lines


class Creds(NamedTuple):
    access_key_id: str
    secret_access_key: str
    session_token: str | None

    def __repr__(self) -> str:  # never print a secret, even in a traceback
        return f"Creds(access_key_id={self.access_key_id[:4]!r}..., secret_access_key=***, session_token=***)"


# --- pure functions ------------------------------------------------------------------------


def parse_scope(authorization: str) -> dict | None:
    """The client's SigV4 credential scope, or None if the header is not SigV4 header auth.

    Returns {akid, date, region, service, signed}; `signed` is the tuple of header names the
    client signed. Region and service are the client's, which already ran the SDK's endpoint rules.
    """
    m = AUTH_RE.fullmatch((authorization or "").strip())
    if not m:
        return None
    return {
        "akid": m["akid"],
        "date": m["date"],
        "region": m["region"],
        "service": m["service"],
        "signed": tuple(m["signed"].split(";")),
    }


def is_query_auth(query: str) -> bool:
    """True for presigned or query-string auth: X-Amz-Signature/X-Amz-Credential or SigV2."""
    return any(
        name.lower() in QUERY_AUTH_PARAMS
        for name, _ in urllib.parse.parse_qsl(query or "", keep_blank_values=True)
    )


def is_credential_vending(
    service: str, headers, query: str, body: bytes | None, path: str = "", host: str = ""
) -> bool:
    """True if the call would hand the sandbox credentials or a token in the response body.

    `body` is None when it has not arrived yet (requestheaders); then only the X-Amz-Target
    header, the query string and the path are checked, and the caller checks again with the body.
    `host` (the pinned hostname) catches S3 Express CreateSession whatever scope it was signed with.
    """
    service = (service or "").lower()
    host = (host or "").lower()
    s3express_host = host.startswith("s3express-") or ".s3express-" in host
    if (service == "s3express" or s3express_host) and any(
        name.lower() == "session" for name, _ in urllib.parse.parse_qsl(query or "", keep_blank_values=True)
    ):
        return True  # CreateSession: GET /?session on a directory bucket
    path = _action_path(path)
    if service in VENDING_PATHS and any(path.startswith(p) for p in VENDING_PATHS[service]):
        return True
    if service == "sts":
        actions = _actions(headers, query, body, path)
        if not actions:
            return body is not None  # the whole request is known and names no action
        return bool(actions - STS_ALLOWED_ACTIONS)
    blocked = VENDING_ACTIONS.get(service)
    return bool(blocked and _actions(headers, query, body, path) & blocked)


def _action_path(path: str) -> str:
    """The path as a service routes it: percent-decoded (until stable), dot segments and empty
    segments removed, lowercased. Only for matching action paths, never for signing."""
    path = (path or "").partition("?")[0]
    for _ in range(4):
        decoded = urllib.parse.unquote(path)
        if decoded == path:
            break
        path = decoded
    return _remove_dot_segments(path).lower() if path else ""


def _actions(headers, query: str, body: bytes | None, path: str) -> set[str]:
    """Every action name the request carries, lowercased: X-Amz-Target (every value, every
    comma-separated part), Action= (query or form body), rpcv2 path. All of them, so a duplicated
    header or parameter cannot hide one."""
    found = []
    for target in _header_values(headers, "x-amz-target"):
        found += [part.strip().rsplit(".", 1)[-1] for part in target.split(",") if part.strip()]
    sources = [query or ""]
    if body:
        sources.append(body.decode("utf-8", "replace"))
    for source in sources:
        found += [v for k, v in urllib.parse.parse_qsl(source, keep_blank_values=True) if k.lower() == "action"]
    m = RPC_V2_PATH.match(_action_path(path))
    if m:
        found.append(m.group(1))
    return {a.strip().lower() for a in found if a.strip()}


def _header_values(headers, name: str) -> list[str]:
    """Every value of a header, unfolded (mitmproxy's .get() joins duplicates into "A, B")."""
    if hasattr(headers, "get_all"):
        return list(headers.get_all(name))
    return [v for k, v in headers.items() if k.lower() == name]


def sign(flow: http.HTTPFlow, creds, scope: dict, payload_hash: str) -> None:
    """Replace the placeholder signature with a SigV4 signature made with `creds`.

    `creds` is (access_key_id, secret_access_key, session_token). Drops Authorization,
    X-Amz-Date and X-Amz-Security-Token, stamps a fresh X-Amz-Date, adds the session token, and
    signs the client's SignedHeaders plus host, x-amz-date, x-amz-security-token and every other
    x-amz-* header present (S3 rejects unsigned ones). S3 signs the path as sent; every other
    service signs it normalized and URI-encoded a second time. The query is sorted raw, by name
    then value.
    """
    akid, secret, token = creds
    req = flow.request
    headers = req.headers
    for name in DROPPED:
        if name in headers:
            del headers[name]
    amz_date = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    date = amz_date[:8]
    headers["X-Amz-Date"] = amz_date
    if token:
        headers["X-Amz-Security-Token"] = token

    present = {name.lower() for name in headers.keys()}
    wanted = {n.lower() for n in scope["signed"]} | {"host", "x-amz-date"}
    wanted |= {n for n in present if n.startswith("x-amz-") or n == "content-md5"}
    names = sorted(n for n in wanted if n not in UNSIGNABLE and (n == "host" or n in present))

    def value(name: str) -> str:
        if name == "host":
            return req.host_header or ""  # Host, or :authority on HTTP/2
        return ",".join(" ".join(v.split()) for v in headers.get_all(name))

    raw_path, _, query = req.path.partition("?")
    service, region = scope["service"], scope["region"]
    if service in S3_SERVICES:
        canonical_uri = raw_path or "/"
    else:
        canonical_uri = urllib.parse.quote(_remove_dot_segments(raw_path or "/"), safe="/~")
    pairs = sorted(p.partition("=")[::2] for p in query.split("&") if p)
    canonical_request = "\n".join(
        [
            req.method,
            canonical_uri,
            "&".join(f"{k}={v}" for k, v in pairs),
            "".join(f"{n}:{value(n)}\n" for n in names),
            ";".join(names),
            payload_hash,
        ]
    )
    credential_scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = ("AWS4" + secret).encode()
    for part in (date, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={akid}/{credential_scope}, "
        f"SignedHeaders={';'.join(names)}, Signature={signature}"
    )


def _remove_dot_segments(path: str) -> str:
    """The path normalization the SDKs apply before signing for every service but S3."""
    out: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if out:
                out.pop()
        else:
            out.append(segment)
    first = "/" if path.startswith("/") else ""
    last = "/" if path.endswith("/") and out else ""
    return first + "/".join(out) + last


# --- mitmproxy hooks ---------------------------------------------------------------------------


def requestheaders(flow: http.HTTPFlow) -> None:
    try:
        carried = _take_carriers(flow)
    except Exception as exc:  # cannot tell whether the flow carried credentials: kill it
        _kill(flow, "error", error=type(exc).__name__)
        return
    if carried is None:
        return  # not an AWS-bound request: leave it alone (this image is server-wide)
    try:
        _requestheaders(flow, carried)
    except Exception as exc:  # fail closed
        _kill(flow, "error", error=type(exc).__name__)


def request(flow: http.HTTPFlow) -> None:
    pending = flow.metadata.pop(PENDING, None)
    if pending is None:
        return
    try:
        _sign_buffered(flow, *pending)
    except Exception as exc:  # fail closed
        _kill(flow, "error", error=type(exc).__name__)


def responseheaders(flow: http.HTTPFlow) -> None:
    response = flow.response
    if response is None:
        return
    secrets = flow.metadata.get(REDACT)
    if secrets and response.status_code >= 400:
        try:
            _hold_error_body(flow, response, secrets)
        except Exception as exc:  # fail closed: the body may hold the token
            _kill(flow, "redaction-error", error=type(exc).__name__)
        return
    try:
        if "application/vnd.amazon.eventstream" in response.headers.get("content-type", "").lower():
            response.stream = True  # ConverseStream: tokens as they arrive, not all at the end
    except Exception as exc:
        LOG.warning("%s responseheaders error=%s", LOG_PREFIX, type(exc).__name__)
        _flush_log()


def _hold_error_body(flow: http.HTTPFlow, response: http.Response, secrets: tuple[str, ...]) -> None:
    """Make sure an error body on a signed flow is scrubbed before any of it reaches the sandbox.

    system.py (it runs before this addon) streams every chunked or event-stream response, and
    mitmproxy streams any body whose Content-Length is over stream_large_bodies. A streamed body
    goes to the client chunk by chunk with no hook in between, so:
      - Content-Length over 1 MiB: kill now, before anything is sent;
      - Content-Length up to 1 MiB: buffer it (undo system.py's stream flag); `response` scrubs it;
      - no Content-Length (chunked, HTTP/2): mitmproxy would switch a body that grows past 1 MiB
        to streaming mid-body, so a _ScrubbingStream holds every chunk back instead and releases
        the scrubbed body at the end.
    """
    lengths = response.headers.get_all("content-length")
    if lengths:
        try:
            size = int(lengths[0]) if len(lengths) == 1 else -1
        except ValueError:
            size = -1
        if size < 0 or size > MAX_REDACT_BODY:
            reason = "error-body-too-large-to-scrub" if size > MAX_REDACT_BODY else "error-body-length-invalid"
            _kill(flow, reason, status=response.status_code)
            return
        response.stream = False
        return
    response.stream = _ScrubbingStream(response.headers.get("content-encoding", ""), secrets)


class _ScrubbingStream:
    """`response.stream` for a length-less error body on a signed flow.

    mitmproxy calls it with each chunk, then with b"" at the end of the body, and sends each item
    of the returned list as a data event. It returns [] (never b""), since on a chunked response
    mitmproxy writes an empty data event as `0\\r\\n\\r\\n`, which would end the body for the client.
    Nothing is passed on until the end; then the whole body goes out scrubbed (decoded and
    re-encoded with its Content-Encoding). A body over MAX_REDACT_BODY, or one that cannot be
    decoded, is never passed on: `failed` says why, and the `response` hook kills the flow.

    The first b"" is taken as the end. Over HTTP/2 it can also be a zero-length DATA frame inside
    the body, so: any body data after the release fails the scrubber (the flow is then killed),
    and the release holds back a tail of 8 bytes or more that is the start of a secret, so a
    token cut by an early end never leaves in part. (A real body ends with such a tail only when
    it ends with part of a secret.)
    """

    def __init__(self, encoding: str, secrets: tuple[str, ...]) -> None:
        self.encoding = encoding.strip()
        self.secrets = secrets
        self.buffer = bytearray()
        self.failed: str | None = None
        self.redacted = False
        self.released = False

    def __call__(self, data: bytes) -> list[bytes]:
        try:
            if self.failed:
                return []
            if self.released:
                if data:  # more body after the release: that b"" was an HTTP/2 empty DATA frame
                    self.failed = "error-body-data-after-release"
                return []
            if data:
                self.buffer += data
                if len(self.buffer) > MAX_REDACT_BODY:
                    self.failed = "error-body-too-large-to-scrub"
                    self.buffer = bytearray()
                return []
            self.released = True
            raw, self.buffer = bytes(self.buffer), bytearray()
            content = content_encoding.decode(raw, self.encoding) if self.encoding else raw
            if not isinstance(content, bytes):
                raise ValueError("Content-Encoding decoded to text")
            scrubbed = _scrub(content, self.secrets)
            self.redacted = scrubbed != content
            if not self.encoding:
                # An encoded body cut early fails to decode above; a plain one may end mid-secret.
                scrubbed = scrubbed[: len(scrubbed) - _partial_secret_suffix(scrubbed, self.secrets)]
            if scrubbed == content:
                return [raw] if raw else []
            out = content_encoding.encode(scrubbed, self.encoding) if self.encoding else scrubbed
            return [out] if out else []
        except Exception:  # undecodable or anything else: pass nothing on
            self.failed = self.failed or "error-body-unscrubbable"
            self.buffer = bytearray()
            return []


def response(flow: http.HTTPFlow) -> None:
    """Scrub the real key ID and session token from AWS error bodies on flows this addon signed.

    AWS echoes them in some errors (S3 SignatureDoesNotMatch carries AWSAccessKeyId and the
    canonical request, with the x-amz-security-token line, as text and as hex bytes). The vault
    redacts response headers only. `responseheaders` already made sure the 4xx/5xx body was held
    back (buffered, or through a _ScrubbingStream); this scrubs a buffered one, or kills the flow
    when the body could not be scrubbed.
    """
    secrets = flow.metadata.get(REDACT)
    if not secrets or flow.response is None:
        return
    try:
        _redact_error_body(flow, secrets)
    except Exception as exc:  # fail closed: the body may still hold the token
        _kill(flow, "redaction-error", error=type(exc).__name__)


def _requestheaders(flow: http.HTTPFlow, carried: dict[str, list[str]]) -> None:
    req = flow.request
    for name in SINGLE_VALUE_HEADERS:
        if len(req.headers.get_all(name)) > 1:
            _refuse(flow, 403, "ambiguous-target" if name == "x-amz-target" else "duplicate-header", header=name)
            return
    if "," in (req.headers.get("x-amz-target") or ""):
        _refuse(flow, 403, "ambiguous-target", header="x-amz-target")
        return
    sni = _pin_to_sni(flow)
    if sni is None:
        _kill(flow, "host-sni-mismatch", sni=_normalize_host(flow.client_conn.sni) or "none")
        return
    path, _, query = req.path.partition("?")
    if is_query_auth(query):
        # Presigned: authorized by whoever signed it. Forward it unchanged (the carriers are
        # already gone, the upstream is pinned); never re-sign it.
        _log("presigned", flow, reason="query-auth-forwarded-unchanged")
        return
    creds = _creds(carried)
    if creds is None:
        _refuse(flow, 403, "incomplete-carriers")
        return

    auth = req.headers.get("authorization")
    if auth is None:
        _log("unsigned", flow, reason="no-authorization")  # forwarded with no credentials
        return
    if auth.strip().lower().startswith("bearer "):
        _refuse(flow, 403, "bearer-on-sigv4-binding")
        return
    if auth.strip().startswith("AWS4-ECDSA-P256-SHA256"):
        _refuse(flow, 501, "sigv4a-not-supported")
        return
    scope = parse_scope(auth)
    if scope is None:
        _refuse(flow, 403, "unsupported-authorization")
        return
    service = scope["service"]
    if is_credential_vending(service, req.headers, query, None, path, host=sni):
        _refuse(flow, 403, "credential-vending", service=service)
        return

    content_sha = req.headers.get("x-amz-content-sha256")
    if content_sha and content_sha.strip().upper().startswith(SIGNED_CHUNK_PREFIXES):
        _refuse(flow, 501, "signed-chunk-upload", service=service)
        return
    if service in S3_SERVICES and content_sha is not None:
        if not (HEX64.fullmatch(content_sha) or content_sha in S3_PAYLOAD_LITERALS):
            _refuse(flow, 403, "unsupported-payload-hash", service=service)
            return
        # S3 declared the payload hash up front: sign now, and stream the body (any size).
        sign(flow, creds, scope, content_sha)
        req.stream = True
        _signed(flow, creds, scope, content_sha, "requestheaders")
        return

    if _may_stream(flow):
        if req.method in ("GET", "HEAD") and (req.is_http2 or req.is_http3) and not req.stream:
            # HTTP/2 sends no Content-Length for a bodiless request. Sign over the empty body;
            # if a body arrives after all, AWS rejects the signature.
            sign(flow, creds, scope, EMPTY_SHA256)
            _signed(flow, creds, scope, EMPTY_SHA256, "requestheaders")
            return
        # The headers would go upstream before the body could be hashed.
        _kill(flow, "body-may-stream-without-payload-hash", service=service)
        return

    # Sign in `request`, once the body is buffered. Remove the placeholder signature now, so
    # nothing the sandbox signed leaves early if the body unexpectedly streams.
    for name in ("authorization", "x-amz-security-token"):
        if name in req.headers:
            del req.headers[name]
    flow.metadata[PENDING] = (creds, scope)


def _sign_buffered(flow: http.HTTPFlow, creds: Creds, scope: dict) -> None:
    req = flow.request
    if flow.response is not None or _is_killed(flow):
        return  # already answered or killed by another addon: nothing goes upstream
    if req.stream:
        _kill(flow, "streamed-before-signing", service=scope["service"])
        return
    body = req.raw_content or b""  # never .content: it decodes Content-Encoding (aws-chunked raises)
    path, _, query = req.path.partition("?")
    if is_credential_vending(scope["service"], req.headers, query, body, path, host=req.host):
        _refuse(flow, 403, "credential-vending", service=scope["service"], buffered=True)
        return
    payload_hash = hashlib.sha256(body).hexdigest()
    if scope["service"] in S3_SERVICES or "x-amz-content-sha256" in req.headers:
        req.headers["x-amz-content-sha256"] = payload_hash  # S3 requires it; keep it true elsewhere
    sign(flow, creds, scope, payload_hash)
    _signed(flow, creds, scope, payload_hash, "request")


def _redact_error_body(flow: http.HTTPFlow, secrets: tuple[str, ...]) -> None:
    """Scrub a buffered error body, or kill the flow when its body could not be scrubbed."""
    resp = flow.response
    if resp.status_code < 400:
        return
    stream = resp.stream
    if isinstance(stream, _ScrubbingStream):  # scrubbed (or withheld) on its way through
        if stream.failed:
            _kill(flow, stream.failed, status=resp.status_code)
        elif stream.redacted:
            _log("redacted", flow, status=resp.status_code, reason="credential-echo-in-error-body")
        return
    if stream:  # streamed past this addon: already sent, unscrubbed
        _kill(flow, "error-body-streamed-unscrubbed", status=resp.status_code)
        return
    raw = resp.raw_content
    if raw is None or len(raw) > MAX_REDACT_BODY:
        _kill(flow, "error-body-too-large-to-scrub", status=resp.status_code)
        return
    try:
        content = resp.get_content(strict=True) or b""
    except ValueError:  # an encoding mitmproxy cannot decode: it may hide the token
        _kill(flow, "undecodable-error-body", status=resp.status_code)
        return
    scrubbed = _scrub(content, secrets)
    if scrubbed != content:
        resp.content = scrubbed  # mitmproxy re-encodes and fixes Content-Length
        _log("redacted", flow, status=resp.status_code, reason="credential-echo-in-error-body")


def _partial_secret_suffix(data: bytes, secrets: tuple[str, ...], minimum: int = 8) -> int:
    """Length of the longest tail of `data` (at least `minimum` bytes) that is the start of a
    secret in one of its forms, or 0."""
    longest = 0
    for variant in _redaction_variants(secrets):
        start = max(0, len(data) - len(variant) + 1)
        pos = data.find(variant[:1], start)
        while pos != -1 and len(data) - pos >= minimum:
            if variant.startswith(data[pos:]):
                longest = max(longest, len(data) - pos)
                break
            pos = data.find(variant[:1], pos + 1)
    return longest


def _scrub(content: bytes, secrets: tuple[str, ...]) -> bytes:
    for variant in _redaction_variants(secrets):
        content = content.replace(variant, b"[REDACTED]")
    return content


def _redaction_variants(secrets: tuple[str, ...]) -> list[bytes]:
    """Each secret as text; percent-encoded (upper- and lowercase hex); with every combination of
    JSON escapes (\\/, \\u002f) and XML character references (&#47;, &#x2f;) of its '/', '+'
    and '='; and as hex bytes, with or without spaces (S3's *Bytes fields), in either case."""
    out: set[bytes] = set()
    for value in secrets:
        if not value or len(value) < 8:
            continue  # never scrub short strings out of a body
        raw = value.encode()
        percent = urllib.parse.quote(value, safe="")
        out |= {
            raw,
            percent.encode(),
            re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), percent).encode(),
            raw.hex().encode(),
            raw.hex().upper().encode(),
            " ".join(f"{b:02x}" for b in raw).encode(),
            " ".join(f"{b:02X}" for b in raw).encode(),
        }
        for table in (JSON_ESCAPES, XML_ESCAPES):
            chars = [c for c in table if c in value]
            for choice in itertools.product(*(table[c] for c in chars)):
                mapping = dict(zip(chars, choice))
                out.add("".join(mapping.get(ch, ch) for ch in value).encode())
    return sorted(out, key=len, reverse=True)


# --- helpers -----------------------------------------------------------------------------------


def _take_carriers(flow: http.HTTPFlow) -> dict[str, list[str]] | None:
    """Remove the carrier headers from the request; return their values, or None if none."""
    headers = flow.request.headers
    found: dict[str, list[str]] = {}
    for name in CARRIERS:
        values = headers.get_all(name)
        if values:
            found[name] = list(values)
        if name in headers:
            del headers[name]
    return found or None


def _creds(carried: dict[str, list[str]]) -> Creds | None:
    def one(name: str) -> str | None:
        values = carried.get(name) or []
        return values[0].strip() if len(values) == 1 and values[0].strip() else None

    if any(len(values) != 1 for values in carried.values()):
        return None  # a carrier appears twice: refuse rather than guess
    akid, secret, token = (one(name) for name in CARRIERS)
    if not (akid and secret and token):
        return None  # the broker always writes all three
    return Creds(akid, secret, token)


def _pin_to_sni(flow: http.HTTPFlow) -> str | None:
    """Require Host == SNI and an SNI under .amazonaws.com, then send the request to the SNI.

    In transparent mode flow.request.host is the IP the sandbox dialed. Setting it to the SNI
    hostname makes mitmproxy open its own connection to that name (upstream TLS verified against
    it), and rewrites Host/:authority to the same name before the request is signed.
    """
    sni = _normalize_host(flow.client_conn.sni)
    host = _normalize_host(_hostname(flow.request.host_header or ""))
    if not sni or not sni.endswith(SNI_SUFFIX) or host != sni:
        return None
    host_headers = flow.request.headers.get_all("host")
    if flow.request.authority and host_headers and _normalize_host(_hostname(host_headers[0])) != sni:
        return None  # HTTP/2 with a :authority and a Host header that disagree
    if flow.request.host != sni or (flow.request.host_header or "") != sni:
        flow.request.host = sni
    return sni


def _normalize_host(value: str | None) -> str:
    return (value or "").strip().lower().removesuffix(".")


def _hostname(authority: str) -> str:
    authority = authority.strip()
    if authority.startswith("["):
        return authority[1 : authority.find("]")] if "]" in authority else authority
    name, sep, port = authority.rpartition(":")
    return name if sep and port.isdigit() else authority


def _may_stream(flow: http.HTTPFlow) -> bool:
    """True if mitmproxy may stream this request body (same test as system.py, 1.2.0).

    A body over stream_large_bodies is streamed: decided before requestheaders when the
    Content-Length says so, or mid-upload for chunked and HTTP/2 bodies without one. A streamed
    request's headers go upstream before `request` fires, and it cannot get a local response.
    """
    req = flow.request
    if req.stream:
        return True
    if "content-length" in req.headers:
        return False
    if req.is_http2 or req.is_http3:
        return True
    return "transfer-encoding" in req.headers


def _refuse(flow: http.HTTPFlow, status: int, reason: str, buffered: bool = False, **fields) -> None:
    """Answer the sandbox locally; nothing is sent upstream."""
    if not buffered and _may_stream(flow):
        # mitmproxy 11.0.2 raises NotImplementedError for a local response on a streamed request.
        _kill(flow, reason, **fields)
        return
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f"<Error><Code>OpenSandboxEgressRefused</Code><Message>{reason}</Message></Error>\n"
    ).encode()
    flow.response = http.Response.make(
        status, body, {"Content-Type": "application/xml", "x-osb-egress-refused": reason}
    )
    _log("refused", flow, status=status, reason=reason, **fields)


def _kill(flow: http.HTTPFlow, reason: str, **fields) -> None:
    if flow.killable:
        flow.kill()
    elif not _is_killed(flow):
        flow.error = Error(Error.KILLED_MESSAGE)  # what kill() sets; mitmproxy then drops the flow
    _log("killed", flow, reason=reason, **fields)


def _is_killed(flow: http.HTTPFlow) -> bool:
    return flow.error is not None and flow.error.msg == Error.KILLED_MESSAGE


def _signed(flow: http.HTTPFlow, creds: Creds, scope: dict, payload_hash: str, hook: str) -> None:
    """After a successful sign(): remember what to scrub from an error body, and log it.

    Only the key ID and the session token: AWS echoes those, never the secret key, so keeping the
    secret in flow.metadata would only lengthen how long it is held."""
    flow.metadata[REDACT] = tuple(v for v in (creds.access_key_id, creds.session_token) if v)
    payload = "sha256" if HEX64.fullmatch(payload_hash) else payload_hash
    _log(
        "re-signed",
        flow,
        service=scope["service"],
        region=scope["region"],
        key=creds.access_key_id[:4] + "...",
        payload=payload,
        hook=hook,
    )


def _log(event: str, flow: http.HTTPFlow, status: int | None = None, **fields) -> None:
    try:
        req = flow.request
        where = f"{_clean(req.method)} {_clean(req.host_header or req.host)}{_clean(req.path.partition('?')[0])}"
        extra = " ".join(f"{k}={_clean(v)}" for k, v in fields.items() if v is not None)
        head = f"{LOG_PREFIX} {event}" + (f" {status}" if status is not None else "")
        LOG.warning("%s %s %s", head, where, extra)
        _flush_log()
    except Exception:  # logging must never decide a flow's fate
        pass


def _flush_log() -> None:
    """Push the line out now. mitmproxy's terminal log print()s each record to sys.stdout and never
    flushes; to a pipe that is block-buffered, so lines would reach the sidecar log in batches."""
    try:
        for handler in LOG.handlers:
            for stream in (getattr(handler, "file", None), getattr(handler, "stream", None)):
                if stream is not None and hasattr(stream, "flush"):
                    stream.flush()
        sys.stdout.flush()
    except Exception:
        pass


def _clean(value) -> str:
    """Printable ASCII only, so a sandbox-chosen Host or path cannot forge log lines."""
    return re.sub(r"[^\x21-\x7e]", "?", str(value))[:200]
