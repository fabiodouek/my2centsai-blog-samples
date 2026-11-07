"""Pause and resume wipe the vault: on Kubernetes the sidecar's vault lives in process memory only.

  1. create a sandbox with a (dummy) vault binding on the echo server; the injected header arrives
  2. write a marker file, pause (the pod is deleted after its root filesystem is committed to ECR), wait for Paused
  3. resume; the marker file is back, but credential_vault.get() shows an empty or missing vault
  4. the same request now reaches the echo server with no credential
  5. a trusted caller re-creates the vault; the request carries the credential again
  6. the audit trail: `credential proxy: applied binding=...` lines (names only, never values) reach the sidecar's
     container log, but in batches: mitmdump's output is block-buffered, so a single request shows nothing yet and
     a burst of requests shows many. The script prints the counts before and after a burst. OSEP-0012's audit fields
     and `opensandbox_credential_proxy_*` metrics are not implemented in 1.2.0 (stated from the source, not checked here)

Pause and resume need the snapshot registry configured by ../opensandbox-lifecycle/00_snapshot_config.sh
(part 3). Needs ./00_echo.sh and ./00_image.sh. Kills only its own sandboxes.
"""

import logging
import time
import uuid
from datetime import timedelta

from opensandbox import SandboxSync
from opensandbox.exceptions import SandboxApiException

from common import (
    ECHO_HOST,
    Checks,
    binding,
    safe_cleanup,
    connection,
    create_sandbox,
    credential,
    deny_policy,
    dummy,
    echo_reply,
    egress_log,
    ensure_port_forward,
    header,
    heading,
    note,
    require_server,
    sh,
    show,
)

SCRIPT = "08_pause_wipe"
RUN = uuid.uuid4().hex[:10]
checks = Checks()

ensure_port_forward()
require_server()
try:
    value = dummy("pause")
    sb = create_sandbox(SCRIPT, RUN, policy=deny_policy(ECHO_HOST), proxy=True)
    vault_args = dict(
        credentials=[credential("pausable", value)],
        bindings=[binding("echo-key", [ECHO_HOST], {"type": "apiKey", "name": "X-Api-Key", "credential": "pausable"}, schemes=["http"])],
    )
    sb.credential_vault.create(**vault_args)
    sandbox_id = sb.id

    def injected(box: SandboxSync, label: str) -> bool:
        out = sh(box, f"curl -sS -m 15 'http://{ECHO_HOST}/pause'", timeout=40)
        got = header(echo_reply(out.text), "x-api-key")
        print(f"  {label:34} echo received X-Api-Key = {'the dummy value' if got == value else (got or 'no credential')}")
        return got == value

    heading("1. before pause")
    checks.check("the credential is injected", injected(sb, "request"))
    before = egress_log(sb, tail=2000)
    print(f"  one request in: {sum('applied binding=' in l for l in before.splitlines())} 'applied binding=' line(s) in the sidecar log so far (mitmdump's output is block-buffered, see step 6)")
    checks.check("no credential value in the sidecar log", value not in before)
    sh(sb, "echo 'written before pause' > /root/marker.txt")

    heading("2. pause")
    started = time.monotonic()
    sb.pause()
    state = ""
    for _ in range(90):
        state = str(sb.get_info().status.state)
        if state.lower() == "paused":
            break
        time.sleep(2)
    print(f"  state {state} after {time.monotonic() - started:.0f} s")
    checks.check("the sandbox reached Paused", state.lower() == "paused")

    heading("3. resume")
    started = time.monotonic()
    sb = SandboxSync.resume(sandbox_id, connection_config=connection("proxy"), resume_timeout=timedelta(seconds=240))
    print(f"  resumed in {time.monotonic() - started:.0f} s")
    marker = sh(sb, "cat /root/marker.txt")
    print(f"  marker file after resume: {marker.text!r}")
    checks.check("the root filesystem came back (marker file)", "written before pause" in marker.text)
    logging.getLogger("opensandbox").setLevel(logging.CRITICAL)  # the SDK logs a traceback for the expected 404
    try:
        state_after = sb.credential_vault.get()
        empty = not state_after.credentials and not state_after.bindings
        print(f"  credential_vault.get(): revision {state_after.revision}, {len(state_after.credentials)} credentials, {len(state_after.bindings)} bindings")
    except SandboxApiException as error:
        empty = error.status_code == 404
        print(f"  credential_vault.get(): HTTP {error.status_code} (no vault)")
    logging.getLogger("opensandbox").setLevel(logging.NOTSET)
    checks.check("the vault is empty after resume", empty)

    heading("4. the same request after resume")
    checks.check("no credential is injected", not injected(sb, "request"))

    heading("5. the trusted caller re-creates the vault")
    sb.credential_vault.create(**vault_args)
    checks.check("the credential is injected again", injected(sb, "request"))

    heading("6. audit trail: the sidecar log")
    one = sum("applied binding=" in l for l in egress_log(sb, tail=2000).splitlines())
    print(f"  after the re-created vault's single request: {one} 'applied binding=' line(s) in the resumed pod's sidecar log")
    sh(sb, f"for i in $(seq 1 90); do curl -s -o /dev/null 'http://{ECHO_HOST}/burst'$i; done", timeout=240)
    time.sleep(5)
    log = egress_log(sb, tail=5000)
    applied = [l for l in log.splitlines() if "applied binding=" in l]
    outbound = [l for l in log.splitlines() if "egress.outbound" in l]
    print(f"  after 90 more requests: {len(applied)} 'applied binding=' line(s), {len(outbound)} egress.outbound event(s)")
    show(applied[-1] if applied else "(none)", limit=1)
    checks.check("the sidecar log carries 'credential proxy: applied binding=' lines (names, no values) once enough output has accumulated", len(applied) > 0 and value not in log)
    note("cause (inferred from the batch pattern in this run, not measured; the source was not modified): the lines arrive in batches, as block-buffering of mitmdump's piped stdout would produce. The batch size (about 8 KiB) is the usual pipe buffer size, not a measured value.")
    note("from the source (launch.go, read, not tested here): only mitmdump lines that start with 'credential proxy:' are relayed to the container log, at warn level; other output is dropped.")
    note("from the source: OSEP-0012's audit fields and opensandbox_credential_proxy_* metrics are not implemented in 1.2.0.")
    note("from the docs: fast-sandbox microVM sandboxes can carry the vault across pause; container sandboxes on Kubernetes cannot.")
except Exception as error:
    checks.check(f"script ran to the end ({type(error).__name__})", False, str(error)[:300])
    if "snapshot" in str(error).lower() or "pause" in str(error).lower():
        note("pause needs the snapshot registry from part 3: ../opensandbox-lifecycle/00_snapshot_config.sh")
finally:
    heading("clean up")
    safe_cleanup(RUN)

checks.finish()
