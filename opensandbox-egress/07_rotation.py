"""Vault rotation: patch with expectedRevision, a stale revision, the effect mid-session, delete as revocation.

A dummy apiKey credential bound to the in-cluster echo server (plain HTTP, port 80). The sandbox keeps
sending the same request; the echo server shows which value arrives.

  1. request                 the echo server receives dummy v1
  2. patch, right revision   replace the credential with v2 (expectedRevision = the current revision): succeeds,
                             the revision goes up, and the very next request carries v2 (no restart, same session)
  3. patch, stale revision   expectedRevision = the old revision: refused; the script records the HTTP status the
                             sidecar actually returns and asserts it is a revision conflict (409 or 412)
  4. delete                  the vault is removed; the next request reaches the echo server without any credential
                             (or is refused): the script records which

Needs ./00_echo.sh and ./00_image.sh. Kills only its own sandboxes.
"""

import logging
import uuid

from opensandbox.exceptions import SandboxApiException

from common import (
    ECHO_HOST,
    Checks,
    binding,
    safe_cleanup,
    conflict_status,
    create_sandbox,
    credential,
    deny_policy,
    dummy,
    echo_reply,
    ensure_port_forward,
    header,
    heading,
    note,
    require_server,
    sh,
)

SCRIPT = "07_rotation"
RUN = uuid.uuid4().hex[:10]
checks = Checks()

ensure_port_forward()
require_server()
try:
    v1, v2 = dummy("v1"), dummy("v2")
    sb = create_sandbox(SCRIPT, RUN, policy=deny_policy(ECHO_HOST), proxy=True)
    state = sb.credential_vault.create(
        credentials=[credential("rotating", v1)],
        bindings=[binding("echo-key", [ECHO_HOST], {"type": "apiKey", "name": "X-Api-Key", "credential": "rotating"}, schemes=["http"])],
    )
    print(f"  sandbox {sb.id}; vault revision {state.revision}")

    def request() -> tuple[int, dict, str]:
        out = sh(sb, f"curl -sS -m 15 -w '\\n@@http=%{{http_code}}' 'http://{ECHO_HOST}/rotate'", timeout=40)
        status = out.text.rsplit("@@http=", 1)[-1].strip() if "@@http=" in out.text else f"curl rc {out.rc}"
        return out.rc, echo_reply(out.text), status

    def seen(label: str) -> str | None:
        rc, reply, status = request()
        value = header(reply, "x-api-key")
        name = {v1: "dummy v1", v2: "dummy v2", None: "no credential"}.get(value, "an unknown value")
        print(f"  {label:34} echo received X-Api-Key = {name}   (HTTP {status})")
        return value

    heading("1. before rotation")
    checks.check("v1 arrives", seen("request") == v1)

    heading("2. patch with the right expectedRevision")
    revision = sb.credential_vault.get().revision
    patched = sb.credential_vault.patch(expected_revision=revision, credentials={"replace": [credential("rotating", v2)]})
    print(f"  expectedRevision={revision} -> accepted; vault revision is now {patched.revision}")
    checks.check("a patch with the current revision succeeds and bumps the revision", patched.revision == revision + 1)
    checks.check("the next request in the same session carries v2", seen("request, same sandbox, no restart") == v2)

    heading("3. patch with a stale expectedRevision")
    logging.getLogger("opensandbox").setLevel(logging.CRITICAL)  # the SDK logs a traceback for the expected refusal
    try:
        sb.credential_vault.patch(expected_revision=revision, credentials={"replace": [credential("rotating", dummy("late"))]})
        status = None
    except SandboxApiException as error:
        status = conflict_status(error)
    logging.getLogger("opensandbox").setLevel(logging.NOTSET)
    print(f"  expectedRevision={revision} (stale, current is {patched.revision}) -> HTTP {status}")
    checks.check(f"a stale expectedRevision is refused with a conflict status (observed: {status})", status in (409, 412))
    checks.check("the refused patch changed nothing: v2 still arrives", seen("request after the refused patch") == v2)
    note(f"the sidecar answers {status} for a revision conflict on this release (the server docs say 409 ErrRevisionConflict; the SDK docstring for server-routed vaults says 412).")

    heading("4. delete as revocation")
    sb.credential_vault.delete()
    print("  vault deleted")
    rc, reply, status = request()
    value = header(reply, "x-api-key")
    outcome = "refused (no echo reply)" if not reply else ("reached the echo server with no credential" if value is None else "STILL INJECTED")
    print(f"  next request: {outcome}   (HTTP {status})")
    checks.check("after delete no credential is injected", value is None, outcome)
    note("an absent vault is not an error for the proxy: it passes the request on uncredentialed. Revocation removes the secret; the network policy decides whether the host is reachable at all.")
except Exception as error:
    checks.check(f"script ran to the end ({type(error).__name__})", False, str(error)[:300])
finally:
    heading("clean up")
    safe_cleanup(RUN)

checks.finish()
