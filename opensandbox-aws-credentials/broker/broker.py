"""The part 5 credential broker: AWS access for a sandbox without an AWS key in the sandbox.

It runs in the cluster (Deployment osb-broker, namespace opensandbox-broker) with the IAM role
osb-broker from EKS Pod Identity. For each sandbox it is asked for, it:

  1. creates the sandbox with placeholder AWS keys, a deny-by-default network policy that
     allows only AWS_HOSTS, and the egress credential proxy turned on;
  2. assumes osb-sandbox with the sandbox ID as the session name and an inline session policy
     (the test permissions, plus an explicit Deny on every action that hands out credentials);
  3. writes the temporary credentials into the sandbox's Credential Vault as three carrier
     headers: the egress sidecar adds them to requests for AWS_HOSTS, and the SigV4 addon in
     the sidecar turns them into a real signature and strips them;
  4. rotates them every REFRESH_SECONDS, half the one-hour session (the broker's own
     credentials are a role session, so its AssumeRole is role chaining, capped at one hour),
     with vault.patch carrying the vault's expected revision. Once the sandbox is gone (the
     server doesn't have it, or it ended), or rotating still fails after its last credentials
     expired, the broker stops rotating and drops it.

Real credentials exist here and in the egress sidecar, never in the sandbox. Nothing here
prints or logs a secret key, a session token or a whole access key ID.

The API listens on 127.0.0.1 only. A kubectl port-forward connects from inside the pod, so
it works, and nothing else in the cluster (sandboxes included) can call the broker:

  kubectl -n opensandbox-broker port-forward svc/osb-broker 18090:80

  POST   /sandboxes               {"image", "bedrock_bearer"?, "timeout_minutes"?, "metadata"?}
                                  (the "endpoints" it returns are the in-cluster server-proxy
                                  URLs the broker uses; from a laptop, connect by sandbox_id
                                  through your own port-forward to the server)
  GET    /sandboxes               the sandboxes this broker manages
  GET    /sandboxes/{id}          state, vault, revision, akid_prefix, expires_at, ...
  POST   /sandboxes/{id}/refresh  {"expected_revision"?}: rotate now; a stale revision answers
                                  with the vault's own conflict status
  DELETE /sandboxes/{id}/vault    revoke: delete the vault and stop rotating
  DELETE /sandboxes/{id}          stop rotating and delete the sandbox
  GET    /healthz

Two more commands, for kubectl exec: `python broker.py whoami` (the broker's identity) and
`python broker.py deny-check` (the credential-vending Deny, checked against AWS).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import string
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from botocore.exceptions import ClientError
from opensandbox import SandboxManagerSync, SandboxSync
from opensandbox.config.connection_sync import ConnectionConfigSync
from opensandbox.constants import DEFAULT_EGRESS_PORT, DEFAULT_EXECD_PORT
from opensandbox.exceptions import SandboxApiException
from opensandbox.models.sandboxes import CredentialProxyConfig, NetworkPolicy, NetworkRule, SandboxFilter

log = logging.getLogger("osb-broker")
# At DEBUG, botocore.auth logs every canonical request it signs, and for the presigned URL in a
# Bedrock key that includes the session token. Keep the AWS SDK's own logging at WARNING.
for _name in ("botocore", "boto3"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# --- Settings ----------------------------------------------------------------------------

SESSION_SECONDS = 3600  # the most a chained role session may last
REFRESH_SECONDS = SESSION_SECONDS // 2  # rotate at half the lifetime: 30 minutes
RETRY_SECONDS = 60  # after a failed rotation
READY_TIMEOUT_SECONDS = 120  # a first pull of the image on a node can take a while
DEFAULT_TIMEOUT_MINUTES = 60  # sandbox lifetime when the request names none
POLICY_MAX_CHARS = 2048  # STS limit on the inline session policy
LISTEN = os.environ.get("BROKER_LISTEN", "127.0.0.1:8080")

# The hosts a sandbox may reach, and the hosts its AWS requests get the carriers on (us-east-1,
# like the rest of the series). The vault binding below is the one allowlist: the SigV4 addon
# re-signs only what the sidecar matched to it.
AWS_HOSTS = [
    "sts.amazonaws.com",
    "sts.us-east-1.amazonaws.com",
    "s3.us-east-1.amazonaws.com",
    "*.s3.us-east-1.amazonaws.com",
    "s3.amazonaws.com",
    "*.s3.amazonaws.com",
    "dynamodb.us-east-1.amazonaws.com",
    "bedrock-runtime.us-east-1.amazonaws.com",
]
BEDROCK_HOST = "bedrock-runtime.us-east-1.amazonaws.com"
VAULT_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE"]

# vault credential name -> (field of the STS credentials, carrier header the sidecar adds)
CARRIERS = {
    "aws-akid": ("AccessKeyId", "x-osb-aws-access-key-id"),
    "aws-secret": ("SecretAccessKey", "x-osb-aws-secret-access-key"),
    "aws-token": ("SessionToken", "x-osb-aws-session-token"),
}

# What the sandbox gets instead of keys. The SDKs sign with anything; the addon replaces the
# signature. The placeholder must not look like a real key to a secret scanner
# (AKIA[0-9A-Z]{16}), hence the underscores.
PLACEHOLDER_ACCESS_KEY_ID = "AKIA_OSB_PLACEHOLDER"
PLACEHOLDER_SECRET = "osb-placeholder-secret"
BEARER_PLACEHOLDER = "bedrock-api-key-osb-placeholder"

# Every sandbox the broker creates carries this metadata, so it can find them again after a
# restart and so cleanup scripts can delete only part 5's sandboxes.
OWN_METADATA = {"example": "opensandbox-aws-credentials", "owner": "osb-broker"}

# The sandbox's permissions live in two files that terraform/main.tf reads too, so the role's
# policy and every session policy are built from the same text: the Allow statements (a
# template with ${bucket}, ${table}, ...) and the actions of the explicit Deny.
PERMISSIONS_TEMPLATE = string.Template(Path(__file__).with_name("sandbox_permissions.json").read_text())
VENDING_ACTIONS: list[str] = json.loads(Path(__file__).with_name("vending_actions.json").read_text())

# Lifecycle states after which a sandbox never runs again (the server's schema lists Pending,
# Running, Pausing, Paused, Resuming, Stopping, Terminated and Failed).
ENDED_STATES = {"stopping", "terminated", "failed"}


@dataclass(frozen=True)
class Config:
    """What the broker needs to know, from the environment (manifests/broker.yaml sets it from
    broker.env, which 01_iam.sh writes from the Terraform outputs)."""

    sandbox_role_arn: str
    region: str
    test_bucket: str
    ddb_table: str
    bedrock_model_id: str
    server_domain: str
    api_key: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Any = os.environ) -> Config:
        return cls(
            sandbox_role_arn=env.get("SANDBOX_ROLE_ARN", ""),
            region=env.get("AWS_REGION", "us-east-1"),
            test_bucket=env.get("AWS_TEST_BUCKET", ""),
            ddb_table=env.get("DDB_TABLE", ""),
            bedrock_model_id=env.get("BEDROCK_MODEL_ID", ""),
            server_domain=env.get("OSB_SERVER_DOMAIN", "opensandbox-server.opensandbox-system.svc:80"),
            api_key=env.get("OSB_API_KEY", ""),
        )

    @property
    def account_id(self) -> str:
        parts = self.sandbox_role_arn.split(":")
        return parts[4] if len(parts) > 4 else ""

    def missing(self) -> list[str]:
        names = {"SANDBOX_ROLE_ARN": self.sandbox_role_arn, "AWS_TEST_BUCKET": self.test_bucket,
                 "DDB_TABLE": self.ddb_table, "BEDROCK_MODEL_ID": self.bedrock_model_id, "OSB_API_KEY": self.api_key}
        return [name for name, value in names.items() if not value]


CONFIG = Config.from_env()

# --- AWS: the role session for one sandbox --------------------------------------------------

_clients: dict[str, Any] = {}
_clients_lock = threading.Lock()


def aws_client(service: str, credentials: dict | None = None) -> Any:
    """A boto3 client: with the broker's own credentials (EKS Pod Identity), or with the given
    temporary credentials. A boto3 session is not thread-safe, so each client gets its own."""
    if credentials is not None:
        return boto3.session.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=CONFIG.region,
        ).client(service)
    with _clients_lock:
        if service not in _clients:
            _clients[service] = boto3.session.Session(region_name=CONFIG.region).client(service)
        return _clients[service]


def permission_values() -> dict[str, str]:
    """What fills in sandbox_permissions.json (terraform/main.tf passes the same names)."""
    cfg = CONFIG
    return {
        "region": cfg.region,
        "account": cfg.account_id,
        "bucket": cfg.test_bucket,
        "table": cfg.ddb_table,
        "model": cfg.bedrock_model_id,
        "foundation_model": cfg.bedrock_model_id.removeprefix("us."),
    }


def sandbox_permissions() -> list[dict]:
    """The Allow statements of the role, filled in. A missing value raises KeyError."""
    return json.loads(PERMISSIONS_TEMPLATE.substitute(permission_values()))


def session_policy() -> str:
    """The inline session policy for a sandbox's AssumeRole, as minified JSON.

    The session can do only what both this and the role's policy allow. It carries the role's
    own statements (the same files), so the Deny on credential-vending actions is there twice:
    if the role ever gets a broad Allow, sandbox sessions still can't mint credentials.
    """
    statements = [*sandbox_permissions(),
                  {"Sid": "NoCredentialVending", "Effect": "Deny", "Action": VENDING_ACTIONS, "Resource": "*"}]
    text = json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":"))
    if len(text) > POLICY_MAX_CHARS:
        raise ValueError(f"session policy is {len(text)} characters; STS allows {POLICY_MAX_CHARS}")
    return text


def assume_for_sandbox(sandbox_id: str) -> dict:
    """Temporary credentials for one sandbox: osb-sandbox, session name = the sandbox ID (so
    CloudTrail shows which sandbox made each call), one hour, the session policy above."""
    reply = aws_client("sts").assume_role(
        RoleArn=CONFIG.sandbox_role_arn,
        RoleSessionName=sandbox_id[:64],
        Policy=session_policy(),
        DurationSeconds=SESSION_SECONDS,
    )
    creds = reply["Credentials"]
    return {name: creds[name] for name in ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")}


def mint_bedrock_key(creds: dict, expires: int = SESSION_SECONDS) -> str:
    """A short-term Bedrock API key made from the sandbox's session, in the format of AWS's
    aws-bedrock-token-generator: "bedrock-api-key-" + base64 of a SigV4 presigned URL for
    CallWithBearerToken. It carries the session's permissions and lasts no longer than the
    session. Signing happens here; nothing is sent to AWS."""
    request = AWSRequest(
        method="POST",
        url="https://bedrock.amazonaws.com/",
        headers={"host": "bedrock.amazonaws.com"},
        params={"Action": "CallWithBearerToken"},
    )
    signer = Credentials(creds["AccessKeyId"], creds["SecretAccessKey"], creds["SessionToken"])
    SigV4QueryAuth(signer, "bedrock", CONFIG.region, expires=expires).add_auth(request)
    presigned = request.url.removeprefix("https://") + "&Version=1"
    return "bedrock-api-key-" + base64.b64encode(presigned.encode()).decode()


# --- The Credential Vault entries -----------------------------------------------------------


def vault_credentials(creds: dict, bedrock_key: str | None = None) -> list[dict]:
    """The vault's credentials: the three STS values, plus the Bedrock key when there is one."""
    items = [{"name": name, "source": {"type": "inline", "value": creds[key]}} for name, (key, _) in CARRIERS.items()]
    if bedrock_key is not None:
        items.append({"name": "bedrock-key", "source": {"type": "inline", "value": bedrock_key}})
    return items


def vault_bindings(bedrock_bearer: bool = False) -> list[dict]:
    """The vault's bindings: which requests get which credentials.

    aws-sigv4 adds the three carrier headers to every AWS request. With bedrock_bearer, the
    Bedrock host gets the Bedrock key as `Authorization: Bearer` instead, and only that: two
    bindings on one host at the same precedence are refused as ambiguous.
    """
    sigv4_hosts = [host for host in AWS_HOSTS if not (bedrock_bearer and host == BEDROCK_HOST)]
    bindings = [{
        "name": "aws-sigv4",
        "match": {"schemes": ["https"], "hosts": sigv4_hosts, "methods": list(VAULT_METHODS), "paths": ["/*"]},
        "auth": {
            "type": "customHeaders",
            "headers": [{"name": header, "credential": name} for name, (_, header) in CARRIERS.items()],
        },
    }]
    if bedrock_bearer:
        bindings.append({
            "name": "bedrock-bearer",
            "match": {"schemes": ["https"], "hosts": [BEDROCK_HOST], "methods": list(VAULT_METHODS), "paths": ["/*"]},
            "auth": {"type": "bearer", "credential": "bedrock-key"},
        })
    return bindings


def write_vault(sb: Any, creds: dict, bedrock_key: str | None = None) -> int:
    """Create the sandbox's vault. Returns its revision (1 for a new vault)."""
    state = sb.credential_vault.create(
        credentials=vault_credentials(creds, bedrock_key),
        bindings=vault_bindings(bedrock_bearer=bedrock_key is not None),
    )
    return state.revision


def refresh_vault(sb: Any, creds: dict, expected_revision: int, bedrock_key: str | None = None) -> int:
    """Replace the credential values in one atomic patch; the bindings stay as they are. The
    sidecar refuses the patch if the vault is no longer at expected_revision. Returns the new
    revision."""
    state = sb.credential_vault.patch(
        expected_revision=expected_revision,
        credentials={"replace": vault_credentials(creds, bedrock_key)},
    )
    return state.revision


# --- The sandboxes the broker manages -------------------------------------------------------


class UnknownSandbox(KeyError):
    """The broker does not manage this sandbox."""


class Revoked(Exception):
    """The sandbox's vault was revoked; nothing more is written to it."""


class VaultConflict(Exception):
    """The vault refused a patch: it is not at the expected revision."""

    def __init__(self, status: int, expected_revision: int, revision: int | None):
        super().__init__(f"vault answered {status}: expected revision {expected_revision}, it is at {revision}")
        self.status, self.expected_revision, self.revision = status, expected_revision, revision


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Session:
    """One managed sandbox: its SDK handle and what the broker last wrote to its vault."""

    sb: Any
    bedrock_bearer: bool
    revision: int = 0
    akid_prefix: str = ""
    expires_at: datetime | None = None
    rotated_at: datetime | None = None
    vault: str = "active"  # or "revoked"
    last_error: str | None = None
    next_rotation: float = 0.0  # time.monotonic()
    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)

    def record(self, creds: dict, revision: int) -> None:
        self.revision = revision
        self.akid_prefix = creds["AccessKeyId"][:4] + "…"
        self.expires_at = creds["Expiration"]
        self.rotated_at = _now()
        self.last_error = None
        self.next_rotation = time.monotonic() + REFRESH_SECONDS

    def failed(self, error: Exception) -> None:
        self.last_error = _mask(f"{type(error).__name__}: {error}")[:300]
        self.next_rotation = time.monotonic() + RETRY_SECONDS

    def view(self) -> dict:
        due = None if self.vault != "active" else max(0, round(self.next_rotation - time.monotonic()))
        return {
            "sandbox_id": self.sb.id,
            "vault": self.vault,
            "revision": self.revision,
            "akid_prefix": self.akid_prefix,
            "expires_at": _iso(self.expires_at),
            "rotated_at": _iso(self.rotated_at),
            "next_rotation_in_seconds": due,
            "bedrock_bearer": self.bedrock_bearer,
            "last_error": self.last_error,
        }


def sandbox_env(bedrock_bearer: bool = False) -> dict[str, str]:
    """The sandbox's environment: placeholders where the keys would go, and the CA bundle the
    sidecar's MITM certificate is in (the AWS CLI and boto3 ignore SSL_CERT_FILE). No
    AWS_ACCOUNT_ID: with it, DynamoDB switches to account-based endpoints outside AWS_HOSTS."""
    env = {
        "AWS_ACCESS_KEY_ID": PLACEHOLDER_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": PLACEHOLDER_SECRET,
        "AWS_REGION": CONFIG.region,
        "AWS_DEFAULT_REGION": CONFIG.region,
        "AWS_CA_BUNDLE": "/opt/opensandbox/merged-ca-certificates.pem",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    if bedrock_bearer:
        env["AWS_BEARER_TOKEN_BEDROCK"] = BEARER_PLACEHOLDER
    return env


def connection() -> ConnectionConfigSync:
    """The in-cluster server Service, which also relays the calls to each sandbox's execd and
    egress sidecar (use_server_proxy=True), vault writes included. Without the proxy, a server
    with the ingress gateway configured (part 2) announces the gateway's NLB, which only the
    author's IP may reach, so a pod can't.
    A new config per sandbox: closing a sandbox closes the transport its config owns."""
    return ConnectionConfigSync(
        domain=CONFIG.server_domain,
        api_key=CONFIG.api_key,
        use_server_proxy=True,
        request_timeout=timedelta(seconds=60),
    )


class Broker:
    """The sandboxes this broker manages, and their rotation threads. State lives in memory;
    after a restart, adopt() picks the broker's sandboxes up again."""

    def __init__(self, create_sandbox: Callable[..., Any] | None = None,
                 connect_sandbox: Callable[..., Any] | None = None, start_loop: bool = True):
        self._create_sandbox = create_sandbox or SandboxSync.create
        self._connect_sandbox = connect_sandbox or SandboxSync.connect
        self._start_loop = start_loop
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    # -- create, read, delete

    def create(self, image: str, bedrock_bearer: bool = False, timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
               metadata: dict[str, str] | None = None) -> dict:
        sb = self._create_sandbox(
            image=image,
            env=sandbox_env(bedrock_bearer),
            metadata={**(metadata or {}), **OWN_METADATA, "bedrock_bearer": str(bedrock_bearer).lower()},
            network_policy=NetworkPolicy(
                default_action="deny",
                egress=[NetworkRule(action="allow", target=host) for host in AWS_HOSTS],
            ),
            credential_proxy=CredentialProxyConfig(enabled=True),
            timeout=timedelta(minutes=timeout_minutes),
            ready_timeout=timedelta(seconds=READY_TIMEOUT_SECONDS),
            connection_config=connection(),
        )
        session = Session(sb=sb, bedrock_bearer=bedrock_bearer)
        try:
            creds, key = self._new_credentials(sb.id, bedrock_bearer)
            session.record(creds, write_vault(sb, creds, key))
            endpoints = {"execd": sb.get_endpoint(DEFAULT_EXECD_PORT).endpoint,
                         "egress": sb.get_endpoint(DEFAULT_EGRESS_PORT).endpoint}
        except BaseException:
            # A sandbox the broker can't give credentials to is of no use: don't leave it running.
            _kill_quietly(sb)
            raise
        self._add(session)
        log.info("sandbox %s: vault revision %d, key %s, expires %s", sb.id, session.revision,
                 session.akid_prefix, _iso(session.expires_at))
        return {**session.view(), "endpoints": endpoints}

    def get(self, sandbox_id: str) -> dict:
        session = self._session(sandbox_id)
        try:
            state = session.sb.get_info().status.state
        except Exception as error:  # the view is still useful without it
            state = f"unknown ({type(error).__name__})"
        return {**session.view(), "state": state}

    def list(self) -> list[dict]:
        with self._lock:
            sessions = list(self._sessions.values())
        return [session.view() for session in sessions]

    def delete(self, sandbox_id: str) -> None:
        session = self._session(sandbox_id)
        try:
            session.sb.kill()
        except SandboxApiException as error:
            if error.status_code != 404:  # already gone is fine; otherwise keep it, and rotating
                raise
        session.stop.set()
        with self._lock:
            self._sessions.pop(sandbox_id, None)
        session.sb.close()
        log.info("sandbox %s: deleted", sandbox_id)

    # -- the vault

    def refresh(self, sandbox_id: str, expected_revision: int | None = None) -> dict:
        """Rotate now (POST /sandboxes/{id}/refresh). With expected_revision, patch with that
        revision instead of the one the broker knows, to show what a stale writer gets."""
        session = self._session(sandbox_id)
        with session.lock:
            if session.vault == "revoked":
                raise Revoked(sandbox_id)
            self._rotate_locked(session, expected_revision, retry_conflict=False)
        log.info("sandbox %s: rotated on request, revision %d, key %s", sandbox_id, session.revision,
                 session.akid_prefix)
        return session.view()

    def rotate(self, sandbox_id: str) -> None:
        """The refresh loop's rotation: like refresh(), but a revision conflict is retried."""
        session = self._session(sandbox_id)
        with session.lock:
            if session.vault == "revoked":
                return
            self._rotate_locked(session, None, retry_conflict=True)

    def _rotate_locked(self, session: Session, expected_revision: int | None, retry_conflict: bool) -> None:
        """New credentials for the sandbox, into its vault. The caller holds session.lock."""
        expected = session.revision if expected_revision is None else expected_revision
        creds, key = self._new_credentials(session.sb.id, session.bedrock_bearer)
        try:
            revision = refresh_vault(session.sb, creds, expected, key)
        except SandboxApiException as error:
            if error.status_code == 404:
                # The sidecar keeps the vault in memory only. After a sidecar restart (or a pause
                # and resume) it is gone, and the trusted side has to write it again.
                revision = write_vault(session.sb, creds, key)
            elif error.status_code not in (409, 412):
                raise
            elif not retry_conflict:
                raise VaultConflict(error.status_code, expected, _vault_revision(session.sb)) from None
            elif (current := _vault_revision(session.sb)) is not None:
                # Another writer moved the revision: read it and patch once more.
                revision = refresh_vault(session.sb, creds, current, key)
            else:
                raise
        session.record(creds, revision)

    def loop_step(self, sandbox_id: str) -> bool:
        """One due rotation, as the refresh loop runs it. Returns False when the loop should end,
        after dropping the sandbox: it is gone (the server doesn't have it, or it ended), or
        rotating keeps failing after the last credentials it got have expired. Nothing more is
        then assumed or written for it."""
        session = self._session(sandbox_id)
        if session.vault == "revoked":
            return False
        ended = _ended(session.sb)
        if ended:
            self._drop(session, ended)
            return False
        try:
            self.rotate(sandbox_id)
        except UnknownSandbox:
            return False
        except Exception as error:
            session.failed(error)
            if session.expires_at is not None and _now() >= session.expires_at:
                self._drop(session, f"rotation still fails after its credentials expired at "
                                    f"{_iso(session.expires_at)} ({session.last_error})", logging.WARNING)
                return False
            log.warning("sandbox %s: rotation failed, retrying in %d s: %s", sandbox_id, RETRY_SECONDS,
                        session.last_error)
            return True
        log.info("sandbox %s: rotated, revision %d, key %s, expires %s", sandbox_id, session.revision,
                 session.akid_prefix, _iso(session.expires_at))
        return True

    def revoke(self, sandbox_id: str) -> None:
        """Delete the vault (DELETE /sandboxes/{id}/vault): from the next request on, the sidecar
        has nothing to add, and the sandbox's AWS calls fail. The STS session itself stays valid
        until it expires, but only the broker and the sidecar ever held it."""
        session = self._session(sandbox_id)
        with session.lock:
            try:
                session.sb.credential_vault.delete()
            except SandboxApiException as error:
                if error.status_code != 404:  # still there: keep rotating it
                    raise
            session.vault = "revoked"
            session.stop.set()
        log.info("sandbox %s: vault revoked", sandbox_id)

    # -- after a restart

    def adopt(self) -> int:
        """Take over the running sandboxes a previous broker pod created (found by OWN_METADATA):
        their rotation threads rotate the credentials at once, then every REFRESH_SECONDS.
        Returns how many."""
        found = []
        with SandboxManagerSync.create(connection_config=connection()) as manager:
            page = 1
            while True:
                result = manager.list_sandbox_infos(SandboxFilter(metadata=OWN_METADATA, page=page, page_size=100))
                found += [info for info in result.sandbox_infos if str(info.status.state).lower() == "running"]
                if not result.pagination.has_next_page:
                    break
                page += 1
        adopted = 0
        for info in found:
            try:
                sb = self._connect_sandbox(info.id, connection_config=connection(), skip_health_check=True)
                session = Session(sb=sb, bedrock_bearer=(info.metadata or {}).get("bedrock_bearer") == "true")
                # No vault (the sidecar lost it): revision 0, and rotate() writes a new one.
                session.revision = _vault_revision(sb) or 0
                session.next_rotation = time.monotonic()  # due now
                self._add(session)
                adopted += 1
                log.info("sandbox %s: adopted at vault revision %d", info.id, session.revision)
            except Exception as error:
                log.warning("sandbox %s: could not adopt it: %s", info.id, _mask(str(error)))
        return adopted

    # -- internals

    def _new_credentials(self, sandbox_id: str, bedrock_bearer: bool) -> tuple[dict, str | None]:
        creds = assume_for_sandbox(sandbox_id)
        return creds, (mint_bedrock_key(creds) if bedrock_bearer else None)

    def _session(self, sandbox_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(sandbox_id)
        if session is None:
            raise UnknownSandbox(sandbox_id)
        return session

    def _add(self, session: Session, start: bool = True) -> None:
        with self._lock:
            self._sessions[session.sb.id] = session
        if start:
            self._start(session)

    def _start(self, session: Session) -> None:
        if self._start_loop:
            threading.Thread(target=self._refresh_loop, args=(session,), daemon=True,
                             name=f"rotate-{session.sb.id[:8]}").start()

    def _drop(self, session: Session, why: str, level: int = logging.INFO) -> None:
        session.stop.set()
        with self._lock:
            if self._sessions.get(session.sb.id) is session:
                del self._sessions[session.sb.id]
        session.sb.close()
        log.log(level, "sandbox %s: %s; stopped rotating and dropped it", session.sb.id, _mask(why))

    def _refresh_loop(self, session: Session) -> None:
        while not session.stop.wait(max(0.0, session.next_rotation - time.monotonic())):
            if time.monotonic() < session.next_rotation:
                continue  # a forced refresh moved the deadline while this thread waited
            try:
                if not self.loop_step(session.sb.id):
                    return
            except UnknownSandbox:  # deleted through the API meanwhile
                return


def _ended(sb: Any) -> str | None:
    """Why the sandbox will never run again, or None if it may (or the server can't say now)."""
    try:
        state = str(sb.get_info().status.state)
    except SandboxApiException as error:
        return "the server no longer has it (404)" if error.status_code == 404 else None
    except Exception:
        return None  # the server is out of reach for now: keep trying
    return f"it is {state}" if state.lower() in ENDED_STATES else None


def _mask(text: str) -> str:
    """Text that leaves the broker (API errors, logs, check output), without the account ID."""
    account = CONFIG.account_id
    return text.replace(account, "<account>") if account else text


def _vault_revision(sb: Any) -> int | None:
    try:
        return sb.credential_vault.get().revision
    except SandboxApiException:
        return None


def _kill_quietly(sb: Any) -> None:
    try:
        sb.kill()
    except Exception as error:
        log.warning("sandbox %s: could not delete it: %s", sb.id, _mask(str(error)))
    finally:
        sb.close()


# --- HTTP API ----------------------------------------------------------------------------------


class BadRequest(Exception):
    pass


MAX_BODY_BYTES = 64 * 1024


def _route(broker: Broker, method: str, parts: list[str], body: Callable[[], dict]) -> tuple[int, Any]:
    match (method, parts):
        case ("GET", ["healthz"]):
            return 200, {"status": "ok", "sandboxes": len(broker.list())}
        case ("GET", ["sandboxes"]):
            return 200, broker.list()
        case ("POST", ["sandboxes"]):
            request = body()
            image = request.get("image")
            if not isinstance(image, str) or not image.strip():
                raise BadRequest("image is required: the workload image for the sandbox")
            bedrock_bearer = request.get("bedrock_bearer", False)
            timeout_minutes = request.get("timeout_minutes", DEFAULT_TIMEOUT_MINUTES)
            metadata = request.get("metadata") or {}
            if not isinstance(bedrock_bearer, bool):
                raise BadRequest("bedrock_bearer must be true or false")
            if type(timeout_minutes) is not int or not 1 <= timeout_minutes <= 24 * 60:
                raise BadRequest("timeout_minutes must be a whole number of minutes, 1 to 1440")
            if not isinstance(metadata, dict) or not all(isinstance(v, str) for v in metadata.values()):
                raise BadRequest("metadata must map strings to strings")
            return 201, broker.create(image.strip(), bedrock_bearer, timeout_minutes, metadata)
        case ("GET", ["sandboxes", sandbox_id]):
            return 200, broker.get(sandbox_id)
        case ("POST", ["sandboxes", sandbox_id, "refresh"]):
            expected = body().get("expected_revision")
            if expected is not None and type(expected) is not int:
                raise BadRequest("expected_revision must be an integer")
            return 200, broker.refresh(sandbox_id, expected)
        case ("DELETE", ["sandboxes", sandbox_id, "vault"]):
            broker.revoke(sandbox_id)
            return 200, broker.get(sandbox_id)
        case ("DELETE", ["sandboxes", sandbox_id]):
            broker.delete(sandbox_id)
            return 200, {"deleted": sandbox_id}
    return 404, {"error": f"no such route: {method} /{'/'.join(parts)}"}


def make_handler(broker: Broker) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "osb-broker"
        timeout = 30  # seconds a read may wait: a client that stops sending can't hold a thread

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

        def _body(self) -> dict:
            declared = self.headers.get("Content-Length")
            if declared is None:
                length = 0
            elif re.fullmatch(r"[0-9]+", declared.strip()):
                length = int(declared)
            else:
                raise BadRequest("Content-Length must be a whole number of bytes")
            if length > MAX_BODY_BYTES:
                raise BadRequest(f"request body over {MAX_BODY_BYTES} bytes")
            raw = self.rfile.read(length) if length else b""
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except ValueError:
                raise BadRequest("request body is not JSON") from None
            if not isinstance(parsed, dict):
                raise BadRequest("request body must be a JSON object")
            return parsed

        def _handle(self, method: str) -> None:
            parts = [part for part in urlsplit(self.path).path.split("/") if part]
            try:
                status, payload = _route(broker, method, parts, self._body)
            except BadRequest as error:
                status, payload = 400, {"error": str(error)}
            except UnknownSandbox as error:
                status, payload = 404, {"error": f"this broker does not manage sandbox {error.args[0]}"}
            except VaultConflict as error:
                # The vault's own status (409 or 412), so a caller sees what the sidecar said.
                status = error.status
                payload = {"error": "vault revision conflict", "vault_status": error.status,
                           "expected_revision": error.expected_revision, "revision": error.revision}
            except Revoked as error:
                status, payload = 409, {"error": f"the vault of sandbox {error.args[0]} was revoked"}
            except Exception as error:  # AWS or the OpenSandbox server said no
                message = _mask(f"{type(error).__name__}: {error}")
                log.warning("%s %s failed: %s", method, self.path, message)
                status, payload = 502, {"error": message[:500]}
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            if self.path != "/healthz":  # the readiness probe, every 10 s
                log.info("%s %s", self.address_string(), format % args)

    return Handler


# --- Checks to run in the broker pod (kubectl exec) ---------------------------------------------

# Calls a sandbox session must not be able to make. Each action is in the role's and the
# session policy's Deny, and none is allowed anyway, so a refusal alone proves little: AWS must
# say it was an explicit deny (a message about "no ... policy allows" would mean the Deny is
# missing).
#
# For a call on a role (AssumeRole), AWS's message names no reason, only the refused action
# and an authorization id, and that call would be refused even with no Deny at all (nothing
# allows it, and osb-sandbox trusts only the broker). So deny-check asks AWS for the
# authorization details (iam-toolbox GetRequestAuthorizationDetails, which the broker role may
# call) and requires NoCredentialVending to have matched in both the role's policy and the
# session policy. Without the details, the line says the call was refused and nothing more.
# (A sandbox session carries Pod Identity's transitive session tags, so its AssumeRole is
# refused on sts:TagSession first.)
DENY_PROBES: list[tuple[str, str, Callable[[Any], Any]]] = [
    ("sts:AssumeRole", "sts",
     lambda client: client.assume_role(RoleArn=CONFIG.sandbox_role_arn, RoleSessionName="osb-deny-check-chained")),
    ("ecr:GetAuthorizationToken", "ecr", lambda client: client.get_authorization_token()),
    ("secretsmanager:GetSecretValue", "secretsmanager",
     lambda client: client.get_secret_value(SecretId="osb-deny-check")),
    ("iam:ListRoles", "iam", lambda client: client.list_roles(MaxItems=1)),
]
AUTHZ_WAIT_SECONDS = 150  # AWS publishes the details about a minute after the denial
AUTHZ_POLL_SECONDS = 10
DENY_SID = "NoCredentialVending"
BOTH_LAYERS = {"IDENTITY_BASED_POLICY": "the role policy", "SESSION_POLICY": "the session policy"}


def _authorization_verdict(auth_id: str) -> tuple[str, str]:
    """("ok" | "FAIL" | "unknown", text) from AWS's authorization details for a denial."""
    deadline = time.monotonic() + AUTHZ_WAIT_SECONDS
    while True:
        try:
            details = aws_client("iam-toolbox").get_request_authorization_details(authorizationId=auth_id)
            break
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code != "ResourceNotFoundException":
                return "unknown", f"the authorization details answered {code}"
            if time.monotonic() >= deadline:
                return "unknown", f"no authorization details after {AUTHZ_WAIT_SECONDS} s"
            time.sleep(AUTHZ_POLL_SECONDS)
        except Exception as error:  # no iam-toolbox in this botocore, no network, ...
            return "unknown", f"the authorization details can't be read here ({type(error).__name__})"
    if not isinstance(details, dict):
        return "unknown", "the authorization details came back in an unexpected shape"
    types = {policy.get("uri"): policy.get("type") for policy in details.get("policies") or []}
    effects, matched_in = set(), set()
    for evaluation in details.get("evaluations") or []:
        effects.add(evaluation.get("evaluatedEffect"))
        for policy in evaluation.get("matchedPolicies") or []:
            if any(st.get("sid") == DENY_SID and st.get("evaluatedEffect") == "DENY"
                   for st in policy.get("matchedStatements") or []):
                matched_in.add(types.get(policy.get("uri")))
    missing = [name for kind, name in BOTH_LAYERS.items() if kind not in matched_in]
    if missing:
        evaluated = ", ".join(sorted(map(str, effects))) or "nothing"
        return "FAIL", f"{DENY_SID} did not match in {' or '.join(missing)} (AWS evaluated {evaluated})"
    return "ok", "explicit deny in the role policy and the session policy (AWS's authorization details)"


def deny_check() -> int:
    """Assume osb-sandbox exactly as for a sandbox (session policy included), call each
    DENY_PROBES action with that session and show what refused it. Returns 1 if a call was
    allowed or AWS shows it was not the Deny that refused it, 0 otherwise. Prints no credential:
    an allowed call's response is dropped unread."""
    creds = assume_for_sandbox("osb-deny-check")
    print(f"session osb-deny-check of {CONFIG.sandbox_role_arn.rsplit('/', 1)[-1]}, with the session policy "
          f"(key {creds['AccessKeyId'][:4]}…, expires {_iso(creds['Expiration'])})")
    failures = unproven = 0
    for action, service, call in DENY_PROBES:
        try:
            call(aws_client(service, credentials=creds))
            verdict = "FAIL  ALLOWED (the response was dropped unread)"
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            message = _mask(error.response.get("Error", {}).get("Message", ""))
            where = re.search(r"explicit deny in an? ([\w -]*?policy)", message)
            refused = re.search(r"not authorized to perform: ([\w-]+:[\w*]+)", message)
            auth_id = re.search(r"authorization id: (\w+)", message)
            on = f" on {refused.group(1)}" if refused else ""
            if code not in ("AccessDenied", "AccessDeniedException"):
                verdict = f"FAIL  {code}: authorization passed ({message[:120]})"
            elif where:
                verdict = f"ok    AccessDenied: explicit deny in {where.group(1)}"
            elif "explicit deny" in message:
                verdict = f"ok    AccessDenied{on}: explicit deny"
            elif re.search(r"no [\w -]*policy allows", message):
                verdict = f"FAIL  AccessDenied, but not an explicit deny: {message[-160:]}"
            elif auth_id:
                print(f"  {'':<31} {action}: AccessDenied{on} with no reason; asking AWS why "
                      f"(authorization id {auth_id.group(1)}, about a minute)", flush=True)
                status, text = _authorization_verdict(auth_id.group(1))
                if status == "unknown":
                    verdict = (f"--    refused (an explicit deny can't be told apart from no-allow by the call "
                               f"alone): {text}")
                else:
                    verdict = f"{status:<6}AccessDenied{on}: {text}"
            else:
                verdict = (f"--    refused (an explicit deny can't be told apart from no-allow by the call alone): "
                           f"{message[-120:]}")
        failures += verdict.startswith("FAIL")
        unproven += verdict.startswith("--")
        print(f"  {action:<31} {verdict}")
    total = len(DENY_PROBES)
    if failures:
        print(f"{failures} of {total} not refused by the explicit Deny")
    elif unproven:
        print(f"all {total} refused; {total - unproven} shown to be by the explicit Deny, "
              f"{unproven} not provable from the call alone")
    else:
        print(f"all {total} refused, each shown to be by the explicit Deny")
    return 1 if failures else 0


def whoami() -> int:
    """The broker's own AWS identity: an EKS Pod Identity role session, so every AssumeRole it
    makes is role chaining. Prints no credential."""
    sts = aws_client("sts")
    print("broker identity:", _mask(sts.get_caller_identity()["Arn"]))
    for name in ("AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE"):
        print(f"  {name}: {'set' if os.environ.get(name) else 'NOT SET'}")
    provider = boto3.session.Session().get_credentials()
    print("  credentials from:", provider.method if provider else "none found")
    try:
        sts.assume_role(RoleArn=CONFIG.sandbox_role_arn, RoleSessionName="osb-whoami",
                        DurationSeconds=2 * SESSION_SECONDS)
        print("  a two-hour AssumeRole of osb-sandbox was accepted (the credentials were dropped)")
    except ClientError as error:
        message = str(error.response.get("Error", {}).get("Message", error))
        print("  a two-hour AssumeRole of osb-sandbox:", _mask(message))
    return 0


def serve() -> None:
    missing = CONFIG.missing()
    if missing:
        sys.exit(f"not set: {', '.join(missing)} (manifests/broker.yaml sets them from broker.env and a Secret)")
    broker = Broker()
    try:
        adopted = broker.adopt()
    except Exception as error:
        log.warning("could not list existing sandboxes, adopting none: %s", _mask(str(error)))
        adopted = 0
    host, port = LISTEN.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), make_handler(broker))
    log.info("listening on %s; sessions of %d s rotated every %d s; adopted %d sandboxes",
             LISTEN, SESSION_SECONDS, REFRESH_SECONDS, adopted)
    server.serve_forever()


class MaskingFormatter(logging.Formatter):
    """Every log line, the SDKs' included, with the account ID masked (the OpenSandbox SDK logs
    the image reference, which names the ECR registry)."""

    def format(self, record: logging.LogRecord) -> str:
        return _mask(super().format(record))


def main(argv: list[str]) -> int:
    handler = logging.StreamHandler()
    handler.setFormatter(MaskingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    command = argv[1] if len(argv) > 1 else "serve"
    if command == "serve":
        serve()
        return 0
    if command == "deny-check":
        return deny_check()
    if command == "whoami":
        return whoami()
    print("usage: broker.py [serve | deny-check | whoami]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
