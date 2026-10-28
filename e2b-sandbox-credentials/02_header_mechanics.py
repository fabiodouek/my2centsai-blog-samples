"""Test 2: how the egress proxy injects, using a throwaway secret and an echo service.

No real credentials. httpbin.org echoes request headers back, which is exactly why you must never
attach a real secret to a host that reflects headers: whatever the proxy adds, the sandbox can read.
"""

import time

from e2b import Sandbox, Secret

from common import DEMO_TAG, banner, run

NAME = "mechanics-dummy"
ECHO = "httpbin.org"

if Secret.exists(NAME):
    Secret.destroy(NAME)
Secret.create(NAME, "dummy-value-v1", metadata=DEMO_TAG)

sbx = Sandbox.create(
    timeout=300,
    metadata={**DEMO_TAG, "test": "02"},
    network={
        "allow_out": lambda ctx: list(ctx.rules.keys()) + ["postman-echo.com"],
        "deny_out": lambda ctx: [ctx.all_traffic],
        "rules": {ECHO: [{"transform": {"headers": {"X-Api-Key": f"Bearer {Secret.fill(NAME)}"}}}]},
    },
)


def header() -> str:
    out = run(sbx, f"curl -s https://{ECHO}/headers | grep -i x-api-key || echo '(no X-Api-Key header)'")
    return out.strip()


try:
    banner("A. Inside the sandbox")
    print(run(sbx, "env | grep -i -E 'dummy|secret' || echo 'no secret in env'"))

    banner("B. Plain request: header added, placeholder resolved inside a larger string")
    print(header())

    banner("C. Sandbox sets the same header itself")
    print(run(sbx, f"curl -s -H 'X-Api-Key: set-by-sandbox' https://{ECHO}/headers | grep -i x-api-key"))

    banner("D. Placeholder in the body and URL is not substituted")
    print(run(sbx, f"curl -s -X POST -d 'k=${{e2b.secrets.{NAME}}}' https://{ECHO}/post | grep -A1 '\"form\"'"))
    print(run(sbx, f"curl -s -g 'https://{ECHO}/get?k=${{e2b.secrets.{NAME}}}' | grep '\"k\"'"))

    banner("E. Allowed host without a rule gets nothing")
    print(run(sbx, "curl -s https://postman-echo.com/headers"))

    banner("F. Host not on the allowlist")
    print(run(sbx, "curl -s -m 5 -o /dev/null -w '%{http_code}' https://example.com || echo ' blocked'"))

    banner("G. Plain HTTP to the rule host")
    print(run(sbx, f"curl -s -m 10 http://{ECHO}/headers | grep -i x-api-key || echo '(no X-Api-Key header)'"))

    banner("H. Rotate: Secret.update while the sandbox runs")
    t0 = time.perf_counter()
    Secret.update(NAME, "dummy-value-v2")
    while "v2" not in (h := header()):
        time.sleep(0.5)
    print(f"{h}   (seen after {time.perf_counter() - t0:.1f}s, no restart)")

    banner("I. Delete the secret while the sandbox runs")
    Secret.destroy(NAME)
    time.sleep(2)
    print(header())
    print(run(sbx, f"curl -s -o /dev/null -w 'HTTP %{{http_code}}' https://{ECHO}/headers"))
finally:
    sbx.kill()
    if Secret.exists(NAME):
        Secret.destroy(NAME)
