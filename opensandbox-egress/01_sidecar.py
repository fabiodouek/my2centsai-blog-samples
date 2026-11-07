"""The egress sidecar exists only when the create request has a network policy.

Two sandboxes from the same image:
  A  no networkPolicy     -> one container, no sidecar, the whole internet is reachable
  B  networkPolicy deny + allow pypi.org, credentialProxy enabled -> an extra egress container

For each, the script lists the pod's containers, asks the server for the diagnostic context, and
curls one allowed and one not-allowed host. The sidecar also carries the platform's `deny.always`
overlay, which therefore exists only on B.

Needs ../opensandbox-eks 01 to 07 and ./00_image.sh. Port-forward: started for you if it is not
running (127.0.0.1:18080). Kills only its own sandboxes (metadata example=opensandbox-egress).
"""

import uuid

from common import (
    Checks,
    safe_cleanup,
    create_sandbox,
    deny_policy,
    egress_container,
    ensure_port_forward,
    heading,
    note,
    pod_containers,
    probe,
    require_server,
    sandbox_pod,
)

SCRIPT = "01_sidecar"
RUN = uuid.uuid4().hex[:10]
checks = Checks()

ensure_port_forward()
require_server()
sandboxes = []
try:
    heading("A: no network policy")
    a = create_sandbox(SCRIPT, RUN)
    sandboxes.append(a)
    pod_a = sandbox_pod(a)
    containers, inits = pod_containers(pod_a)
    print(f"  pod {pod_a}: containers {containers}, init containers {inits}")
    checks.check("A has no egress container", egress_container(pod_a) is None)
    print(f"  diagnostic context: {a.get_diagnostic_context().backend_type} {dict(a.get_diagnostic_context().backend_identifiers)}")
    for url in ("https://pypi.org/simple/", "https://example.org/"):
        reached, detail = probe(a, url)
        print(f"  {url:28} {'reached' if reached else 'blocked'} ({detail})")
        checks.check(f"A reaches {url}", reached)

    heading("B: networkPolicy deny + allow pypi.org, credentialProxy enabled")
    b = create_sandbox(SCRIPT, RUN, policy=deny_policy("pypi.org"), proxy=True)
    sandboxes.append(b)
    pod_b = sandbox_pod(b)
    containers, inits = pod_containers(pod_b)
    print(f"  pod {pod_b}: containers {containers}, init containers {inits}")
    sidecar = egress_container(pod_b)
    checks.check("B has an egress container", sidecar is not None, f"found: {sidecar}")
    print(f"  egress policy as the sidecar enforces it: {b.get_egress_policy().model_dump(by_alias=True, exclude_none=True)}")
    reached, detail = probe(b, "https://pypi.org/simple/")
    checks.check("B reaches the allowed pypi.org", reached, detail)
    reached, detail = probe(b, "https://example.org/")
    checks.check("B cannot reach example.org (not in the policy)", not reached, detail)
    note("the sidecar and the platform's deny.always overlay exist only because the create request carried a networkPolicy;")
    note("without one the pod has a single container and no egress control at all.")
except Exception as error:
    checks.check(f"script ran to the end ({type(error).__name__})", False, str(error)[:200])
finally:
    heading("clean up")
    safe_cleanup(RUN)

checks.finish()
