"""Unit tests for sigv4_resign.py, the SigV4 re-signing addon in the egress image.

Each request is built the way the sandbox's SDK and the Credential Vault build it (placeholder
signature, then carrier headers), driven through a model of mitmproxy 11.0.2's request path
(harness.drive), and the request that would go upstream is checked by an independent SigV4
verifier written from the AWS spec (verifier.py). One assertion per test.

Review Focus mapping (parts 3-6 plan):
  1. never forward a request it could not safely re-sign: fail closed, carriers never upstream
  2. Host-header spoofing (#1758): Host must equal the SNI, the upstream is pinned to the SNI
  3. credential-vending calls are refused before signing
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import urllib.parse

import pytest
from mitmproxy import http

import harness as h
import sigv4_resign as addon
from mitmproxy.net import encoding as content_encoding

EMPTY_SHA = hashlib.sha256(b"").hexdigest()
REGION = "us-east-1"
STS = f"sts.{REGION}.amazonaws.com"
S3_BUCKET = f"osb-test-bucket.s3.{REGION}.amazonaws.com"
DYNAMODB = f"dynamodb.{REGION}.amazonaws.com"
BEDROCK = f"bedrock-runtime.{REGION}.amazonaws.com"
FORM = ("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")


# --- request builders: what the sandbox sends, after the vault injected the carriers ---------


def sts_call(action: str = "GetCallerIdentity", extra: str = "", **kw):
    body = f"Action={action}&Version=2011-06-15{extra}".encode()
    return h.client_flow("POST", STS, "/", service="sts", headers=[FORM], body=body, **kw)


def sts_query(action: str):
    path = f"/?Action={action}&RoleArn=arn%3Aaws%3Aiam%3A%3A123456789012%3Arole%2Fx&RoleSessionName=s&Version=2011-06-15"
    return h.client_flow("GET", STS, path, service="sts")


def dynamodb_put(size: int = 200, **kw):
    item = {"TableName": "osb-cred-test", "Item": {"pk": {"S": "k"}, "blob": {"S": "x" * size}}}
    body = json.dumps(item).encode()
    headers = [
        ("Content-Type", "application/x-amz-json-1.0"),
        ("X-Amz-Target", "DynamoDB_20120810.PutItem"),
    ]
    return h.client_flow("POST", DYNAMODB, "/", service="dynamodb", headers=headers, body=body, **kw)


def bedrock_converse():
    path = "/model/us.anthropic.claude-haiku-4-5-20251001-v1%3A0/converse"
    body = json.dumps({"messages": [{"role": "user", "content": [{"text": "hi"}]}]}).encode()
    # bedrock-runtime signs as service "bedrock": the scope comes from the client, not the host.
    return h.client_flow(
        "POST", BEDROCK, path, service="bedrock", headers=[("Content-Type", "application/json")], body=body
    )


def s3_put_chunked(size: int = 5 * h.MIB):
    """boto3 1.36+/CLI v2 PutObject: aws-chunked, unsigned chunks, CRC32 trailer, chunked framing."""
    data = bytes(range(256)) * (size // 256)
    headers = [
        ("Content-Encoding", "aws-chunked"),
        ("X-Amz-Decoded-Content-Length", str(len(data))),
        ("X-Amz-Trailer", "x-amz-checksum-crc32"),
        ("X-Amz-Sdk-Checksum-Algorithm", "CRC32"),
    ]
    return h.client_flow(
        "PUT", S3_BUCKET, "/big.bin", service="s3", headers=headers, body=h.aws_chunked(data),
        content_sha="STREAMING-UNSIGNED-PAYLOAD-TRAILER", chunked=True,
    )


def s3_put_crt(size: int = 5 * h.MIB):
    """The CRT S3 client: Content-Length, UNSIGNED-PAYLOAD."""
    return h.client_flow(
        "PUT", S3_BUCKET, "/crt.bin", service="s3", body=b"c" * size, content_sha="UNSIGNED-PAYLOAD"
    )


def s3_put_small(path: str = "/small.txt", body: bytes = b"hello from the sandbox\n"):
    return h.client_flow(
        "PUT", S3_BUCKET, path, service="s3", body=body, content_sha=hashlib.sha256(body).hexdigest()
    )


def s3_list(query: str = "list-type=2&prefix=a%20b%2F&encoding-type=url"):
    return h.client_flow("GET", S3_BUCKET, f"/?{query}", service="s3", content_sha=EMPTY_SHA)


def unsigned_get():
    return h.client_flow("GET", S3_BUCKET, "/public.txt", service="s3", sign=False)


def h2_get():
    path = "/?Action=ListUsers"  # any bodiless GET to a non-S3 service over HTTP/2
    return h.client_flow("GET", f"iam.amazonaws.com", path, service="iam", http2=True)


FORWARDED = {
    "sts": sts_call,
    "dynamodb": dynamodb_put,
    "bedrock": bedrock_converse,
    "s3-chunked-trailer": s3_put_chunked,
    "s3-crt-unsigned": s3_put_crt,
    "s3-small": s3_put_small,
    "s3-list": s3_list,
    "h2-get": h2_get,
}

REFUSED_OR_KILLED = {
    "large-dynamodb": lambda: dynamodb_put(size=2 * h.MIB),
    "chunked-dynamodb": lambda: dynamodb_put(size=2 * h.MIB, chunked=True),
    "host-spoof": lambda: sts_call(sni=f"s3.{REGION}.amazonaws.com"),
    "assume-role": lambda: sts_call("AssumeRole", "&RoleArn=arn%3Aaws%3Aiam%3A%3A123456789012%3Arole%2Fx&RoleSessionName=s"),
    "bearer": lambda: h.client_flow(
        "POST", BEDROCK, "/model/m/converse", service="bedrock", body=b"{}", auth="Bearer bedrock-api-key-placeholder"
    ),
    "signed-chunk": lambda: h.client_flow(
        "PUT", S3_BUCKET, "/sc.bin", service="s3", body=b"x" * 100, content_sha="STREAMING-AWS4-HMAC-SHA256-PAYLOAD"
    ),
    # refusals whose body is streamed: mitmproxy cannot send a local response, so they are killed
    "signed-chunk-large": lambda: h.client_flow(
        "PUT", S3_BUCKET, "/sc.bin", service="s3", body=b"x" * (5 * h.MIB), content_sha="STREAMING-AWS4-HMAC-SHA256-PAYLOAD"
    ),
    "bearer-large": lambda: h.client_flow(
        "POST", BEDROCK, "/model/m/converse", service="bedrock", body=b"b" * (2 * h.MIB),
        auth="Bearer bedrock-api-key-placeholder",
    ),
}

# Presigned (query-string auth): forwarded unchanged, carriers stripped, never re-signed.
SIGV4_QUERY = (
    "X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Credential=AKIA_OSB_PLACEHOLDER%2F20261101%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20261101T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host&X-Amz-Signature=" + "0" * 64
)
SIGV2_QUERY = "AWSAccessKeyId=AKIA_OSB_PLACEHOLDER&Signature=c2lnbmF0dXJl&Expires=1798761600"
PRESIGNED = {
    "presigned-sigv4": lambda: h.client_flow("GET", S3_BUCKET, f"/k?{SIGV4_QUERY}", service="s3", sign=False),
    "presigned-sigv2": lambda: h.client_flow("GET", S3_BUCKET, f"/k?{SIGV2_QUERY}", service="s3", sign=False),
    # a header signature as well: still never re-signed
    "presigned-with-auth-header": lambda: h.client_flow("GET", S3_BUCKET, f"/k?{SIGV4_QUERY}", service="s3", content_sha=EMPTY_SHA),
    "presigned-large-put": lambda: h.client_flow(
        "PUT", S3_BUCKET, f"/k?{SIGV4_QUERY}", service="s3", sign=False,
        body=b"p" * (5 * h.MIB), content_sha="UNSIGNED-PAYLOAD",
    ),
}

ALL = {**FORWARDED, **REFUSED_OR_KILLED, **PRESIGNED, "unsigned": unsigned_get}


def run(build):
    flow, body = build()
    return h.drive(addon, flow, body)


def refusal(outcome):
    """(kind, status, reason) of a locally answered request; the reason is in a response header."""
    reason = outcome.flow.response.headers.get("x-osb-egress-refused") if outcome.flow.response else None
    return outcome.kind, outcome.status, reason


# --- Review Focus 1: re-signing is correct, or the flow does not leave ------------------------


def test_resign_sts_verifies():
    """STS POST (signed in `request` from the hashed body): AWS would accept it, no carriers."""
    assert h.verify_sent(run(sts_call), expect_service="sts", expect_region=REGION)


@pytest.mark.parametrize("build", [s3_put_chunked, s3_put_crt, s3_put_small], ids=["chunked-trailer", "crt-unsigned", "small-hex"])
def test_resign_s3_streaming_from_content_sha(build):
    """S3 signs in `requestheaders` from x-amz-content-sha256: the headers go upstream when
    streaming starts, before `request` fires, and already carry the real signature."""
    outcome = run(build)
    assert outcome.events == ["requestheaders", "sent", "request"] and h.verify_sent(outcome, expect_service="s3")


def test_s3_signed_in_requestheaders_sets_stream():
    flow, _ = s3_put_chunked()
    addon.requestheaders(flow)
    assert flow.request.stream is True


def test_s3_path_not_normalized():
    """Key 'a b/../c+ä.txt': S3 signs the path as sent (a%20b, '..' kept), not double-encoded."""
    assert h.verify_sent(run(lambda: s3_put_small("/a%20b/../c%2B%C3%A4.txt")), expect_service="s3")


def test_s3_query_sorted_by_name():
    """Canonical query sorts by name, then value ('prefix' before 'prefix-x'; a raw-string sort fails)."""
    assert h.verify_sent(run(lambda: s3_list("prefix-x=2&prefix=1&list-type=2")), expect_service="s3")


def test_resign_dynamodb_verifies():
    """A small DynamoDB body: x-amz-target signed, the placeholder session token replaced."""
    assert h.verify_sent(run(lambda: dynamodb_put(placeholder_token=True)), expect_service="dynamodb")


def test_resign_bedrock_converse_verifies():
    """Bedrock Runtime: scope service 'bedrock' from the client; the path is encoded twice."""
    assert h.verify_sent(run(bedrock_converse), expect_service="bedrock", expect_region=REGION)


def test_non_s3_path_normalized_then_encoded_twice():
    """Every service but S3 signs the RFC 3986-normalized path ('./' and '..' removed)."""
    def build():
        flow, body = bedrock_converse()  # the placeholder signature is discarded anyway
        flow.request.path = "/model/./x/../us.anthropic.claude-haiku-4-5-20251001-v1%3A0/converse"
        return flow, body
    assert h.verify_sent(run(build), expect_service="bedrock")


def test_s3_without_content_sha_hashes_body():
    """An S3 client that sends no x-amz-content-sha256 (curl): hashed in `request`, header added."""
    assert h.verify_sent(run(lambda: h.client_flow("PUT", S3_BUCKET, "/c.txt", service="s3", body=b"curl body")), expect_service="s3")


def test_expect_header_never_signed():
    """mitmproxy drops `Expect: 100-continue` after requestheaders; signing it would break the request."""
    unsigned = h.CLIENT_UNSIGNED - {"expect"}  # a client that did sign Expect
    build = lambda: h.client_flow(
        "PUT", S3_BUCKET, "/e.txt", service="s3", body=b"expect", headers=[("Expect", "100-continue")],
        content_sha=hashlib.sha256(b"expect").hexdigest(), client_unsigned=unsigned,
    )
    assert h.verify_sent(run(build), expect_service="s3")


def test_h2_get_without_length_signs_empty_body():
    """HTTP/2 GET with no Content-Length: signed in `requestheaders` over the empty body."""
    assert h.verify_sent(run(h2_get), expect_service="iam")


@pytest.mark.parametrize(
    "build",
    [
        lambda: dynamodb_put(size=2 * h.MIB),  # Content-Length > 1 MiB: mitmproxy streams it
        lambda: dynamodb_put(size=2 * h.MIB, chunked=True),  # chunked: would switch mid-upload
        lambda: dynamodb_put(size=2 * h.MIB, chunked=True, http2=True),  # HTTP/2, no length
    ],
    ids=["content-length", "chunked", "http2-no-length"],
)
def test_large_non_s3_body_killed(build):
    """Review Focus 1: a 2 MiB DynamoDB body has no usable payload hash, so the flow is killed."""
    assert run(build).kind == "killed"


@pytest.mark.parametrize("build", [s3_put_chunked, sts_call], ids=["s3-requestheaders", "sts-request"])
def test_exception_fails_closed(build, monkeypatch):
    """Review Focus 1: an error while signing kills a flow that carries carriers."""

    def broken_sign(*args, **kwargs):
        raise RuntimeError("injected signing failure")

    monkeypatch.setattr(addon, "sign", broken_sign)
    assert run(build).kind == "killed"


@pytest.mark.parametrize("name", sorted(ALL))
def test_carriers_absent(name):
    """Review Focus 1: the carrier headers are stripped from every flow, whatever its outcome."""
    outcome = run(ALL[name])
    assert not {n.lower() for n in outcome.flow.request.headers} & set(h.CARRIER_NAMES)


@pytest.mark.parametrize("name", ["signed-chunk-large", "bearer-large"])
def test_refusal_of_a_streamed_body_is_a_kill(name):
    assert run(REFUSED_OR_KILLED[name]).kind == "killed"


@pytest.mark.parametrize("name", sorted(ALL))
def test_never_a_local_response_on_a_streamed_flow(name):
    """mitmproxy 11.0.2 raises NotImplementedError for a response on a streamed request."""
    assert run(ALL[name]).kind != "error"


@pytest.mark.parametrize(
    "mode", ["STREAMING-AWS4-HMAC-SHA256-PAYLOAD", "STREAMING-AWS4-HMAC-SHA256-PAYLOAD-TRAILER"]
)
def test_signed_chunk_refused_501(mode):
    """Chunk signatures chain from the seed signature, so header-only re-signing cannot work."""
    outcome = run(lambda: h.client_flow("PUT", S3_BUCKET, "/sc.bin", service="s3", body=b"x" * 100, content_sha=mode))
    assert refusal(outcome) == ("refused", 501, "signed-chunk-upload")


def test_sigv4a_refused_501():
    auth = "AWS4-ECDSA-P256-SHA256 Credential=AKIA_OSB_PLACEHOLDER/20261101/s3/aws4_request, SignedHeaders=host, Signature=00"
    outcome = run(lambda: h.client_flow("GET", S3_BUCKET, "/k", service="s3", auth=auth, content_sha=EMPTY_SHA))
    assert refusal(outcome) == ("refused", 501, "sigv4a-not-supported")


def test_bearer_with_carriers_refused():
    """Ruling C9: a Bearer request on a SigV4-bound host is refused, not forwarded."""
    outcome = run(REFUSED_OR_KILLED["bearer"])
    assert refusal(outcome) == ("refused", 403, "bearer-on-sigv4-binding")


@pytest.mark.parametrize(
    "carriers",
    [
        {h.CARRIER_NAMES[0]: h.REAL_AKID},
        {h.CARRIER_NAMES[0]: h.REAL_AKID, h.CARRIER_NAMES[1]: h.REAL_SECRET},  # review M-4
    ],
    ids=["key-id-only", "no-session-token"],
)
def test_incomplete_carriers_refused(carriers):
    """All three carriers, or nothing is signed (the broker always writes three)."""
    outcome = run(lambda: sts_call(carriers=carriers))
    assert refusal(outcome) == ("refused", 403, "incomplete-carriers")


def test_duplicated_carrier_refused():
    def build():
        flow, body = sts_call()
        flow.request.headers.add(h.CARRIER_NAMES[2], "a-second-session-token")
        return flow, body

    assert refusal(run(build)) == ("refused", 403, "incomplete-carriers")


def test_unsigned_request_forwarded_without_credentials():
    """No Authorization at all (e.g. --no-sign-request): forwarded as it came, minus the carriers."""
    outcome = run(unsigned_get)
    assert outcome.kind == "forwarded" and not outcome.sent.header_names() & {"authorization", "x-amz-security-token"}


# --- Review Focus 2: Host spoofing (#1758) -----------------------------------------------------


@pytest.mark.parametrize(
    "sni,tls",
    [(f"s3.{REGION}.amazonaws.com", True), ("attacker.example", True), (None, False)],
    ids=["other-aws-host", "foreign-host", "plain-http-no-sni"],
)
def test_host_spoof_killed(sni, tls):
    """The vault matched the binding on Host; the TLS SNI says otherwise, so the flow dies."""
    assert run(lambda: sts_call(sni=sni, tls=tls)).kind == "killed"


def test_sni_outside_amazonaws_killed():
    """Ruling C21: carriers on a host that is not *.amazonaws.com are never used."""
    outcome = run(lambda: h.client_flow("POST", "api.example.com", "/", service="sts", headers=[FORM], body=b"Action=GetCallerIdentity"))
    assert outcome.kind == "killed"


def test_upstream_pinned_to_sni():
    """The upstream is the SNI-verified hostname, not the IP the sandbox dialed."""
    outcome = run(sts_call)
    assert (outcome.sent.host, outcome.sent.port) == (STS, 443)


# --- Review Focus 3: credential-vending calls ---------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda: sts_call("AssumeRole", "&RoleArn=arn%3Aaws%3Aiam%3A%3A123456789012%3Arole%2Fx&RoleSessionName=s"),
        lambda: sts_query("AssumeRole"),
        lambda: sts_call("GetSessionToken"),
        lambda: sts_call("GetFederationToken", "&Name=x"),
        lambda: sts_call("AssumeRoot"),
        lambda: sts_call("GetCallerIdentity", "&Action=AssumeRole"),  # parameter pollution
        lambda: h.client_flow(
            "POST", STS, "/", service="sts", body=b"{}",
            headers=[("Content-Type", "application/x-amz-json-1.0"), ("X-Amz-Target", "AWSSecurityTokenServiceV20110615.AssumeRole")],
        ),
        lambda: h.client_flow("POST", STS, "/", service="sts", headers=[FORM], body=b"Version=2011-06-15"),  # no action
        lambda: h.client_flow(
            "POST", f"api.ecr.{REGION}.amazonaws.com", "/", service="ecr", body=b"{}",
            headers=[("Content-Type", "application/x-amz-json-1.1"), ("X-Amz-Target", "AmazonEC2ContainerRegistry_V20150921.GetAuthorizationToken")],
        ),
        lambda: h.client_flow(
            "GET", f"osb--use1-az4--x-s3.s3express-use1-az4.{REGION}.amazonaws.com", "/?session",
            service="s3express", content_sha=EMPTY_SHA,
        ),
        lambda: h.client_flow(
            "POST", f"codeartifact.{REGION}.amazonaws.com", "/v1/authorization-token?domain=osb&domain-owner=123456789012",
            service="codeartifact",
        ),
    ],
    ids=[
        "sts-assume-role", "sts-assume-role-get", "sts-get-session-token", "sts-get-federation-token",
        "sts-assume-root", "sts-duplicate-action", "sts-json-target", "sts-no-action",
        "ecr-get-authorization-token", "s3express-create-session", "codeartifact-get-authorization-token",
    ],
)
def test_credential_vending_refused(build):
    """Review Focus 3: refused with 403 before anything is signed or forwarded."""
    assert refusal(run(build)) == ("refused", 403, "credential-vending")


@pytest.mark.parametrize("name", sorted(PRESIGNED))
def test_presigned_forwarded_unchanged(name):
    """Lead ruling: a presigned request is authorized by whoever signed it (the trusted side), or
    carries the placeholder key and AWS rejects it. Forwarded as it came: query and any
    Authorization header untouched, carriers gone, nothing re-signed."""
    flow, body = PRESIGNED[name]()
    path_before = flow.request.path.partition("?")
    auth_before = flow.request.headers.get("authorization")
    outcome = h.drive(addon, flow, body)
    sent = outcome.sent
    assert (
        outcome.kind,
        (sent.path, sent.query),
        dict((k.lower(), v) for k, v in sent.headers).get("authorization"),
        sent.header_names() & set(h.CARRIER_NAMES),
    ) == ("forwarded", (path_before[0], path_before[2]), auth_before, set())


# --- review round 1: duplicate headers (I-1), normalized vending paths (M-2), s3express (M-3) ---

ECR_TARGET = "AmazonEC2ContainerRegistry_V20150921."
STS_TARGET = "AWSSecurityTokenServiceV20110615."
CODEARTIFACT = f"codeartifact.{REGION}.amazonaws.com"


def json_call(host, service, targets):
    headers = [("Content-Type", "application/x-amz-json-1.1")] + [("X-Amz-Target", t) for t in targets]
    return h.client_flow("POST", host, "/", service=service, body=b"{}", headers=headers)


def sts_with(extra_headers, **kw):
    body = b"Action=GetCallerIdentity&Version=2011-06-15"
    return h.client_flow("POST", STS, "/", service="sts", headers=[FORM, *extra_headers], body=body, **kw)


AMBIGUOUS_TARGETS = {
    "ecr-two-targets": lambda: json_call(
        f"api.ecr.{REGION}.amazonaws.com", "ecr", [ECR_TARGET + "GetAuthorizationToken", ECR_TARGET + "DescribeRepositories"]
    ),
    "sts-two-targets": lambda: json_call(STS, "sts", [STS_TARGET + "AssumeRole", STS_TARGET + "GetCallerIdentity"]),
    "ecr-comma-in-target": lambda: json_call(
        f"api.ecr.{REGION}.amazonaws.com", "ecr", [f"{ECR_TARGET}GetAuthorizationToken, {ECR_TARGET}DescribeRepositories"]
    ),
}


@pytest.mark.parametrize("name", sorted(AMBIGUOUS_TARGETS))
def test_ambiguous_target_refused(name):
    """Review I-1: a folded "A, B" read would check only B while the service may act on A."""
    assert refusal(run(AMBIGUOUS_TARGETS[name])) == ("refused", 403, "ambiguous-target")


@pytest.mark.parametrize(
    "service,targets",
    [
        ("ecr", [ECR_TARGET + "GetAuthorizationToken", ECR_TARGET + "DescribeRepositories"]),
        ("ecr", [ECR_TARGET + "DescribeRepositories", ECR_TARGET + "GetAuthorizationToken"]),
        ("sts", [STS_TARGET + "AssumeRole", STS_TARGET + "GetCallerIdentity"]),
        ("ecr", [f"{ECR_TARGET}DescribeRepositories, {ECR_TARGET}GetAuthorizationToken"]),
    ],
    ids=["ecr-vending-first", "ecr-vending-last", "sts-vending-first", "ecr-comma"],
)
def test_is_credential_vending_sees_every_target(service, targets):
    """The function on its own, without the hook's ambiguity refusal: every target value counts."""
    headers = http.Headers([(b"X-Amz-Target", t.encode()) for t in targets])
    assert addon.is_credential_vending(service, headers, "", b"{}") is True


DUPLICATE_HEADERS = {
    "host": lambda: sts_with([("Host", STS)]),
    "authorization": lambda: sts_with(
        [("Authorization", "AWS4-HMAC-SHA256 Credential=AKIA_OSB_PLACEHOLDER/20261101/us-east-1/sts/aws4_request, "
                           "SignedHeaders=host, Signature=" + "1" * 64)]
    ),
    "x-amz-content-sha256": lambda: h.client_flow(
        "PUT", S3_BUCKET, "/d.txt", service="s3", body=b"dup", headers=[("X-Amz-Content-SHA256", "UNSIGNED-PAYLOAD")],
        content_sha=hashlib.sha256(b"dup").hexdigest(),
    ),
}


@pytest.mark.parametrize("name", sorted(DUPLICATE_HEADERS))
def test_duplicate_security_header_refused(name):
    assert refusal(run(DUPLICATE_HEADERS[name])) == ("refused", 403, "duplicate-header")


def test_h2_authority_and_host_header_disagree_killed():
    """Review M-7: HTTP/2 with :authority = the SNI and a Host header naming another host."""
    assert run(lambda: sts_with([("Host", "attacker.example")], http2=True)).kind == "killed"


def test_host_with_port_pinned_and_signed():
    """Review M-7: 'Host: sts...:443' is pinned to the bare SNI name and signed with it."""
    assert h.verify_sent(run(lambda: sts_call(host_header=f"{STS}:443")), expect_service="sts")


VENDING_VARIANTS = {
    "codeartifact-encoded-dash": lambda: h.client_flow("POST", CODEARTIFACT, "/v1/authorization%2Dtoken?domain=osb", service="codeartifact"),
    "codeartifact-double-encoded": lambda: h.client_flow("POST", CODEARTIFACT, "/v1/authorization%252Dtoken?domain=osb", service="codeartifact"),
    "codeartifact-dot-segments": lambda: h.client_flow("POST", CODEARTIFACT, "/x/../v1/authorization-token?domain=osb", service="codeartifact"),
    "codeartifact-double-slash-and-case": lambda: h.client_flow("POST", CODEARTIFACT, "//v1/Authorization-Token?domain=osb", service="codeartifact"),
    "rpcv2-encoded-operation": lambda: h.client_flow(
        "POST", f"api.ecr.{REGION}.amazonaws.com", "/service/AmazonEC2ContainerRegistry_V20150921/operation/GetAuthoriz%61tionToken",
        service="ecr", body=b"\xa0", headers=[("Content-Type", "application/cbor"), ("Smithy-Protocol", "rpc-v2-cbor")],
    ),
    "s3express-host-signed-as-s3": lambda: h.client_flow(
        "GET", f"osb--use1-az4--x-s3.s3express-use1-az4.{REGION}.amazonaws.com", "/?session", service="s3", content_sha=EMPTY_SHA
    ),
}


@pytest.mark.parametrize("name", sorted(VENDING_VARIANTS))
def test_vending_path_and_host_variants_refused(name):
    """Review M-2/M-3: action paths are matched decoded, normalized and lowercased; S3 Express
    CreateSession is caught by its host whatever scope service the client signed with."""
    assert refusal(run(VENDING_VARIANTS[name])) == ("refused", 403, "credential-vending")


# --- responses, untouched traffic, logging, key shapes ------------------------------------------


def test_eventstream_response_streamed():
    """ConverseStream: tokens must reach the sandbox as they arrive, not in one buffered block."""
    flow, _ = bedrock_converse()
    flow.response = http.Response.make(200, b"", {"content-type": "application/vnd.amazon.eventstream"})
    addon.responseheaders(flow)
    assert flow.response.stream is True


def signed_flow_with_response(response: http.Response):
    """An STS flow this addon re-signed, with `response` from upstream run through the hooks."""
    flow = run(sts_call).flow
    flow.response = response
    addon.responseheaders(flow)
    addon.response(flow)
    return flow


def _escaped(value: str, table: dict[str, str]) -> bytes:
    return "".join(table.get(c, c) for c in value).encode()


TOKEN_FORMS = {
    "raw": h.REAL_TOKEN.encode(),
    "url-encoded": urllib.parse.quote(h.REAL_TOKEN, safe="").encode(),
    "url-encoded-lowercase": urllib.parse.quote(h.REAL_TOKEN, safe="").lower().encode(),
    "json-escaped-slash": h.REAL_TOKEN.replace("/", "\\/").encode(),
    "json-unicode-escapes": _escaped(h.REAL_TOKEN, {"/": "\\u002f", "+": "\\u002b", "=": "\\u003d"}),
    "json-unicode-escapes-upper": _escaped(h.REAL_TOKEN, {"/": "\\u002F", "+": "\\u002B", "=": "\\u003D"}),
    "json-html-safe": _escaped(h.REAL_TOKEN, {"=": "\\u003d", "+": "\\u002b"}),  # '/' left as is
    "xml-decimal-refs": _escaped(h.REAL_TOKEN, {"/": "&#47;", "+": "&#43;", "=": "&#61;"}),
    "xml-hex-refs": _escaped(h.REAL_TOKEN, {"/": "&#x2F;", "+": "&#x2B;", "=": "&#x3D;"}),
    "xml-hex-refs-lower": _escaped(h.REAL_TOKEN, {"/": "&#x2f;", "+": "&#x2b;", "=": "&#x3d;"}),
    "hex-bytes": " ".join(f"{b:02x}" for b in h.REAL_TOKEN.encode()).encode(),  # S3 CanonicalRequestBytes
    "hex-bytes-upper": " ".join(f"{b:02X}" for b in h.REAL_TOKEN.encode()).encode(),
    "hex-no-separator": h.REAL_TOKEN.encode().hex().encode(),
    "hex-no-separator-upper": h.REAL_TOKEN.encode().hex().upper().encode(),
}


@pytest.mark.parametrize("form", sorted(TOKEN_FORMS))
def test_error_body_redacted(form):
    """Review M-1: an AWS error that echoes the real key ID and session token reaches the
    sandbox with both replaced by [REDACTED]."""
    leaked = TOKEN_FORMS[form]
    body = (
        b"<Error><Code>SignatureDoesNotMatch</Code><AWSAccessKeyId>" + h.REAL_AKID.encode()
        + b"</AWSAccessKeyId><CanonicalRequest>x-amz-security-token:" + leaked + b"</CanonicalRequest></Error>"
    )
    content = signed_flow_with_response(http.Response.make(403, body, {"Content-Type": "application/xml"})).response.content
    assert (leaked in content, h.REAL_AKID.encode() in content, content.count(b"[REDACTED]")) == (False, False, 2)


def test_secret_key_not_stored_for_redaction():
    """Lead ruling (N-3): only the key ID and the session token are kept for scrubbing error
    bodies; AWS never echoes the secret key, so it is not held in flow.metadata."""
    stored = run(sts_call).flow.metadata[addon.REDACT]
    assert (h.REAL_SECRET in stored, h.REAL_AKID in stored, h.REAL_TOKEN in stored) == (False, True, True)


def test_gzip_error_body_redacted():
    response = http.Response.make(403, b"", {"Content-Type": "application/xml", "Content-Encoding": "gzip"})
    response.content = b"<Error><Message>" + h.REAL_TOKEN.encode() + b"</Message></Error>"  # stored compressed
    assert h.REAL_TOKEN.encode() not in signed_flow_with_response(response).response.get_content()


def test_undecodable_error_body_killed():
    """A body mitmproxy cannot decode might hide the token: the flow is killed, nothing is sent."""
    garbage = b"not gzip " + h.REAL_TOKEN.encode()
    outcome = h.drive_response(
        addon, run(sts_call).flow, 403, [("Content-Encoding", "gzip"), ("Content-Length", str(len(garbage)))], garbage
    )
    assert (outcome.kind, outcome.delivered) == ("killed", b"")


# --- review round 2 (N-1): system.py streams every chunked response --------------------------

ECHO_ERROR = (
    b"<Error><Code>SignatureDoesNotMatch</Code><AWSAccessKeyId>" + h.REAL_AKID.encode()
    + b"</AWSAccessKeyId><CanonicalRequest>x-amz-security-token:" + h.REAL_TOKEN.encode()
    + b"</CanonicalRequest></Error>"
)
CHUNKED_XML = [("Content-Type", "application/xml"), ("Transfer-Encoding", "chunked")]


def secrets_in(data: bytes) -> list[str]:
    return [name for name, value in (("token", h.REAL_TOKEN), ("key-id", h.REAL_AKID)) if value.encode() in data]


def test_chunked_error_body_scrubbed_behind_system_py():
    """Review N-1: system.py streams the chunked 403; the addon still holds it back and scrubs it.
    7-byte chunks, so the token is split across chunk boundaries."""
    outcome = h.drive_response(addon, run(sts_call).flow, 403, CHUNKED_XML, ECHO_ERROR, chunk=7)
    assert (outcome.kind, secrets_in(outcome.delivered), outcome.delivered.count(b"[REDACTED]")) == ("streamed", [], 2)


def test_chunked_error_body_over_1mib_killed():
    """Over 1 MiB it cannot be held: nothing of the body is passed on and the flow is killed."""
    outcome = h.drive_response(addon, run(sts_call).flow, 403, CHUNKED_XML, ECHO_ERROR + b" " * (2 * h.MIB))
    assert (outcome.kind, outcome.delivered) == ("killed-after-headers", b"")


def test_content_length_error_body_over_1mib_killed():
    """A Content-Length over 1 MiB: killed in responseheaders, before even the status goes out."""
    body = ECHO_ERROR + b" " * (2 * h.MIB)
    outcome = h.drive_response(addon, run(sts_call).flow, 403, [("Content-Length", str(len(body)))], body)
    assert (outcome.kind, outcome.delivered) == ("killed", b"")


def test_content_length_error_body_buffered_despite_system_py():
    """system.py also streams text/event-stream; a signed 4xx with a Content-Length is buffered anyway."""
    headers = [("Content-Type", "text/event-stream"), ("Content-Length", str(len(ECHO_ERROR)))]
    outcome = h.drive_response(addon, run(sts_call).flow, 400, headers, ECHO_ERROR)
    assert (outcome.kind, secrets_in(outcome.delivered), outcome.delivered.count(b"[REDACTED]")) == ("sent", [], 2)


def test_gzip_chunked_error_body_scrubbed():
    gz = content_encoding.encode(ECHO_ERROR, "gzip")
    outcome = h.drive_response(addon, run(sts_call).flow, 403, CHUNKED_XML + [("Content-Encoding", "gzip")], gz, chunk=11)
    plain = content_encoding.decode(outcome.delivered, "gzip")
    assert (outcome.kind, secrets_in(plain), plain.count(b"[REDACTED]")) == ("streamed", [], 2)


def test_undecodable_chunked_error_body_killed():
    garbage = b"not gzip " + h.REAL_TOKEN.encode()
    outcome = h.drive_response(addon, run(sts_call).flow, 403, CHUNKED_XML + [("Content-Encoding", "gzip")], garbage)
    assert (outcome.kind, outcome.delivered) == ("killed-after-headers", b"")


def test_chunked_error_body_not_truncated():
    """The scrubbed body arrives whole: only the secrets change (the hold-back keeps nothing here)."""
    outcome = h.drive_response(addon, run(sts_call).flow, 403, CHUNKED_XML, ECHO_ERROR, chunk=7)
    expected = ECHO_ERROR.replace(h.REAL_TOKEN.encode(), b"[REDACTED]").replace(h.REAL_AKID.encode(), b"[REDACTED]")
    assert outcome.delivered == expected


def test_h2_empty_data_frame_mid_body_then_more_data_killed():
    """Review r2 M-1: over HTTP/2 an empty DATA frame mid-body reads as the end. The scrubber
    releases what it has, minus the start of the token it cut, then fails on the data that
    follows, and the flow is killed: no part of the token leaves."""
    token_at = ECHO_ERROR.index(h.REAL_TOKEN.encode())
    split = token_at + len(h.REAL_TOKEN) // 2
    parts = [ECHO_ERROR[:split], b"", ECHO_ERROR[split:]]
    outcome = h.drive_response(
        addon, run(sts_call).flow, 403, [("Content-Type", "application/xml")], b"", parts=parts, http_version="HTTP/2.0"
    )
    assert (
        outcome.kind,
        secrets_in(outcome.delivered),
        h.REAL_TOKEN[:8].encode() in outcome.delivered,
        outcome.delivered.endswith(b"x-amz-security-token:"),
    ) == ("killed-after-headers", [], False, True)


def test_h2_empty_final_data_frame_keeps_the_redacted_flag(caplog):
    """An empty END_STREAM DATA frame, then end-of-message: two b"" calls. The second must not
    reset the `redacted` flag, so the redacted line is still logged and nothing is killed."""
    parts = [ECHO_ERROR, b""]
    with caplog.at_level(logging.DEBUG):
        outcome = h.drive_response(
            addon, run(sts_call).flow, 403, [("Content-Type", "application/xml")], b"", parts=parts, http_version="HTTP/2.0"
        )
    redacted_lines = [r for r in caplog.records if r.getMessage().startswith("credential proxy: [sigv4] redacted")]
    assert (outcome.kind, secrets_in(outcome.delivered), len(redacted_lines)) == ("streamed", [], 1)


def test_chunked_success_body_streamed_untouched():
    """Only errors are held: a chunked 200 streams through as it came (ConverseStream, downloads)."""
    body = b"<ok>" + h.REAL_TOKEN.encode() + b"</ok>" * 1000
    outcome = h.drive_response(addon, run(sts_call).flow, 200, CHUNKED_XML, body, chunk=100)
    assert (outcome.kind, outcome.delivered) == ("streamed", body)


def test_error_streamed_past_the_addon_killed():
    """Defensive: a signed 4xx that is streamed without the addon's scrubber is killed at `response`."""
    flow = run(sts_call).flow
    flow.response = http.Response.make(403, b"")
    flow.response.stream = True
    flow.response.data.content = None
    addon.response(flow)
    assert h.killed(flow)


def test_success_body_untouched():
    body = b"<ok>" + h.REAL_TOKEN.encode() + b"</ok>"
    assert signed_flow_with_response(http.Response.make(200, body)).response.content == body


def test_error_body_of_a_flow_not_signed_here_untouched():
    """Only flows this addon signed: a presigned request's error is passed through as it came."""
    flow, body = PRESIGNED["presigned-sigv4"]()
    h.drive(addon, flow, body)
    error = b"<Error>" + h.REAL_TOKEN.encode() + b"</Error>"
    flow.response = http.Response.make(403, error)
    addon.response(flow)
    assert flow.response.content == error


def test_redaction_error_fails_closed(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("injected redaction failure")

    monkeypatch.setattr(addon, "_redact_error_body", broken)
    assert h.killed(signed_flow_with_response(http.Response.make(403, b"<Error/>")))


def test_flow_without_carriers_untouched():
    """The image is server-wide: traffic with no AWS binding (GitHub, Anthropic) is not changed."""
    flow, body = h.client_flow(
        "POST", "api.github.com", "/graphql", service="none", sign=False, carriers=False,
        auth="Bearer placeholder", body=b"{}",
    )
    before = list(flow.request.headers.items(multi=True))
    outcome = h.drive(addon, flow, body)
    assert (outcome.sent.host, outcome.sent.headers) == (h.DIALED_IP, before)


def test_resign_logged_at_warning(caplog):
    """The re-sign line is a WARNING, the level that shows in the egress sidecar's log."""
    with caplog.at_level(logging.DEBUG):
        run(sts_call)
    assert [r.levelno for r in caplog.records if r.getMessage().startswith("credential proxy: [sigv4] re-signed")] == [
        logging.WARNING
    ]


@pytest.mark.parametrize("name", sorted(ALL))
def test_every_log_line_starts_with_credential_proxy(name, caplog):
    """The egress process copies mitmdump output into the sidecar log only for lines that start
    with `credential proxy:` (found on the cluster in part 4)."""
    with caplog.at_level(logging.DEBUG):
        run(ALL[name])
    lines = [r.getMessage() for r in caplog.records if r.name == "root"]
    assert (bool(lines), [m for m in lines if not m.startswith("credential proxy: [sigv4] ")]) == (True, [])


def test_log_line_flushed():
    """mitmproxy's terminal log print()s to a block-buffered pipe and never flushes; the addon
    flushes after each line, so it reaches the sidecar log at once, not in batches."""

    class Recorder(io.StringIO):
        flushes = 0

        def flush(self):
            Recorder.flushes += 1
            super().flush()

    class PrintHandler(logging.Handler):
        """Like mitmproxy 11.0.2's TermLogHandler: print() to `.file`, never flush."""

        def __init__(self, file):
            super().__init__()
            self.file = file

        def emit(self, record):
            print(self.format(record), file=self.file)

    handler = PrintHandler(Recorder())
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        run(sts_call)
    finally:
        root.removeHandler(handler)
    assert (Recorder.flushes >= 1, "credential proxy: [sigv4] re-signed POST" in handler.file.getvalue()) == (True, True)


@pytest.mark.parametrize("name", sorted(ALL))
def test_logs_never_contain_secrets(name, caplog):
    with caplog.at_level(logging.DEBUG):
        run(ALL[name])
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert not [s for s in (h.REAL_AKID, h.REAL_SECRET, h.REAL_TOKEN) if s in text]


def test_test_keys_do_not_match_secret_scan():
    """Ruling C6: the placeholder and test keys must not trip the series' secret scan."""
    pattern = re.compile(r"(AKIA|ASIA)[0-9A-Z]{16}")
    assert not [k for k in (h.PLACEHOLDER_AKID, h.REAL_AKID) if pattern.search(k)]


# --- the pure functions the hooks use -----------------------------------------------------------


def test_parse_scope_reads_the_client_scope():
    auth = (
        "AWS4-HMAC-SHA256 Credential=AKIA_OSB_PLACEHOLDER/20261101/us-east-1/bedrock/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, Signature=" + "a" * 64
    )
    assert addon.parse_scope(auth) == {
        "akid": "AKIA_OSB_PLACEHOLDER",
        "date": "20261101",
        "region": "us-east-1",
        "service": "bedrock",
        "signed": ("content-type", "host", "x-amz-date"),
    }


@pytest.mark.parametrize("auth", ["", "Bearer x", "AWS AKIA_OSB_PLACEHOLDER:c2ln", "AWS4-HMAC-SHA256 Credential=x"])
def test_parse_scope_rejects_other_schemes(auth):
    assert addon.parse_scope(auth) is None


@pytest.mark.parametrize(
    "query,expected",
    [
        ("X-Amz-Signature=abc", True),
        ("x-amz-credential=abc", True),
        ("AWSAccessKeyId=a&Signature=b&Expires=1", True),
        ("list-type=2&prefix=a", False),
        ("", False),
    ],
)
def test_is_query_auth(query, expected):
    assert addon.is_query_auth(query) is expected


def test_get_caller_identity_is_not_vending():
    assert addon.is_credential_vending("sts", http.Headers(), "", b"Action=GetCallerIdentity&Version=2011-06-15") is False
