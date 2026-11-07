"""An independent SigV4 verifier for the addon's unit tests.

Written from the AWS documentation, not from botocore, so a re-signing bug cannot hide behind
a shared implementation:
  - Create a signed AWS API request (IAM User Guide)
  - Authenticating Requests: Using the Authorization Header (Amazon S3)
  - Signature Calculations for the Authorization Header: single chunk, and trailing headers (S3)

It plays the AWS endpoint: it gets the request exactly as the egress proxy would send it
upstream and says whether AWS would accept the signature. It also enforces two rules that are
specific to this series:
  - no carrier header (x-osb-*) reaches the endpoint;
  - every x-amz-* header present is signed (S3 rejects unsigned ones).

Adapted from the research lab's verifier (a TLS server); this one is a plain function.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import re
import urllib.parse
import zlib
from dataclasses import dataclass

AUTH_RE = re.compile(
    r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/([^/]+)/aws4_request,\s*"
    r"SignedHeaders=([^,]+),\s*Signature=([0-9a-f]{64})$"
)
S3_SERVICES = ("s3", "s3-object-lambda")


@dataclass
class Result:
    ok: bool
    detail: str
    canonical_request: str = ""

    def __bool__(self) -> bool:  # lets a test write `assert verify(...)`
        return self.ok

    def __repr__(self) -> str:  # pytest prints this when the assertion fails
        return f"Result(ok={self.ok}, detail={self.detail!r})\n--canonical request--\n{self.canonical_request}"


def uri_encode(value: str, encode_slash: bool = True) -> str:
    """RFC 3986 encoding as the SigV4 spec defines it: unreserved characters stay, the rest is %XX."""
    return urllib.parse.quote(value, safe="-_.~" + ("" if encode_slash else "/"))


def remove_dot_segments(path: str) -> str:
    """RFC 3986 section 5.2.4, step by step."""
    inp, out = path, ""
    while inp:
        if inp.startswith("../"):
            inp = inp[3:]
        elif inp.startswith("./"):
            inp = inp[2:]
        elif inp.startswith("/./"):
            inp = "/" + inp[3:]
        elif inp == "/.":
            inp = "/"
        elif inp.startswith("/../") or inp == "/..":
            inp = "/" + inp[4:] if inp.startswith("/../") else "/"
            out = out[: out.rfind("/")] if "/" in out else ""
        elif inp in (".", ".."):
            inp = ""
        else:
            end = inp.find("/", 1 if inp.startswith("/") else 0)
            out += inp if end == -1 else inp[:end]
            inp = "" if end == -1 else inp[end:]
    return out


def canonical_uri(raw_path: str, service: str) -> str:
    path = raw_path or "/"
    if service in S3_SERVICES:
        # S3: the object key is URI-encoded once and never normalized ('/' kept, '..' kept).
        return uri_encode(urllib.parse.unquote(path), encode_slash=False)
    # Every other service: normalize the path (RFC 3986), then encode each segment twice. The
    # path on the wire is already encoded once, so encode it one more time.
    return uri_encode(remove_dot_segments(path) or "/", encode_slash=False)


def canonical_query(raw_query: str) -> str:
    """Decode, re-encode each name and value, sort by name then value; a bare name gets '='."""
    if not raw_query:
        return ""
    pairs = []
    for part in raw_query.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        pairs.append(
            (uri_encode(urllib.parse.unquote(name)), uri_encode(urllib.parse.unquote(value)))
        )
    return "&".join(f"{n}={v}" for n, v in sorted(pairs))


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    key = _hmac(("AWS4" + secret_key).encode(), date)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    return key


def build_canonical_request(method, raw_path, raw_query, headers, signed, payload_hash, service):
    grouped: dict[str, list[str]] = {}
    for name, value in headers:
        grouped.setdefault(name.lower(), []).append(value)
    canonical_headers = "".join(
        f"{n}:{','.join(' '.join(v.split()) for v in grouped.get(n, []))}\n" for n in signed
    )
    return "\n".join(
        [
            method,
            canonical_uri(raw_path, service),
            canonical_query(raw_query),
            canonical_headers,
            ";".join(signed),
            payload_hash,
        ]
    )


def signature(secret_key, amz_date, region, service, canonical_request) -> str:
    scope = f"{amz_date[:8]}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = signing_key(secret_key, amz_date[:8], region, service)
    return hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()


def decode_aws_chunked(body: bytes) -> tuple[bytes, dict[str, str]]:
    """Decode an aws-chunked body with unsigned chunks; return (data, trailers)."""
    data, trailers, i = b"", {}, 0
    while True:
        j = body.index(b"\r\n", i)
        size = int(body[i:j].split(b";")[0], 16)
        i = j + 2
        if size == 0:
            for line in body[i:].decode().split("\r\n"):
                if ":" in line:
                    name, value = line.split(":", 1)
                    trailers[name.strip().lower()] = value.strip()
            return data, trailers
        data += body[i : i + size]
        i += size + 2


def verify(
    method: str,
    raw_path: str,
    raw_query: str,
    headers: list[tuple[str, str]],
    body: bytes,
    *,
    access_key: str,
    secret_key: str,
    session_token: str | None = None,
    expect_service: str | None = None,
    expect_region: str | None = None,
    now: datetime.datetime | None = None,
) -> Result:
    """Would AWS accept this request? `headers` are (name, value) pairs as received."""
    h: dict[str, list[str]] = {}
    for name, value in headers:
        h.setdefault(name.lower(), []).append(value)

    leaked = sorted(n for n in h if n.startswith("x-osb-"))
    if leaked:
        return Result(False, f"carrier headers reached the endpoint: {leaked}")

    auth = (h.get("authorization") or [""])[0]
    m = AUTH_RE.match(auth)
    if not m:
        return Result(False, f"missing or malformed Authorization: {auth[:80]!r}")
    akid, date, region, service, signed_str, sig = m.groups()
    if akid != access_key:
        return Result(False, f"InvalidAccessKeyId: {akid}")
    if expect_service is not None and service != expect_service:
        return Result(False, f"credential scope service {service!r}, expected {expect_service!r}")
    if expect_region is not None and region != expect_region:
        return Result(False, f"credential scope region {region!r}, expected {expect_region!r}")

    signed = signed_str.split(";")
    if signed != sorted(signed):
        return Result(False, f"SignedHeaders not sorted: {signed_str}")
    if "host" not in signed:
        return Result(False, "host is not signed")
    missing = [n for n in signed if n not in h]
    if missing:
        return Result(False, f"signed headers missing from the request: {missing}")
    unsigned_amz = sorted(
        n
        for n in h
        if (n.startswith("x-amz-") or n == "content-md5")
        and n not in signed
        and n != "x-amz-content-sha256"
    )
    if unsigned_amz:
        return Result(
            False, f"There were headers present in the request which were not signed: {unsigned_amz}"
        )

    token = (h.get("x-amz-security-token") or [None])[0]
    if session_token is not None and token != session_token:
        return Result(False, "missing or wrong x-amz-security-token")
    if session_token is None and token is not None:
        return Result(False, "unexpected x-amz-security-token")

    amz_date = (h.get("x-amz-date") or [""])[0]
    if amz_date[:8] != date:
        return Result(False, f"x-amz-date {amz_date!r} does not match the scope date {date}")
    stamp = datetime.datetime.strptime(amz_date, "%Y%m%dT%H%M%SZ").replace(
        tzinfo=datetime.timezone.utc
    )
    now = now or datetime.datetime.now(datetime.timezone.utc)
    skew = abs((now - stamp).total_seconds())
    if skew > (900 if service in S3_SERVICES else 300):
        return Result(False, f"RequestTimeTooSkewed: {skew:.0f}s")

    content_sha = (h.get("x-amz-content-sha256") or [None])[0]
    aws_chunked = "aws-chunked" in ",".join(h.get("content-encoding", []))
    if service in S3_SERVICES:
        if content_sha is None:
            return Result(False, "S3 requires x-amz-content-sha256")
        if content_sha.startswith("STREAMING-AWS4-"):
            return Result(False, "signed-chunk streaming is not supported by this verifier")
        if content_sha.startswith("STREAMING-UNSIGNED-PAYLOAD") and not aws_chunked:
            return Result(False, "STREAMING-UNSIGNED-PAYLOAD-TRAILER without aws-chunked encoding")
        if re.fullmatch(r"[0-9a-f]{64}", content_sha) and hashlib.sha256(body).hexdigest() != content_sha:
            return Result(False, "XAmzContentSHA256Mismatch")
        payload_hash = content_sha
    else:
        payload_hash = content_sha or hashlib.sha256(body).hexdigest()

    if aws_chunked:
        data, trailers = decode_aws_chunked(body)
        declared = int((h.get("x-amz-decoded-content-length") or ["-1"])[0])
        if len(data) != declared:
            return Result(False, f"IncompleteBody: decoded {len(data)}, declared {declared}")
        if "x-amz-checksum-crc32" in trailers:
            crc = base64.b64encode(zlib.crc32(data).to_bytes(4, "big")).decode()
            if crc != trailers["x-amz-checksum-crc32"]:
                return Result(False, "BadDigest: trailer CRC32 does not match the data")

    creq = build_canonical_request(method, raw_path, raw_query, headers, signed, payload_hash, service)
    expected = signature(secret_key, amz_date, region, service, creq)
    if expected != sig:
        return Result(False, "SignatureDoesNotMatch", creq)
    return Result(True, f"OK service={service} region={region} signed={signed_str}", creq)
