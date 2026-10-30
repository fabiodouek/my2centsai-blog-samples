"""Test 1: how Daytona swaps placeholders, using throwaway secrets and public echo services.

No real credentials. Daytona scrubs the real value out of responses, so an echo service always shows
the placeholder and can't prove a swap. Instead this script uses endpoints that check a credential
(httpbin and postman-echo basic-auth: 200 or 401) and request lengths (a swap changes Content-Length).
"""

import base64
import time

from daytona import CreateSecretParams, UpdateSecretParams

from common import banner, create_sandbox, daytona, find_secret, run

HTTPBIN_PASS = "dummy-value-v1"
POSTMAN_BASIC = base64.b64encode(b"postman:password").decode()  # postman-echo's fixed demo login

# name -> (value, hosts). An empty host list means unrestricted: sent to any host.
DUMMIES = {
    "mech-basic": (base64.b64encode(f"demo:{HTTPBIN_PASS}".encode()).decode(), ["httpbin.org"]),
    "mech-raw": (HTTPBIN_PASS + "-padded-to-change-the-length", ["httpbin.org"]),
    "mech-postman": (POSTMAN_BASIC, ["httpbin.org"]),
    "mech-open": (POSTMAN_BASIC, []),
}
for name, (value, hosts) in DUMMIES.items():
    if old := find_secret(name):
        daytona.secret.delete(old.id)
    daytona.secret.create(CreateSecretParams(name=name, value=value, hosts=hosts or None))

sbx = create_sandbox(
    "02", secrets={"BASIC": "mech-basic", "RAW": "mech-raw", "POSTMAN": "mech-postman", "OPEN": "mech-open"}, user=None
)
AUTH = f"https://httpbin.org/basic-auth/demo/{HTTPBIN_PASS}"


def code(cmd: str, tries: int = 4) -> str:
    """HTTP status of a curl call. httpbin.org returns sporadic 502s, so retry those and timeouts."""
    for _ in range(tries):
        c = run(sbx, f"curl -s -m 10 -o /dev/null -w '%{{http_code}}' {cmd} || true")[-3:]
        if c not in ("502", "000"):
            return c
        time.sleep(1)
    return c


def show(cmd: str) -> str:
    """Status plus the start of the body, to tell an upstream answer from the Daytona proxy's own error."""
    out = run(sbx, f"curl -s -m 10 -w ' [%{{http_code}}]' {cmd} || true")
    return " ".join(out.split())[-90:]


def wait_for(status: str, cmd: str, limit: float = 120) -> str:
    t0 = time.perf_counter()
    while (c := code(cmd)) != status and time.perf_counter() - t0 < limit:
        time.sleep(1)
    return f"{c} after {time.perf_counter() - t0:.1f}s"


try:
    banner("A. Inside the sandbox")
    print(run(sbx, "env | grep -E '^(BASIC|RAW|POSTMAN|OPEN)='"))
    print(run(sbx, "env | grep -iE '^(https?_proxy|no_proxy|ssl_cert_file|node_extra_ca_certs|git_ssl_cainfo)='"))

    banner("B. Placeholder in a header, alone and inside a larger string")
    print("Authorization: Basic $BASIC        ->", code(f'-H "Authorization: Basic $BASIC" {AUTH}'))
    print("Authorization: Basic <wrong>       ->", code(f'-H "Authorization: Basic Zm9vOmJhcg==" {AUTH}'))

    banner("C. What the sandbox sees in an echo: the placeholder (responses are scrubbed)")
    print(run(sbx, 'curl -s -H "X-Api-Key: $RAW" https://httpbin.org/headers | grep -i x-api-key'))
    print(run(sbx, 'curl -s --compressed -H "X-Api-Key: $RAW" https://httpbin.org/gzip | grep -i x-api-key'))

    banner("D. Body, URL path and plain HTTP")
    # httpbin reports the Content-Length it received: placeholder length vs real-value length.
    print("placeholder length:", run(sbx, 'printf %s "$RAW" | wc -c'), "| real value length:", len(DUMMIES["mech-raw"][0]))
    print("body Content-Length at httpbin:", run(sbx, 'curl -s -d "$RAW" https://httpbin.org/post | grep -i \'"Content-Length"\''))
    print("placeholder in URL path  ->", show('"https://httpbin.org/anything/$RAW"'))
    print("placeholder in query     ->", show('"https://httpbin.org/anything?k=$RAW"'))
    print("header over plain HTTP   ->", show(f'-H "Authorization: Basic $BASIC" http://httpbin.org/basic-auth/demo/{HTTPBIN_PASS}'))

    banner("E. Host binding")
    print("POSTMAN (bound to httpbin.org) at postman-echo.com ->",
          show('-H "Authorization: Basic $POSTMAN" https://postman-echo.com/basic-auth'))
    print("OPEN (no hosts, unrestricted) at postman-echo.com ->",
          show('-H "Authorization: Basic $OPEN" https://postman-echo.com/basic-auth'))

    banner("F. Rotate: update the value while the sandbox runs")
    basic = find_secret("mech-basic")
    check = f'-H "Authorization: Basic $BASIC" {AUTH}'
    daytona.secret.update(basic.id, UpdateSecretParams(value=base64.b64encode(b"demo:wrong").decode()))
    print("wrong value:", wait_for("401", check))
    print("placeholder unchanged:", run(sbx, "echo $BASIC") == basic.placeholder)
    daytona.secret.update(basic.id, UpdateSecretParams(value=DUMMIES["mech-basic"][0]))
    print("right value:", wait_for("200", check))

    banner("G. Delete the secret while the sandbox runs")
    t0 = time.perf_counter()
    daytona.secret.delete(basic.id)
    seen = []
    while time.perf_counter() - t0 < 90:
        c = run(sbx, f"curl -s -m 10 -o /dev/null -w '%{{http_code}}' {check} || true")[-3:]
        if not seen or seen[-1][1] != c:
            seen.append((round(time.perf_counter() - t0, 1), c))
        if len(seen) > 1 and time.perf_counter() - t0 > 60:
            break
        time.sleep(1)
    print("status changes after delete (seconds, HTTP):", seen)
finally:
    sbx.delete()
    for name in DUMMIES:
        if s := find_secret(name):
            daytona.secret.delete(s.id)
