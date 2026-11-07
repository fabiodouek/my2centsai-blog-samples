"""Credential lifetime: a sandbox works across a rotation, a stale revision conflicts, deleting the vault revokes.

  uv run python 07_lifetime.py [--wait-loop]

The broker's own credentials are an EKS Pod Identity role session, so its sts:AssumeRole for a sandbox is
role chaining, and AWS caps a chained session at one hour. The role osb-sandbox itself allows two hours
(max_session_duration = 7200), so the cap that applies is the chaining one, and AWS names it. A sandbox therefore gets one-hour credentials, and
the broker rotates them every 30 minutes (half the session) by patching the sandbox's vault. This script:

  1. calls AWS from the sandbox, then forces a rotation (POST /sandboxes/{id}/refresh) and calls AWS again:
     the sandbox did nothing, the vault revision went up, the expiry moved out, the call still works;
  2. sends a refresh carrying a stale expected_revision: the vault answers 409 or 412 (412 can also mean
     "vault not Ready", so the script checks the body) and the vault's own revision is unchanged;
  3. deletes the vault (DELETE /sandboxes/{id}/vault): the next AWS call from the same sandbox fails (the
     placeholder signature reaches STS with nothing added: InvalidClientTokenId), and the broker has stopped
     rotating it (a later refresh is refused). The sandbox keeps running; it just has no credentials.
     This is a revocation of the sandbox's access. The temporary credentials themselves stay valid at AWS
     until they expire, so revoking them everywhere needs an IAM Deny on aws:TokenIssueTime, not this.

--wait-loop also waits (up to 35 minutes) for the broker's own timer to rotate the credentials, without a
forced refresh. Without it the script narrates that and moves on.

Needs ./01_iam.sh, ./02_broker.sh and ./00_image.sh to have run.
"""

import argparse
import subprocess
import time
from datetime import datetime, timezone

from common import (
    Checks, BrokerError, broker_get, broker_refresh, broker_revoke, cleanup, ensure_broker_forward,
    ensure_port_forward, env_sh, error_code, heading, log_mark, log_since, new_sandbox, note, run, scrub, sh, short_time,
)

SCRIPT = "07_lifetime"
CALL = "aws sts get-caller-identity --query Arn --output text"

parser = argparse.ArgumentParser()
parser.add_argument("--wait-loop", action="store_true", help="wait for the broker's own 30-minute rotation")
args = parser.parse_args()

ensure_port_forward()
ensure_broker_forward()
checks = Checks()


def seconds_left(iso: str) -> int:
    return int((datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)).total_seconds())


try:
    heading("1. a sandbox, and the clock on its credentials")
    sb, v1 = new_sandbox(script=SCRIPT)
    sid = v1["sandbox_id"]
    print(f"  revision {v1['revision']}; expires in {seconds_left(v1['expires_at'])} s (the role-chaining cap: one hour)")
    print(f"  the broker's timer rotates in {v1['next_rotation_in_seconds']} s (half the session: 1,800 s)")
    checks.check("session length is the one-hour cap", 3500 <= seconds_left(v1["expires_at"]) <= 3600, f"{seconds_left(v1['expires_at'])} s")
    checks.check("rotation is due at half the session", 1700 <= v1["next_rotation_in_seconds"] <= 1800)
    checks.check("AWS call before the rotation", run(sb, CALL) == 0)

    heading("2. force a rotation: the same sandbox keeps working")
    v2 = broker_refresh(sid)
    print(f"  refresh -> revision {v1['revision']} -> {v2['revision']}; "
          f"new expiry {short_time(v2['expires_at'])}")
    checks.check("the vault revision went up", v2["revision"] > v1["revision"])
    checks.check("the credentials are new (a later expiry than before)", v2["expires_at"] > v1["expires_at"], f"{short_time(v1['expires_at'])} -> {short_time(v2['expires_at'])}")
    checks.check("expiry moved out by a new full session", 3500 <= seconds_left(v2["expires_at"]) <= 3600, f"{seconds_left(v2['expires_at'])} s left")
    checks.check("the sandbox's AWS call after the rotation", run(sb, CALL) == 0)

    heading("3. a patch with the right revision works, one with a stale revision conflicts")
    v3 = broker_refresh(sid, expected_revision=v2["revision"])
    print(f"  refresh with expected_revision={v2['revision']} -> revision {v3['revision']}")
    checks.check("expected_revision = current revision: accepted", v3["revision"] > v2["revision"])
    try:
        broker_refresh(sid, expected_revision=v1["revision"])
        stale_status, stale_body = None, {}
    except BrokerError as error:
        stale_status, stale_body = error.status, error.body
    print(f"  refresh with expected_revision={v1['revision']} (stale) -> HTTP {stale_status}, vault_status {stale_body.get('vault_status')}, "
          f"revision now {stale_body.get('revision')}")
    checks.check("a stale expected_revision is refused with 409 or 412", stale_status in (409, 412) and stale_body.get("vault_status") in (409, 412), f"HTTP {stale_status}")
    checks.check("the body reports a revision conflict (not 'vault not ready')", stale_body.get("error") == "vault revision conflict" and stale_body.get("revision") == v3["revision"])
    checks.check("the vault's own revision (in the conflict answer) is the accepted one, and the broker kept it",
                 stale_body.get("revision") == v3["revision"] == broker_get(sid)["revision"])
    checks.check("the sandbox's AWS call after the conflict", run(sb, CALL) == 0)
    note("412 is also what the vault answers when it is not Ready (the sidecar is starting), so the broker's body tells the two apart")

    heading("4. the broker's own timer")
    if args.wait_loop:
        print("  waiting for the broker to rotate on its own (up to 35 minutes) ...", flush=True)
        deadline = time.monotonic() + 35 * 60
        revision = broker_get(sid)["revision"]
        while time.monotonic() < deadline and broker_get(sid)["revision"] == revision:
            time.sleep(30)
        after = broker_get(sid)
        checks.check("the broker rotated the credentials by itself", after["revision"] > revision, f"revision {revision} -> {after['revision']}")
        checks.check("the sandbox's AWS call after the broker's own rotation", run(sb, CALL) == 0)
    else:
        print("  skipped: the broker patches the vault every 1,800 s from the last rotation, with the vault's expected revision.")
        print("  (run with --wait-loop to watch one; the unit tests in broker/ cover the loop, including the retry after a failure)")

    heading("5. why the cap matters: the broker's identity")
    who = ""
    try:
        done = subprocess.run(["kubectl", "--context", env_sh("KUBE_CONTEXT"), "-n", "opensandbox-broker", "exec", "deploy/osb-broker", "-c", "broker",
                               "--", "python", "/app/broker.py", "whoami"], capture_output=True, text=True, timeout=90)
        who = scrub(done.stdout + done.stderr)
        print("  $ python broker.py whoami   (in the broker pod)")
        for line in who.splitlines()[:6]:
            print(f"      {line[:170]}")
    except (subprocess.TimeoutExpired, OSError) as error:
        note(f"could not run whoami in the broker pod: {type(error).__name__}")
    checks.check("the broker's identity is a role session from EKS Pod Identity (container-role)", "container-role" in who)
    checks.check("a two-hour AssumeRole is refused for role chaining, not for the role's own maximum",
                 "role chaining" in who and "MaxSessionDuration" not in who)
    note("the role allows two hours (max_session_duration = 7200); the broker's session is a role session, so its AssumeRole is role chaining: at most 3,600 s")

    heading("6. revoke mid-session: delete the vault")
    revoked = broker_revoke(sid)
    print(f"  DELETE /sandboxes/{{id}}/vault -> vault: {revoked['vault']}; next rotation: {revoked['next_rotation_in_seconds']}")
    checks.check("the broker reports the vault revoked and stops rotating", revoked["vault"] == "revoked" and revoked["next_rotation_in_seconds"] is None)
    mark = log_mark(sid)
    out = sh(sb, CALL, timeout=120)
    code = error_code(out.text)
    print(f"  $ {CALL}\n      exit code {out.rc}; client saw: {code}")
    time.sleep(6)  # let the sidecar's log catch up before asserting that a line is absent
    added = [line for line in log_since(sid, mark, "credential proxy") if "applied binding" in line or "re-signed" in line]
    checks.check("the sandbox's next AWS call fails after the vault is deleted: STS rejects the placeholder key", out.rc != 0 and code == "InvalidClientTokenId", code)
    checks.check("the sidecar added no credential and signed nothing", not added, f"{len(added)} line(s)")
    try:
        broker_refresh(sid)
        later = None
    except BrokerError as error:
        later = error.status
    checks.check("the broker refuses to rotate a revoked sandbox", later is not None and later >= 400, f"HTTP {later}")
    state = sh(sb, "echo still running; env | grep -c '^AWS_'").text.replace("\n", " ")
    print(f"  the sandbox itself is still running: {state}")
    note("revoking is the sandbox losing its access; the temporary credentials stay valid at AWS until they expire (at most an hour)")
finally:
    cleanup()
checks.finish()
