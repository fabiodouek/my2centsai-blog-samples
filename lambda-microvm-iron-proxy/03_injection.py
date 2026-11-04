"""Test 2: how injection behaves, with a fake 'demo' secret and httpbin.org as an echo server. First, how a
request reaches iron-proxy with no proxy settings: the name resolves to the NLB, the client talks TLS to
iron-proxy believing it's the real host, and the certificate comes from the demo CA. Then where a placeholder
gets swapped (header, query, body, plain HTTP), where it doesn't (another host, another secret's placeholder,
split, base64), and what a host off the allowlist gets."""

import json
import time

from common import (audit_line, banner, check, is_request, is_tunnel, proxy_audit, rand_label, run, sm, start_log,
                    summary, tf)

start_log("03_injection")
t = tf()
NLB = set(t["nlb_ips"])
demo_real = sm.get_secret_value(SecretId=t["secret_arns"]["demo"])["SecretString"]
print("demo placeholder:", t["placeholders"]["demo"], "| demo real value (fake, safe to show):", demo_real)
since = int(time.time() * 1000)
vm = run("03-injection")

# Prints the certificate a client gets for host h: its subject and who issued it
CERT = ("python3 -c \"import socket,ssl; c=ssl.create_default_context().wrap_socket(socket.create_connection(('{h}',443),10),"
        "server_hostname='{h}'); d=c.getpeercert(); f=lambda k: ', '.join('='.join(x[0]) for x in d[k]); "
        "print('certificate:', f('subject'), '| issuer:', f('issuer'))\" 2>&1 | tail -1")

banner("How a request reaches iron-proxy (no proxy settings in the MicroVM)")
env = vm.sh("env | grep -iE '^(https?|no|all)_proxy=' || echo 'proxy variables: none'")
print(env)
check("no proxy variables in the MicroVM", "proxy variables: none" in env, env)
out = vm.sh("curl -sv -m 20 https://api.github.com/zen -H \"Authorization: token $GH_TOKEN\" 2>&1 | "
            "grep -E '^\\* Connected|^< HTTP' | head -2; " + CERT.format(h="api.github.com"), timeout=60)
print(out)
check("api.github.com: connected to an NLB IP", any(ip in out for ip in NLB), out.splitlines()[0] if out else "")
check("api.github.com: 200 through iron-proxy", "HTTP/2 200" in out or "HTTP/1.1 200" in out)
check("api.github.com: certificate issued by the demo CA (TLS intercepted)", "my2cents iron-proxy demo CA" in out)


def echo(label: str, cmd: str, pick):
    time.sleep(1.5)  # httpbin allows 2 echo requests per second per client IP
    out = vm.sh(cmd, timeout=40)
    try:
        val = pick(json.loads(out))
    except Exception:
        val = out[:200]
    print(f"{label:52} -> {val}")
    return val


banner("Placeholder in the request, as httpbin receives it")
v = echo("Authorization header", "curl -s https://httpbin.org/anything -H \"Authorization: Bearer $DEMO_TOKEN\"",
         lambda j: j["headers"].get("Authorization"))
check("header: swapped", v == f"Bearer {demo_real}", v)
v = echo("custom header X-Token", "curl -s https://httpbin.org/anything -H \"X-Token: $DEMO_TOKEN\"",
         lambda j: j["headers"].get("X-Token"))
check("custom header: swapped (match_headers: [] means every header)", v == demo_real, v)
v = echo("query string", "curl -s \"https://httpbin.org/anything?token=$DEMO_TOKEN\"", lambda j: j["args"])
check("query string: swapped", v == {"token": demo_real}, v)
v = echo("JSON body", "curl -s https://httpbin.org/anything -H 'Content-Type: application/json' -d \"{\\\"token\\\":\\\"$DEMO_TOKEN\\\"}\"",
         lambda j: j["json"])
check("body: swapped", v == {"token": demo_real}, v)
v = echo("plain HTTP (no TLS between proxy and httpbin)", "curl -s http://httpbin.org/anything -H \"X-Token: $DEMO_TOKEN\"",
         lambda j: j["headers"].get("X-Token"))
check("plain HTTP (port 80): intercepted and swapped", v == demo_real, v)
v = echo("placeholder split across two headers", "curl -s https://httpbin.org/anything -H \"X-A: ${DEMO_TOKEN:0:8}\" -H \"X-B: ${DEMO_TOKEN:8}\"",
         lambda j: (j["headers"].get("X-A"), j["headers"].get("X-B")))
check("split placeholder: not swapped", demo_real not in str(v), v)
v = echo("base64 of the placeholder", "curl -s https://httpbin.org/anything -H \"X-Token: $(printf %s $DEMO_TOKEN | base64)\"",
         lambda j: j["headers"].get("X-Token"))
check("base64 placeholder: not swapped", v and demo_real not in str(v), v)

banner("Placeholders sent to the wrong host")
v = echo("GitHub placeholder to httpbin", "curl -s https://httpbin.org/anything -H \"Authorization: token $GH_TOKEN\"",
         lambda j: j["headers"].get("Authorization"))
check("GitHub placeholder reaches httpbin unchanged", v == f"token {t['placeholders']['github']}", v)
v = echo("Anthropic placeholder to httpbin", "curl -s https://httpbin.org/anything -H \"x-api-key: $ANTHROPIC_API_KEY\"",
         lambda j: j["headers"].get("X-Api-Key"))
check("Anthropic placeholder reaches httpbin unchanged", v == t["placeholders"]["anthropic"], str(v)[:40])
v = echo("demo placeholder to api.github.com", "curl -s -o /dev/null -w '%{http_code}' https://api.github.com/user -H \"Authorization: token $DEMO_TOKEN\"",
         lambda j: j)
check("demo placeholder means nothing to GitHub (401)", v == 401, v)

banner("Allowlist")
for label, url in [("https://example.com", "https://example.com"),
                   ("https://api.openai.com/v1/models", "https://api.openai.com/v1/models"),
                   ("https://pypi.org/simple/", "https://pypi.org/simple/"),
                   ("https://<random>.example.com (doesn't exist)", f"https://{rand_label()}.example.com")]:
    code = vm.sh(f"curl -s -m 20 -o /dev/null -w %{{http_code}} {url}", timeout=40)
    print(f"{label:52} -> {code}")
    check(f"{label}: 403 from the proxy", code.strip() == "403", code)
print("response body of a denial:", repr(vm.sh("curl -s -m 20 https://example.com")[:120]))
print("curl's exit code for a denial:", vm.sh("curl -s -m 20 -o /dev/null https://example.com; echo $?"))
out = vm.sh(CERT.format(h="example.com"), timeout=40)
print("TLS for a denied host:", out)
check("a denied host still gets a demo-CA certificate (refused after TLS, not at it)", "my2cents iron-proxy demo CA" in out, out)
vm.terminate()

banner("iron-proxy audit log")
recs = [a for a in proxy_audit(since) if is_request(a)]
for a in recs:
    print(audit_line(a))
check("audit: a swap logged with the demo secret", any("swapped" in json.dumps(a.get("request_transforms") or []) for a in recs))
check("audit: example.com rejected by the allowlist",
      any(a["audit"].get("host", "").startswith("example.com") and a.get("rejected_by") == "allowlist" for a in recs))
check("audit: every request intercepted directly (mode=mitm), no CONNECT", recs and not any(is_tunnel(a) for a in recs)
      and {a["audit"].get("mode") for a in recs} == {"mitm"}, str({a["audit"].get("mode") for a in recs}))
summary()
