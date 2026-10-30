"""Test 1: how the LangSmith proxy injects headers, using GitHub as the observable endpoint.

Restricted egress blocks echo services like httpbin, so every check reads GitHub's answer instead:
200 vs 401 on /user, and the rate-limit bucket (60/h unauthenticated vs 5000/h authenticated).
"""

import json
import os

from langsmith.sandbox import opaque_secret, workspace_secret

from common import box, client, create_sandbox, run, secrets_api, banner

token = os.environ["GITHUB_TOKEN"]
secrets_api("POST", json=[{"key": "MECH_BARE_TOKEN", "value": token}])

CODE = "curl -s -m 10 -o /dev/null -w '%{http_code}'"
RATE = "curl -s https://api.github.com/rate_limit | jq -c '.rate | {limit, used}'"

rules = [
    # Prefix + {NAME} in one header value: is the reference templated, or must the secret hold "Bearer ..."?
    {"name": "templated", "match_hosts": ["api.github.com"], "match_paths": ["/user"],
     "headers": [{"name": "Authorization", "type": "workspace_secret", "value": "Bearer {MECH_BARE_TOKEN}"}]},
    # Same host, other paths: a write-only value sent at create time.
    {"name": "opaque", "match_hosts": ["api.github.com"], "match_paths": ["/repos/*"],
     "headers": [{"name": "Authorization", **opaque_secret(f"Bearer {token}")}],
     "env_vars": {"MECH_VISIBLE": "env_vars are stored and returned in plaintext"}},
]
sbx = create_sandbox("02", rules=rules)
try:
    banner("A. Inside the sandbox")
    print(run(sbx, "env | grep -iE '^(MECH|GH_|AWS_|.*TOKEN|.*KEY)' || echo 'no credential env vars'"))
    print(run(sbx, "env | grep -iE 'ssl_cert_file|node_extra_ca_certs|requests_ca_bundle' ; env | grep -i proxy || echo 'no HTTP_PROXY: interception is transparent'"))

    banner("B. Matched host and path: /user")
    print("GET /user                              ->", run(sbx, f"{CODE} https://api.github.com/user"))
    print("  (\"Bearer {MECH_BARE_TOKEN}\" was templated: prefix + reference works)")

    banner("C. Sandbox sends its own Authorization header")
    print("GET /user with 'Bearer bogus'          ->", run(sbx, f"{CODE} -H 'Authorization: Bearer bogus' https://api.github.com/user"))

    banner("D. match_paths: unmatched path gets nothing")
    print("GET /rate_limit (no rule for this path) ->", run(sbx, RATE))
    print("GET /repos/<repo> (opaque rule)        ->", run(sbx, f"{CODE} https://api.github.com/repos/{os.environ['GITHUB_REPO']}"))

    banner("E. Plain HTTP to a rule host")
    print("http://api.github.com/user             ->", run(sbx, f"{CODE} http://api.github.com/user || echo blocked"))

    banner("F. What leaves the process, and who signed the certificate")
    print(run(sbx, "curl -sv https://api.github.com/user -o /dev/null 2>&1 | grep -iE '^> (authorization|host)|issuer'"))

    banner("G. What the API stores for this sandbox")
    print(json.dumps(box(sbx.name).get("proxy_config"), indent=1)[:1500])
finally:
    client.delete_sandbox(sbx.name)
