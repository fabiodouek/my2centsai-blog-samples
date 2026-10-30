"""Step 2: how injection works, with a throwaway 'canary' secret and an echo
service (httpbin.org) that shows the request it received. No real credentials."""

from common import banner, create_task, https, inject, set_policy, show, sh

NAME = "cred-mech"
CURL = "curl -s -m 15 -w ' -> HTTP %{http_code}\\n'"

banner("a task with no egress policy: no network at all")
create_task(NAME)
show(NAME, "getent hosts httpbin.org || echo 'DNS: NXDOMAIN'")
show(NAME, f"{CURL} https://httpbin.org/get -o /dev/null; echo curl exit $?")

banner("policy: https to httpbin.org, swap X-Canary for the canary secret")
set_policy(NAME, [inject("httpbin.org", "X-Canary", "canary", "value")])
show(NAME, "echo | openssl s_client -connect httpbin.org:443 -servername httpbin.org 2>/dev/null "
           "| openssl x509 -noout -issuer")
show(NAME, f"{CURL} -H 'X-Canary: placeholder' https://httpbin.org/headers | grep -iE 'x-canary|HTTP'")

banner("the header is only filled in when the request carries it")
show(NAME, f"{CURL} https://httpbin.org/headers | grep -iE 'x-canary|HTTP' || echo '(no X-Canary header sent, none injected)'")

banner("a host the policy doesn't list, and plain HTTP to a listed one")
show(NAME, f"{CURL} -o /dev/null https://example.com/; echo curl exit $?")
show(NAME, f"{CURL} -o /dev/null http://httpbin.org/get; echo curl exit $?")

banner("a task that doesn't trust the gateway's CA")
create_task(f"{NAME}-notrust", trust_gateway=False)
set_policy(f"{NAME}-notrust", [https("httpbin.org")])
print(sh(f"{NAME}-notrust", f"{CURL} -o /dev/null https://httpbin.org/get; echo curl exit $?"))

banner("the echo is the catch: a listed host that reflects headers hands the secret back")
print("  Above, httpbin.org returned the canary value to the sandbox. Only list hosts")
print("  you trust not to echo credentials back; the gateway can't tell them apart.")
