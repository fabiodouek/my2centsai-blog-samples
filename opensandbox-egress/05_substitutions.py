"""All five auth types and the placeholder substitutions, against the in-cluster echo server.

The echo server answers with the request it received, so the script can show what the sandbox sent
(a placeholder) next to what arrived (the injected dummy value). Every value here is a made-up dummy; no
real token is involved. One sandbox, deny-default, the echo server over plain HTTP on port 80
(bindings with schemes ["http"]). Bindings are told apart by path so none overlaps another:

  /bearer/*   bearer          Authorization: Bearer <dummy>
  /basic/*    basic           Authorization: Basic <dummy, already base64>
  /apikey/*   apiKey          X-Api-Key: <dummy>
  /custom/*   customHeaders   X-Tenant: <dummy>, X-Trace: <dummy>
  /sub/*      passthrough     no header; the placeholder PLACEHOLDER_TOKEN is replaced in the path, the query,
                              a header and a JSON body

Needs ./00_echo.sh and ./00_image.sh. Kills only its own sandboxes.
"""

import base64
import uuid

from common import (
    ECHO_HOST,
    Checks,
    binding,
    safe_cleanup,
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

SCRIPT = "05_substitutions"
RUN = uuid.uuid4().hex[:10]
PLACEHOLDER = "PLACEHOLDER_TOKEN"
checks = Checks()

ensure_port_forward()
require_server()
try:
    v_bearer, v_apikey, v_sub = dummy("bearer"), dummy("apikey"), dummy("sub")
    v_tenant, v_trace = dummy("tenant"), dummy("trace")
    v_basic_plain = dummy("basic")
    v_basic = base64.b64encode(f"svc:{v_basic_plain}".encode()).decode()

    sb = create_sandbox(SCRIPT, RUN, policy=deny_policy(ECHO_HOST), proxy=True)
    host = [ECHO_HOST]
    state = sb.credential_vault.create(
        credentials=[credential("c-bearer", v_bearer), credential("c-basic", v_basic), credential("c-apikey", v_apikey),
                     credential("c-tenant", v_tenant), credential("c-trace", v_trace), credential("c-sub", v_sub)],
        bindings=[
            binding("b-bearer", host, {"type": "bearer", "credential": "c-bearer"}, schemes=["http"], paths=["/bearer/*"]),
            binding("b-basic", host, {"type": "basic", "credential": "c-basic"}, schemes=["http"], paths=["/basic/*"]),
            binding("b-apikey", host, {"type": "apiKey", "name": "X-Api-Key", "credential": "c-apikey"}, schemes=["http"], paths=["/apikey/*"]),
            binding("b-custom", host, {"type": "customHeaders", "headers": [
                {"name": "X-Tenant", "credential": "c-tenant"}, {"name": "X-Trace", "credential": "c-trace"}]},
                schemes=["http"], paths=["/custom/*"]),
            binding("b-sub", host, {"type": "passthrough", "substitutions": [
                {"credential": "c-sub", "placeholder": PLACEHOLDER, "in": ["path", "query", "header", "body"]}]},
                schemes=["http"], paths=["/sub/*"]),
        ],
    )
    print(f"  sandbox {sb.id}; vault revision {state.revision}, {len(state.bindings)} bindings")

    def call(path: str, extra: str = "") -> dict:
        out = sh(sb, f"curl -sS -m 15 {extra} 'http://{ECHO_HOST}{path}'", timeout=40)
        reply = echo_reply(out.text)
        if not reply:
            print(f"        no JSON back (curl rc {out.rc}): {out.text[:160]}")
        return reply

    heading("auth types: the sandbox sends no credential")
    reply = call("/bearer/x")
    print(f"  bearer      Authorization = {header(reply, 'authorization')}")
    checks.check("bearer: Authorization: Bearer <dummy> arrived", header(reply, "authorization") == f"Bearer {v_bearer}")
    reply = call("/basic/x")
    print(f"  basic       Authorization = {header(reply, 'authorization')}")
    checks.check("basic: Authorization: Basic <dummy> arrived", header(reply, "authorization") == f"Basic {v_basic}")
    reply = call("/apikey/x")
    print(f"  apiKey      X-Api-Key     = {header(reply, 'x-api-key')}")
    checks.check("apiKey: X-Api-Key arrived", header(reply, "x-api-key") == v_apikey)
    reply = call("/custom/x")
    print(f"  custom      X-Tenant = {header(reply, 'x-tenant')}   X-Trace = {header(reply, 'x-trace')}")
    checks.check("customHeaders: both headers arrived", header(reply, "x-tenant") == v_tenant and header(reply, "x-trace") == v_trace)

    heading("bearer replaces a header the sandbox sets itself")
    reply = call("/bearer/x", "-H 'Authorization: Bearer sandbox-own-value'")
    print(f"  sandbox sent  Authorization: Bearer sandbox-own-value")
    print(f"  echo received Authorization: {header(reply, 'authorization')}")
    checks.check("the injected bearer replaced the sandbox's own Authorization header", header(reply, "authorization") == f"Bearer {v_bearer}")

    heading(f"substitutions: the sandbox sends {PLACEHOLDER}")
    cmd = (f"curl -sS -m 15 -X POST -H 'Content-Type: application/json' -H 'X-Custom: {PLACEHOLDER}' "
           f"-d '{{\"token\":\"{PLACEHOLDER}\"}}' 'http://{ECHO_HOST}/sub/{PLACEHOLDER}/resource?key={PLACEHOLDER}'")
    print(f"  sandbox ran: {cmd}")
    reply = call(f"/sub/{PLACEHOLDER}/resource?key={PLACEHOLDER}", f"-X POST -H 'Content-Type: application/json' -H 'X-Custom: {PLACEHOLDER}' -d '{{\"token\":\"{PLACEHOLDER}\"}}'")
    print(f"  echo received path   : {reply.get('path')}")
    print(f"  echo received header : X-Custom: {header(reply, 'x-custom')}")
    print(f"  echo received body   : {reply.get('body')}")
    path = reply.get("path", "")
    checks.check("path: placeholder replaced", f"/sub/{v_sub}/resource" in path)
    checks.check("query: placeholder replaced", f"key={v_sub}" in path)
    checks.check("header: placeholder replaced", header(reply, "x-custom") == v_sub)
    checks.check("body: placeholder replaced", v_sub in (reply.get("body") or ""))
    everything = str(reply)
    checks.check("the placeholder itself did not arrive anywhere", PLACEHOLDER not in everything)

    heading("a request outside every binding gets nothing injected")
    reply = call("/other/x")
    injected = [n for n in ("authorization", "x-api-key", "x-tenant", "x-trace") if header(reply, n)]
    print(f"  /other/x: injected headers {injected or 'none'}")
    checks.check("no binding matches /other/*: nothing is injected", not injected)
    note("the echo server also logs these requests (kubectl -n opensandbox logs deploy/echo); the values are dummies.")
except Exception as error:
    checks.check(f"script ran to the end ({type(error).__name__})", False, str(error)[:300])
finally:
    heading("clean up")
    safe_cleanup(RUN)

checks.finish()
