"""Unit tests for broker.py. boto3 (STS) and the OpenSandbox SDK are mocked: nothing here
talks to AWS or a cluster.

    uv run --no-project --with-requirements broker/requirements.txt --with pytest pytest broker/ -v
"""

import ast
import base64
import dataclasses
import fnmatch
import http.client
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.exceptions import ClientError
from opensandbox.exceptions import SandboxApiException

import broker

ACCOUNT = "123456789012"
BUCKET = "osb-test-bucket"
MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
VENDING_MINIMUM = [  # the brief's list (Review Focus 3), as concrete actions
    "sts:AssumeRole", "sts:AssumeRoleWithSAML", "sts:AssumeRoleWithWebIdentity",
    "sts:GetSessionToken", "sts:GetFederationToken", "s3express:CreateSession",
    "ecr:GetAuthorizationToken", "codeartifact:GetAuthorizationToken", "redshift:GetClusterCredentials",
    "secretsmanager:GetSecretValue", "iam:CreateAccessKey", "iam:PassRole",
    "sts:GetServiceBearerToken",  # the bearer token behind CodeArtifact and ECR Public logins
]
# Every STS action there is, as IAM Access Analyzer validate-policy accepted them on 2026-11-01
# (each prefix sts:A* to sts:Z* probed; INVALID_ACTION for the rest).
STS_ACTIONS = [
    "AssumeRole", "AssumeRoleWithSAML", "AssumeRoleWithWebIdentity", "AssumeRoot",
    "DecodeAuthorizationMessage", "GetAccessKeyInfo", "GetCallerIdentity", "GetDelegatedAccessToken",
    "GetFederationToken", "GetServiceBearerToken", "GetSessionToken", "GetWebIdentityToken",
    "SetContext", "SetSourceIdentity", "TagSession",
]
HERE = Path(broker.__file__).parent
CREDS = {
    "AccessKeyId": "ASIATEST_FAKE_KEY_01",
    "SecretAccessKey": "secret-value-never-printed",
    "SessionToken": "token-value-never-printed",
    "Expiration": datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc),
}


def denied_by(action, patterns):
    """IAM action matching: case-insensitive, with * and ? wildcards."""
    return any(fnmatch.fnmatchcase(action.lower(), pattern.lower()) for pattern in patterns)


@pytest.fixture(autouse=True)
def config(monkeypatch):
    cfg = broker.Config(
        sandbox_role_arn=f"arn:aws:iam::{ACCOUNT}:role/osb-sandbox",
        region="us-east-1",
        test_bucket=BUCKET,
        ddb_table="osb-cred-test",
        bedrock_model_id=MODEL,
        server_domain="opensandbox-server.opensandbox-system.svc:80",
        api_key="test-api-key",
    )
    monkeypatch.setattr(broker, "CONFIG", cfg)
    return cfg


@pytest.fixture
def sts(monkeypatch):
    client = MagicMock()
    client.assume_role.return_value = {"Credentials": dict(CREDS)}
    monkeypatch.setattr(broker, "aws_client", lambda service, credentials=None: client)
    return client


def fake_sandbox(sandbox_id="2f6c0e9a-1b2c-4d5e-8f90-a1b2c3d4e5f6", revision=1):
    sb = MagicMock()
    sb.id = sandbox_id
    sb.credential_vault.create.return_value.revision = revision
    sb.credential_vault.patch.return_value.revision = revision + 1
    sb.credential_vault.get.return_value.revision = revision
    sb.get_endpoint.side_effect = lambda port: MagicMock(endpoint=f"10.60.1.7:{port}")
    sb.get_info.return_value.status.state = "Running"
    return sb


def api_error(status):
    return SandboxApiException(message=f"HTTP {status}", status_code=status)


# --- assume_for_sandbox and the session policy --------------------------------------


def test_assume_for_sandbox_call(sts):
    sandbox_id = "x" * 70
    assert broker.assume_for_sandbox(sandbox_id) == CREDS
    kwargs = sts.assume_role.call_args.kwargs
    assert kwargs["RoleArn"] == f"arn:aws:iam::{ACCOUNT}:role/osb-sandbox"
    assert kwargs["RoleSessionName"] == "x" * 64
    assert kwargs["DurationSeconds"] == 3600


def test_session_policy_fits_and_denies_vending(sts):
    broker.assume_for_sandbox("sbx-1")
    text = sts.assume_role.call_args.kwargs["Policy"]
    assert len(text) <= 2048
    policy = json.loads(text)
    denies = [s for s in policy["Statement"] if s["Effect"] == "Deny"]
    assert len(denies) == 1 and denies[0]["Resource"] == "*"
    assert [a for a in VENDING_MINIMUM if not denied_by(a, denies[0]["Action"])] == []
    # One list for the role (Terraform reads the same file) and the session policy.
    shared = json.loads((Path(broker.__file__).parent / "vending_actions.json").read_text())
    assert denies[0]["Action"] == shared


def test_session_policy_allows_only_the_test_resources(sts):
    broker.assume_for_sandbox("sbx-1")
    policy = json.loads(sts.assume_role.call_args.kwargs["Policy"])
    allows = {
        (tuple(sorted(s["Action"] if isinstance(s["Action"], list) else [s["Action"]])),
         tuple(sorted(s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]])))
        for s in policy["Statement"] if s["Effect"] == "Allow"
    }
    assert allows == {
        (("s3:ListBucket",), (f"arn:aws:s3:::{BUCKET}",)),
        (("s3:AbortMultipartUpload", "s3:DeleteObject", "s3:GetObject", "s3:PutObject"), (f"arn:aws:s3:::{BUCKET}/*",)),
        (("dynamodb:*Item", "dynamodb:Query", "dynamodb:Scan"),
         (f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/osb-cred-test",)),
        (("bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"), (
            "arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
            f"arn:aws:bedrock:us-east-1:{ACCOUNT}:inference-profile/{MODEL}",
        )),
        (("bedrock:CallWithBearerToken",), ("*",)),
    }
    bearer = next(s for s in policy["Statement"] if s.get("Action") == ["bedrock:CallWithBearerToken"])
    assert bearer["Condition"] == {"StringEquals": {"bedrock:bearerTokenType": "SHORT_TERM"}}


# --- vault carriers and bindings -------------------------------------------------------


def test_aws_hosts():
    assert broker.AWS_HOSTS == [
        "sts.amazonaws.com",
        "sts.us-east-1.amazonaws.com",
        "s3.us-east-1.amazonaws.com",
        "*.s3.us-east-1.amazonaws.com",
        "s3.amazonaws.com",
        "*.s3.amazonaws.com",
        "dynamodb.us-east-1.amazonaws.com",
        "bedrock-runtime.us-east-1.amazonaws.com",
    ]


def test_write_vault_carriers_and_binding():
    sb = fake_sandbox()
    assert broker.write_vault(sb, CREDS) == 1
    kwargs = sb.credential_vault.create.call_args.kwargs
    assert kwargs["credentials"] == [
        {"name": "aws-akid", "source": {"type": "inline", "value": CREDS["AccessKeyId"]}},
        {"name": "aws-secret", "source": {"type": "inline", "value": CREDS["SecretAccessKey"]}},
        {"name": "aws-token", "source": {"type": "inline", "value": CREDS["SessionToken"]}},
    ]
    assert kwargs["bindings"] == [
        {
            "name": "aws-sigv4",
            "match": {
                "schemes": ["https"],
                "hosts": broker.AWS_HOSTS,
                "methods": ["GET", "HEAD", "POST", "PUT", "DELETE"],
                "paths": ["/*"],
            },
            "auth": {
                "type": "customHeaders",
                "headers": [
                    {"name": "x-osb-aws-access-key-id", "credential": "aws-akid"},
                    {"name": "x-osb-aws-secret-access-key", "credential": "aws-secret"},
                    {"name": "x-osb-aws-session-token", "credential": "aws-token"},
                ],
            },
        }
    ]


def test_refresh_vault_replaces_the_three_credentials():
    sb = fake_sandbox(revision=4)
    assert broker.refresh_vault(sb, CREDS, expected_revision=4) == 5
    sb.credential_vault.patch.assert_called_once_with(
        expected_revision=4,
        credentials={"replace": broker.vault_credentials(CREDS)},
    )
    names = [c["name"] for c in broker.vault_credentials(CREDS)]
    assert names == ["aws-akid", "aws-secret", "aws-token"]


def test_bedrock_bearer_binding_takes_the_bedrock_host_only():
    sb = fake_sandbox()
    broker.write_vault(sb, CREDS, bedrock_key="bedrock-api-key-real")
    kwargs = sb.credential_vault.create.call_args.kwargs
    assert kwargs["credentials"][-1] == {
        "name": "bedrock-key", "source": {"type": "inline", "value": "bedrock-api-key-real"}}
    sigv4, bearer = kwargs["bindings"]
    assert bearer == {
        "name": "bedrock-bearer",
        "match": {
            "schemes": ["https"],
            "hosts": ["bedrock-runtime.us-east-1.amazonaws.com"],
            "methods": ["GET", "HEAD", "POST", "PUT", "DELETE"],
            "paths": ["/*"],
        },
        "auth": {"type": "bearer", "credential": "bedrock-key"},
    }
    # Two bindings on one host at the same precedence are refused as ambiguous.
    assert not set(sigv4["match"]["hosts"]) & set(bearer["match"]["hosts"])
    assert set(sigv4["match"]["hosts"]) | set(bearer["match"]["hosts"]) == set(broker.AWS_HOSTS)


def test_mint_bedrock_key():
    key = broker.mint_bedrock_key(CREDS)
    assert key.startswith("bedrock-api-key-")
    url = base64.b64decode(key.removeprefix("bedrock-api-key-")).decode()
    assert url.startswith("bedrock.amazonaws.com/?") and url.endswith("&Version=1")
    query = parse_qs(urlsplit("https://" + url).query)
    assert query["Action"] == ["CallWithBearerToken"]
    assert query["X-Amz-Expires"] == ["3600"]
    assert query["X-Amz-Credential"][0].startswith(CREDS["AccessKeyId"] + "/")
    assert query["X-Amz-Credential"][0].endswith("/us-east-1/bedrock/aws4_request")
    assert query["X-Amz-Security-Token"] == [CREDS["SessionToken"]]
    assert CREDS["SecretAccessKey"] not in url


# --- the sandbox the broker creates ------------------------------------------------------


def test_placeholder_key_does_not_match_the_secret_scan():
    assert not re.search(r"AKIA[0-9A-Z]{16}", broker.PLACEHOLDER_ACCESS_KEY_ID)
    assert broker.sandbox_env()["AWS_ACCESS_KEY_ID"] == broker.PLACEHOLDER_ACCESS_KEY_ID


def test_create_sandbox_network_policy_env_and_vault(sts):
    sb = fake_sandbox()
    create = MagicMock(return_value=sb)
    b = broker.Broker(create_sandbox=create, start_loop=False)
    reply = b.create(image="registry/opensandbox-eks/python:3.12-slim", metadata={"run": "r1"})
    kwargs = create.call_args.kwargs
    assert kwargs["network_policy"].default_action == "deny"
    assert [(r.action, r.target) for r in kwargs["network_policy"].egress] == [("allow", h) for h in broker.AWS_HOSTS]
    assert kwargs["credential_proxy"].enabled is True
    env = kwargs["env"]
    assert env["AWS_ACCESS_KEY_ID"] == broker.PLACEHOLDER_ACCESS_KEY_ID
    assert env["AWS_CA_BUNDLE"] == "/opt/opensandbox/merged-ca-certificates.pem"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert "AWS_ACCOUNT_ID" not in env and "AWS_SESSION_TOKEN" not in env
    assert "AWS_BEARER_TOKEN_BEDROCK" not in env
    assert kwargs["metadata"] == {
        "run": "r1", "example": "opensandbox-aws-credentials", "owner": "osb-broker", "bedrock_bearer": "false"}
    sb.credential_vault.create.assert_called_once()
    assert reply["sandbox_id"] == sb.id
    assert reply["endpoints"] == {"execd": "10.60.1.7:44772", "egress": "10.60.1.7:18080"}
    assert reply["revision"] == 1
    assert reply["akid_prefix"] == "ASIA…"
    for value in (CREDS["SecretAccessKey"], CREDS["SessionToken"], CREDS["AccessKeyId"]):
        assert value not in json.dumps(reply)


def test_create_with_bedrock_bearer(sts):
    sb = fake_sandbox()
    create = MagicMock(return_value=sb)
    b = broker.Broker(create_sandbox=create, start_loop=False)
    b.create(image="img", bedrock_bearer=True)
    env = create.call_args.kwargs["env"]
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == broker.BEARER_PLACEHOLDER
    credentials = sb.credential_vault.create.call_args.kwargs["credentials"]
    assert credentials[-1]["name"] == "bedrock-key"
    assert credentials[-1]["source"]["value"].startswith("bedrock-api-key-")


def test_create_kills_the_sandbox_when_the_vault_write_fails(sts):
    sb = fake_sandbox()
    sb.credential_vault.create.side_effect = api_error(412)
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    with pytest.raises(SandboxApiException):
        b.create(image="img")
    sb.kill.assert_called_once()
    assert b.list() == []


# --- refresh and revisions (Review Focus 4) ---------------------------------------------


def test_refresh_interval_is_half_the_session():
    assert broker.SESSION_SECONDS == 3600
    assert broker.REFRESH_SECONDS == 1800


def test_refresh_uses_the_known_revision_and_moves_expiry(sts):
    sb = fake_sandbox(revision=1)
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    later = dict(CREDS, AccessKeyId="ASIANEWK_FAKE_KEY_02", Expiration=CREDS["Expiration"] + timedelta(minutes=30))
    sts.assume_role.return_value = {"Credentials": later}
    view = b.refresh(sb.id)
    assert sb.credential_vault.patch.call_args.kwargs["expected_revision"] == 1
    assert view["revision"] == 2
    assert view["akid_prefix"] == "ASIA…"
    assert view["expires_at"] == "2026-11-01T06:30:00Z"


def test_refresh_with_a_stale_revision_surfaces_the_vault_status(sts):
    sb = fake_sandbox(revision=1)
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    sb.credential_vault.patch.side_effect = api_error(409)
    with pytest.raises(broker.VaultConflict) as caught:
        b.refresh(sb.id, expected_revision=0)
    assert (caught.value.status, caught.value.expected_revision, caught.value.revision) == (409, 0, 1)
    assert b.get(sb.id)["revision"] == 1  # the broker's own state is untouched


def test_loop_rotation_recreates_a_vault_the_sidecar_lost(sts):
    sb = fake_sandbox(revision=3)
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    sb.credential_vault.patch.side_effect = api_error(404)
    sb.credential_vault.create.return_value.revision = 1
    b.rotate(sb.id)  # what the refresh loop calls
    assert sb.credential_vault.create.call_count == 2
    assert b.get(sb.id)["revision"] == 1


def test_loop_rotation_rereads_the_revision_after_a_conflict(sts):
    sb = fake_sandbox(revision=1)
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    sb.credential_vault.get.return_value.revision = 7
    sb.credential_vault.patch.side_effect = [api_error(409), MagicMock(revision=8)]
    b.rotate(sb.id)
    assert [c.kwargs["expected_revision"] for c in sb.credential_vault.patch.call_args_list] == [1, 7]
    assert b.get(sb.id)["revision"] == 8


def test_refresh_loop_rotates_when_due_and_stops_on_delete(sts, monkeypatch):
    monkeypatch.setattr(broker, "REFRESH_SECONDS", 0.2)
    sb = fake_sandbox(revision=1)
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=True)
    b.create(image="img")
    assert sb.credential_vault.patch.call_count == 0  # not before it is due
    deadline = time.monotonic() + 3
    while sb.credential_vault.patch.call_count == 0 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert sb.credential_vault.patch.call_count >= 1
    b.delete(sb.id)
    rotations = sb.credential_vault.patch.call_count
    time.sleep(0.5)
    assert sb.credential_vault.patch.call_count == rotations


def test_loop_rotation_never_patches_without_a_revision(sts):
    sb = fake_sandbox(revision=1)
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    sb.credential_vault.patch.side_effect = api_error(409)
    sb.credential_vault.get.side_effect = api_error(500)  # the current revision can't be read
    with pytest.raises(SandboxApiException):
        b.rotate(sb.id)
    assert [c.kwargs["expected_revision"] for c in sb.credential_vault.patch.call_args_list] == [1]


def test_adopt_takes_over_running_sandboxes_after_a_restart(monkeypatch):
    def info(sandbox_id, state, bearer):
        metadata = {**broker.OWN_METADATA, "bedrock_bearer": bearer}
        return MagicMock(id=sandbox_id, status=MagicMock(state=state), metadata=metadata)

    manager = MagicMock()
    manager.__enter__.return_value = manager
    manager.list_sandbox_infos.return_value = MagicMock(
        sandbox_infos=[info("sbx-a", "Running", "true"), info("sbx-b", "Terminating", "false")],
        pagination=MagicMock(has_next_page=False),
    )
    monkeypatch.setattr(broker, "SandboxManagerSync", MagicMock(create=MagicMock(return_value=manager)))
    connect = MagicMock(return_value=fake_sandbox("sbx-a", revision=6))
    b = broker.Broker(create_sandbox=MagicMock(), connect_sandbox=connect, start_loop=False)
    assert b.adopt() == 1
    assert manager.list_sandbox_infos.call_args.args[0].metadata == broker.OWN_METADATA
    assert connect.call_args.args[0] == "sbx-a"
    view = b.get("sbx-a")
    assert (view["revision"], view["bedrock_bearer"], view["next_rotation_in_seconds"]) == (6, True, 0)


def test_delete_keeps_the_sandbox_when_the_kill_fails(sts):
    sb = fake_sandbox()
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    sb.kill.side_effect = api_error(500)
    with pytest.raises(SandboxApiException):
        b.delete(sb.id)
    assert b.get(sb.id)["vault"] == "active"


def test_revoke_keeps_rotating_when_the_vault_delete_fails(sts):
    sb = fake_sandbox()
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    sb.credential_vault.delete.side_effect = api_error(500)
    with pytest.raises(SandboxApiException):
        b.revoke(sb.id)
    assert b.get(sb.id)["vault"] == "active"
    b.refresh(sb.id)  # still allowed


def test_get_reports_state_revision_prefix_and_expiry(sts):
    sb = fake_sandbox()
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    view = b.get(sb.id)
    assert view["state"] == "Running"
    assert view["revision"] == 1
    assert view["akid_prefix"] == "ASIA…"
    assert view["expires_at"] == "2026-11-01T06:00:00Z"
    assert view["vault"] == "active"


def test_revoke_deletes_the_vault_and_stops_refreshing(sts):
    sb = fake_sandbox()
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    b.revoke(sb.id)
    sb.credential_vault.delete.assert_called_once()
    assert b.get(sb.id)["vault"] == "revoked"
    with pytest.raises(broker.Revoked):
        b.refresh(sb.id)


def test_delete_kills_the_sandbox(sts):
    sb = fake_sandbox()
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    b.delete(sb.id)
    sb.kill.assert_called_once()
    assert b.list() == []
    with pytest.raises(KeyError):
        b.get(sb.id)


# --- HTTP API ------------------------------------------------------------------------------


@pytest.fixture
def api(sts):
    sb = fake_sandbox()
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), broker.make_handler(b))
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", sb, b
    server.shutdown()
    server.server_close()


def call(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            return reply.status, json.loads(reply.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_http_create_get_refresh_conflict_delete(api):
    base, sb, _ = api
    status, created = call(base, "POST", "/sandboxes", {"image": "img"})
    assert status == 201 and created["sandbox_id"] == sb.id
    status, view = call(base, "GET", f"/sandboxes/{sb.id}")
    assert status == 200 and view["revision"] == 1
    status, refreshed = call(base, "POST", f"/sandboxes/{sb.id}/refresh", {})
    assert status == 200 and refreshed["revision"] == 2
    sb.credential_vault.patch.side_effect = api_error(409)
    sb.credential_vault.get.return_value.revision = 2
    status, conflict = call(base, "POST", f"/sandboxes/{sb.id}/refresh", {"expected_revision": 1})
    assert status == 409
    assert conflict == {"error": "vault revision conflict", "vault_status": 409, "expected_revision": 1, "revision": 2}
    status, _ = call(base, "DELETE", f"/sandboxes/{sb.id}")
    assert status == 200
    assert call(base, "GET", f"/sandboxes/{sb.id}")[0] == 404


def test_http_rejects_a_request_without_an_image(api):
    base, _, _ = api
    status, body = call(base, "POST", "/sandboxes", {"bedrock_bearer": True})
    assert status == 400 and "image" in body["error"]


# --- the live Deny check (run in the broker pod in Step 5) ---------------------------------


def denied(message, code="AccessDenied"):
    return ClientError({"Error": {"Code": code, "Message": message}}, "op")


def test_deny_check_passes_on_explicit_denies(monkeypatch, capsys):
    monkeypatch.setattr(broker, "assume_for_sandbox", lambda sandbox_id: dict(CREDS))
    clients = MagicMock()
    error = denied("User: arn:aws:sts::123456789012:assumed-role/osb-sandbox/osb-deny-check is not authorized "
                   "to perform: x with an explicit deny in an identity-based policy")
    clients.assume_role.side_effect = error
    clients.get_authorization_token.side_effect = denied(error.response["Error"]["Message"], "AccessDeniedException")
    clients.get_secret_value.side_effect = denied(error.response["Error"]["Message"], "AccessDeniedException")
    clients.list_roles.side_effect = error
    monkeypatch.setattr(broker, "aws_client", lambda service, credentials=None: clients)
    assert broker.deny_check() == 0
    out = capsys.readouterr().out
    assert out.count("explicit deny") >= 4
    assert CREDS["SecretAccessKey"] not in out and CREDS["SessionToken"] not in out


def explicit_denies():
    clients = MagicMock()
    for call in ("assume_role", "get_authorization_token", "get_secret_value", "list_roles"):
        getattr(clients, call).side_effect = denied("... with an explicit deny in a session policy")
    return clients


def test_deny_check_fails_when_a_call_is_allowed(monkeypatch, capsys):
    monkeypatch.setattr(broker, "assume_for_sandbox", lambda sandbox_id: dict(CREDS))
    clients = explicit_denies()
    clients.assume_role.side_effect = None
    clients.assume_role.return_value = {"Credentials": dict(CREDS)}  # a real credential came back
    monkeypatch.setattr(broker, "aws_client", lambda service, credentials=None: clients)
    assert broker.deny_check() == 1
    out = capsys.readouterr().out
    assert "ALLOWED" in out
    assert CREDS["SecretAccessKey"] not in out and CREDS["SessionToken"] not in out


def test_deny_check_fails_on_an_implicit_deny(monkeypatch, capsys):
    # Refused, but not by the Deny: the Deny statement is missing or does not match.
    monkeypatch.setattr(broker, "assume_for_sandbox", lambda sandbox_id: dict(CREDS))
    clients = explicit_denies()
    clients.get_authorization_token.side_effect = denied(
        "... because no identity-based policy allows the ecr:GetAuthorizationToken action", "AccessDeniedException")
    monkeypatch.setattr(broker, "aws_client", lambda service, credentials=None: clients)
    assert broker.deny_check() == 1
    assert "not an explicit deny" in capsys.readouterr().out


def test_deny_check_fails_when_authorization_passed(monkeypatch):
    # ResourceNotFound means AWS let the call through to the secret lookup.
    monkeypatch.setattr(broker, "assume_for_sandbox", lambda sandbox_id: dict(CREDS))
    clients = explicit_denies()
    clients.get_secret_value.side_effect = denied("Secrets Manager can't find the specified secret.",
                                                  "ResourceNotFoundException")
    monkeypatch.setattr(broker, "aws_client", lambda service, credentials=None: clients)
    assert broker.deny_check() == 1


# --- review round 1 ---------------------------------------------------------------------------
# One Deny for the role, the session and the addon (Task 8 ruling) -----------------------------


def _addon_constant(tree, name):
    """A module-level constant of the egress addon, read from its source: the addon imports
    mitmproxy, so it can't be imported here."""
    def value(node):
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "frozenset":
            return {item.lower() for item in ast.literal_eval(node.args[0])}
        if isinstance(node, ast.Dict):
            return {ast.literal_eval(k): value(v) for k, v in zip(node.keys, node.values, strict=True)}
        return ast.literal_eval(node)

    for node in tree.body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == [name]:
            return value(node.value)
    raise AssertionError(f"{name} not found in the addon")


def test_the_deny_covers_the_addon_blocklist():
    addon = HERE.parent / "egress" / "sigv4_resign.py"
    if not addon.is_file():
        pytest.skip("egress/sigv4_resign.py is not next to broker/")
    tree = ast.parse(addon.read_text())
    deny = broker.VENDING_ACTIONS
    blocked = [f"{service}:{action}" for service, actions in _addon_constant(tree, "VENDING_ACTIONS").items()
               for action in sorted(actions)]
    assert [a for a in blocked if not denied_by(a, deny)] == []
    # The addon treats STS as an allowlist: whatever it lets through, the Deny lets through too.
    allowed = _addon_constant(tree, "STS_ALLOWED_ACTIONS")
    assert [a for a in STS_ACTIONS if (a.lower() in allowed) == denied_by(f"sts:{a}", deny)] == []


def test_role_and_session_policies_are_rendered_from_the_same_files():
    main_tf = (HERE.parent / "terraform" / "main.tf").read_text()
    assert 'file("${path.module}/../broker/vending_actions.json")' in main_tf
    assert 'templatefile("${path.module}/../broker/sandbox_permissions.json"' in main_tf
    template = (HERE / "sandbox_permissions.json").read_text()
    assert set(re.findall(r"\$\{(\w+)\}", template)) == set(broker.permission_values())
    allows = [s for s in json.loads(broker.session_policy())["Statement"] if s["Effect"] == "Allow"]
    assert allows == broker.sandbox_permissions()


def test_session_policy_stays_under_the_limit_with_long_names(monkeypatch, config):
    # The longest bucket name S3 allows; the policy must still fit.
    monkeypatch.setattr(broker, "CONFIG", dataclasses.replace(config, test_bucket="b" * 63))
    assert len(broker.session_policy()) <= 2048


# The refresh loop ends for a sandbox that is gone (Important 1) -------------------------------


def creds_expiring(minutes):
    return dict(CREDS, Expiration=datetime.now(timezone.utc) + timedelta(minutes=minutes))


def test_loop_drops_a_sandbox_the_server_no_longer_has(sts, caplog):
    sb = fake_sandbox()
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    sb.get_info.side_effect = api_error(404)
    calls = sts.assume_role.call_count
    with caplog.at_level(logging.INFO, logger="osb-broker"):
        assert b.loop_step(sb.id) is False
    assert sts.assume_role.call_count == calls  # no AssumeRole for a sandbox that is gone
    assert b.list() == []
    lines = [r.getMessage() for r in caplog.records if "dropped it" in r.getMessage()]
    assert lines == [f"sandbox {sb.id}: the server no longer has it (404); stopped rotating and dropped it"]


@pytest.mark.parametrize("state", ["Terminated", "Failed", "Stopping"])
def test_loop_drops_a_sandbox_that_ended(sts, state):
    sb = fake_sandbox()
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    sb.get_info.return_value.status.state = state
    assert b.loop_step(sb.id) is False
    assert b.list() == []
    sb.credential_vault.patch.assert_not_called()


def test_loop_retries_a_failing_rotation_while_the_credentials_last(sts):
    sts.assume_role.return_value = {"Credentials": creds_expiring(30)}
    sb = fake_sandbox()
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    sb.credential_vault.patch.side_effect = api_error(500)
    assert b.loop_step(sb.id) is True
    assert b.get(sb.id)["last_error"]
    assert 55 <= b.get(sb.id)["next_rotation_in_seconds"] <= 60


def test_loop_gives_up_once_the_last_credentials_expired(sts, caplog):
    sts.assume_role.return_value = {"Credentials": creds_expiring(-1)}
    sb = fake_sandbox()
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=False)
    b.create(image="img")
    sb.credential_vault.patch.side_effect = api_error(500)
    sb.credential_vault.create.side_effect = api_error(500)
    with caplog.at_level(logging.INFO, logger="osb-broker"):
        assert b.loop_step(sb.id) is False
    assert b.list() == []
    assert len([r for r in caplog.records if "dropped it" in r.getMessage()]) == 1


def test_refresh_loop_thread_ends_for_a_sandbox_that_is_gone(sts, monkeypatch):
    monkeypatch.setattr(broker, "REFRESH_SECONDS", 0.1)
    sb = fake_sandbox()
    b = broker.Broker(create_sandbox=MagicMock(return_value=sb), start_loop=True)
    b.create(image="img")
    sb.get_info.side_effect = api_error(404)
    deadline = time.monotonic() + 3
    while b.list() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert b.list() == []
    time.sleep(0.3)
    assert sts.assume_role.call_count == 1  # only the create's
    assert not [t for t in threading.enumerate() if t.name == f"rotate-{sb.id[:8]}"]


# A forced refresh re-creates a lost vault (minor 4) -------------------------------------------


def test_forced_refresh_recreates_a_lost_vault(api):
    base, sb, _ = api
    call(base, "POST", "/sandboxes", {"image": "img"})
    sb.credential_vault.patch.side_effect = api_error(404)
    status, view = call(base, "POST", f"/sandboxes/{sb.id}/refresh", {})
    assert status == 200 and view["revision"] == 1
    assert sb.credential_vault.create.call_count == 2


# Request bodies (minor 5) -------------------------------------------------------------------------


@pytest.mark.parametrize("length", ["-1", "abc", " "])
def test_http_bad_content_length_is_a_400_and_never_blocks(api, length):
    base, _, _ = api
    url = urlsplit(base)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=3)
    conn.putrequest("POST", "/sandboxes")
    conn.putheader("Content-Length", length)
    conn.endheaders()
    reply = conn.getresponse()
    assert reply.status == 400
    assert "Content-Length" in json.loads(reply.read())["error"]
    conn.close()


# No secret leaves the broker (minor 6), and no account ID either --------------------------------


def test_no_secret_in_the_logs_or_the_api_output(api, sts, caplog):
    base, sb, b = api
    sts.assume_role.return_value = {"Credentials": creds_expiring(30)}
    caplog.set_level(logging.DEBUG)
    outputs = [call(base, "POST", "/sandboxes", {"image": "img", "bedrock_bearer": True})]
    outputs.append(call(base, "POST", f"/sandboxes/{sb.id}/refresh", {}))
    outputs.append(call(base, "GET", f"/sandboxes/{sb.id}"))
    sb.credential_vault.patch.side_effect = api_error(500)
    assert b.loop_step(sb.id) is True  # a rotation that fails, the way the loop runs it
    outputs.append(call(base, "GET", f"/sandboxes/{sb.id}"))
    outputs.append(call(base, "GET", "/sandboxes"))
    minted = [c.kwargs["credentials"][-1]["source"]["value"] for c in sb.credential_vault.create.call_args_list]
    secrets = [CREDS["SecretAccessKey"], CREDS["SessionToken"], CREDS["AccessKeyId"], *minted]
    text = caplog.text + json.dumps(outputs)
    assert [s for s in secrets if s in text] == []
    assert "ASIA…" in text  # only the 4-character "ASIA" is shown, which says nothing about the account


def test_errors_leaving_the_broker_mask_the_account_id(api, sts, caplog):
    base, _, _ = api
    sts.assume_role.side_effect = ClientError({"Error": {"Code": "AccessDenied", "Message": (
        f"User: arn:aws:sts::{ACCOUNT}:assumed-role/osb-broker/eks-osb-eks is not authorized to perform: "
        f"sts:AssumeRole on resource: arn:aws:iam::{ACCOUNT}:role/osb-sandbox")}}, "AssumeRole")
    caplog.set_level(logging.DEBUG, logger="osb-broker")
    status, body = call(base, "POST", "/sandboxes", {"image": "img"})
    assert status == 502
    assert ACCOUNT not in body["error"] and "arn:aws:iam::<account>:role/osb-sandbox" in body["error"]
    assert ACCOUNT not in "".join(r.getMessage() for r in caplog.records if r.name == "osb-broker")


def test_deny_check_masks_the_account_id(monkeypatch, capsys):
    monkeypatch.setattr(broker, "assume_for_sandbox", lambda sandbox_id: dict(CREDS))
    clients = explicit_denies()
    clients.get_authorization_token.side_effect = denied(
        f"User: arn:aws:sts::{ACCOUNT}:assumed-role/osb-sandbox/osb-deny-check is not authorized", "Other")
    monkeypatch.setattr(broker, "aws_client", lambda service, credentials=None: clients)
    broker.deny_check()
    out = capsys.readouterr().out
    assert ACCOUNT not in out and "arn:aws:sts::<account>:assumed-role" in out


def test_the_image_carries_every_file_the_broker_reads():
    dockerfile = (HERE / "Dockerfile").read_text()
    allowed = (HERE / ".dockerignore").read_text().split()
    for name in ("broker.py", "sandbox_permissions.json", "vending_actions.json"):
        assert re.search(rf"^COPY .*\b{re.escape(name)}\b", dockerfile, re.M), name
        assert f"!{name}" in allowed, name


# AWS's AccessDenied for a call on a role (AssumeRole) names no reason, only an authorization id
# (observed in Step 5). The call alone can't tell an explicit deny from "nothing allows it" (the
# target role's trust names only the broker), so the probe needs AWS's authorization details.
ROLE_DENIAL = (f"User: arn:aws:sts::{ACCOUNT}:assumed-role/osb-sandbox/osb-deny-check is not authorized to perform: "
               f"sts:TagSession on resource: arn:aws:iam::{ACCOUNT}:role/osb-sandbox. Go to https://us-east-1.console."
               "aws.amazon.com/iam/home?region=us-east-1#/authorization-details/clnftocqxo4yd5nrre3yhekrr for complete "
               "details, or call the GetRequestAuthorizationDetails API with the following authorization id: "
               "clnftocqxo4yd5nrre3yhekrr")


def authorization_details(effect="EXPLICIT_DENY", deny_in=("IDENTITY_BASED_POLICY", "SESSION_POLICY")):
    """GetRequestAuthorizationDetails' answer, shaped as AWS returned it in Step 5."""
    types = ["IDENTITY_BASED_POLICY", "RESOURCE_BASED_POLICY", "SERVICE_CONTROL_POLICY", "SESSION_POLICY"]
    policies = [{"type": t, "uri": f"inline:{i}"} for i, t in enumerate(types)]
    matched = [{"uri": f"inline:{types.index(t)}",
                "matchedStatements": [{"sid": "NoCredentialVending", "evaluatedEffect": "DENY"}]} for t in deny_in]
    matched.append({"uri": "inline:2", "matchedStatements": [{"sid": "<Statement1>", "evaluatedEffect": "ALLOW"}]})
    return {"evaluations": [{"action": a, "resource": "arn:aws:iam::123456789012:role/osb-sandbox",
                             "evaluatedEffect": effect, "matchedPolicies": matched}
                            for a in ("sts:TagSession", "sts:AssumeRole")],
            "policies": policies}


def verdict_line(out, action):
    """The probe's verdict line ("  <action>  <verdict>"), not the note printed while it waits."""
    return next(line for line in out.splitlines() if line.startswith(f"  {action} "))


@pytest.fixture
def role_denial(monkeypatch):
    """deny_check() with a reasonless AssumeRole denial; returns the mocked clients."""
    monkeypatch.setattr(broker, "assume_for_sandbox", lambda sandbox_id: dict(CREDS))
    monkeypatch.setattr(broker, "AUTHZ_POLL_SECONDS", 0)
    clients = explicit_denies()
    clients.assume_role.side_effect = denied(ROLE_DENIAL)
    monkeypatch.setattr(broker, "aws_client", lambda service, credentials=None: clients)
    return clients


def test_a_reasonless_denial_alone_is_not_counted_as_deny_evidence(role_denial, capsys):
    role_denial.get_request_authorization_details.side_effect = denied("not allowed", "AccessDeniedException")
    assert broker.deny_check() == 0  # refused, so not a failure ...
    out = capsys.readouterr().out
    line = verdict_line(out, "sts:AssumeRole")
    assert "can't be told apart from no-allow by the call alone" in line
    assert not line.lstrip().split()[1].startswith("ok") and "covers" not in line
    assert "all 4 refused, each shown to be by the explicit Deny" not in out  # ... and not evidence
    assert "3 shown to be by the explicit Deny" in out


def test_authorization_details_prove_the_deny_in_both_policies(role_denial, capsys):
    role_denial.get_request_authorization_details.side_effect = [
        denied("No authorization details found", "ResourceNotFoundException"),  # not published yet
        authorization_details(),
    ]
    assert broker.deny_check() == 0
    out = capsys.readouterr().out
    assert role_denial.get_request_authorization_details.call_args.kwargs == {
        "authorizationId": "clnftocqxo4yd5nrre3yhekrr"}
    line = verdict_line(out, "sts:AssumeRole")
    assert line.split()[1] == "ok" and "explicit deny in the role policy and the session policy" in line
    assert "all 4 refused, each shown to be by the explicit Deny" in out


@pytest.mark.parametrize("details", [
    authorization_details(effect="IMPLICIT_DENY", deny_in=()),  # nothing allows it: no Deny matched
    authorization_details(deny_in=("IDENTITY_BASED_POLICY",)),  # the session policy's Deny is missing
])
def test_authorization_details_without_both_denies_fail(role_denial, capsys, details):
    role_denial.get_request_authorization_details.return_value = details
    assert broker.deny_check() == 1
    assert "FAIL" in verdict_line(capsys.readouterr().out, "sts:AssumeRole")


def test_details_that_never_arrive_are_not_evidence(role_denial, monkeypatch, capsys):
    monkeypatch.setattr(broker, "AUTHZ_WAIT_SECONDS", 0)
    role_denial.get_request_authorization_details.side_effect = denied("No authorization details found",
                                                                       "ResourceNotFoundException")
    assert broker.deny_check() == 0
    assert "can't be told apart" in capsys.readouterr().out


def test_main_logs_through_the_masking_formatter(monkeypatch):
    seen = {}
    monkeypatch.setattr(broker.logging, "basicConfig", lambda **kwargs: seen.update(kwargs))
    assert broker.main(["broker.py", "no-such-command"]) == 2
    assert [type(h.formatter) for h in seen["handlers"]] == [broker.MaskingFormatter]


# The ingress gateway is configured on this server, so without the server proxy the SDK gets the
# gateway's NLB endpoint (open to one laptop IP only) and a pod can't reach it (seen in Step 5).
def test_the_broker_reaches_sandboxes_through_the_server_proxy():
    config = broker.connection()
    assert config.use_server_proxy is True
    assert config.domain == "opensandbox-server.opensandbox-system.svc:80"


def test_log_lines_from_any_library_have_the_account_masked():
    record = logging.LogRecord("opensandbox.sync.sandbox", logging.INFO, __file__, 1,
                               "Creating sandbox with startup source: %s", (f"{ACCOUNT}.dkr.ecr.us-east-1/x",), None)
    line = broker.MaskingFormatter("%(name)s: %(message)s").format(record)
    assert ACCOUNT not in line and "<account>.dkr.ecr" in line
