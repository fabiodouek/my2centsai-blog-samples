"""Which clients trust the proxy's CA, and through which setting.

With credentialProxy enabled the egress sidecar intercepts ports 80 and 443 and re-signs HTTPS with its
own CA. execd's bootstrap exports NODE_EXTRA_CA_CERTS (the CA alone) and REQUESTS_CA_BUNDLE and
SSL_CERT_FILE (the merged bundle: public roots plus the CA), and installs the CA into the OS store. It
does not export AWS_CA_BUNDLE. The script makes the same HTTPS call from four clients and prints what
each one does: curl, Python requests and Node against api.github.com, and the AWS CLI v2 with
`aws sts get-caller-identity` and made-up keys (a client that trusts the CA gets the service's own
"InvalidClientTokenId" answer; one that does not stops at the TLS handshake).

Then it settles WHY the AWS CLI trusts the CA, by changing one setting at a time in the same sandbox:

  default environment                      as the bootstrap leaves it
  REQUESTS_CA_BUNDLE and SSL_CERT_FILE unset   whatever the OS store and the CLI's own default provide
  only REQUESTS_CA_BUNDLE set               the merged bundle
  only SSL_CERT_FILE set                    the merged bundle
  only AWS_CA_BUNDLE set                    the merged bundle

A (dummy) vault binding is present, as in the real setup. Needs ./00_image.sh. Kills only its own sandboxes.
"""

import uuid

from common import (
    AWS_CA_BUNDLE,
    ECHO_HOST,
    Checks,
    binding,
    safe_cleanup,
    create_sandbox,
    credential,
    deny_policy,
    dummy,
    ensure_port_forward,
    heading,
    note,
    require_server,
    sh,
    show,
)

SCRIPT = "03_ca_trust"
RUN = uuid.uuid4().hex[:10]
checks = Checks()
AWS_ENV = "AWS_ACCESS_KEY_ID=osbplaceholderkeyid AWS_SECRET_ACCESS_KEY=osbplaceholdersecret AWS_DEFAULT_REGION=us-east-1"
STS = "aws sts get-caller-identity --region us-east-1 --cli-connect-timeout 10 --cli-read-timeout 15"
UA = "my2cents-egress-example"


def first_error(text: str) -> str:
    for line in text.splitlines():
        if any(word in line for word in ("SSL", "certificate", "CERTIFICATE", "InvalidClientTokenId", "error", "Error")):
            return line.strip()[:220]
    return text.strip().splitlines()[-1][:220] if text.strip() else "(no output)"


ensure_port_forward()
require_server()
try:
    policy = deny_policy("api.github.com", "sts.us-east-1.amazonaws.com", ECHO_HOST)
    sb = create_sandbox(SCRIPT, RUN, policy=policy, proxy=True)
    sb.credential_vault.create(
        credentials=[credential("carrier", dummy("unused"))],
        bindings=[binding("echo-carrier", [ECHO_HOST], {"type": "apiKey", "name": "x-unused", "credential": "carrier"}, schemes=["http"])],
    )
    print(f"  sandbox {sb.id}, vault revision {sb.credential_vault.get().revision}")

    heading("what the bootstrap set up")
    show(sh(sb, "ls -l /opt/opensandbox/ 2>&1 | head -8; "
                "for v in REQUESTS_CA_BUNDLE SSL_CERT_FILE NODE_EXTRA_CA_CERTS AWS_CA_BUNDLE CURL_CA_BUNDLE GIT_SSL_CAINFO; do "
                "printf '%s=%s\\n' $v \"$(printenv $v || echo '<unset>')\"; done; "
                "ls /usr/local/share/ca-certificates/ 2>&1").text, limit=20)

    heading("the same HTTPS call from four clients")
    results = {}
    out = sh(sb, f"curl -sS -m 15 -A {UA} -o /dev/null -w '%{{http_code}}' https://api.github.com/zen", timeout=40)
    results["curl"] = out.rc == 0 and out.text.strip().endswith("200")
    print(f"  curl              {'trusts the CA' if results['curl'] else 'FAILS'}: {first_error(out.text) if not results['curl'] else 'HTTP 200'}")

    out = sh(sb, "python3 -c \"import requests; print(requests.get('https://api.github.com/zen', timeout=15).status_code)\"", timeout=40)
    results["requests"] = out.rc == 0 and out.text.strip().endswith("200")
    print(f"  Python requests   {'trusts the CA' if results['requests'] else 'FAILS'}: {first_error(out.text) if not results['requests'] else 'HTTP 200'}")

    node = ("const https=require('https');https.get('https://api.github.com/zen',{headers:{'user-agent':'" + UA + "'}},"
            "r=>{console.log(r.statusCode);r.resume()}).on('error',e=>{console.log('ERR '+e.message);process.exit(1)})")
    out = sh(sb, f"node -e \"{node}\"", timeout=40)
    results["node"] = out.rc == 0 and out.text.strip().endswith("200")
    print(f"  Node              {'trusts the CA' if results['node'] else 'FAILS'}: {first_error(out.text) if not results['node'] else 'HTTP 200'}")

    out = sh(sb, f"{AWS_ENV} {STS}", timeout=60)
    aws_default_ok = "InvalidClientTokenId" in out.text
    print(f"  AWS CLI v2        {'trusts the CA' if aws_default_ok else 'FAILS'}: {first_error(out.text)}")

    heading("who signs the certificate the sandbox sees")
    show(sh(sb, "curl -sv -m 15 -o /dev/null https://api.github.com/zen 2>&1 | grep -E 'issuer|subject:' | head -2").text, limit=3)

    heading("why the AWS CLI v2 trusts it: one setting at a time")
    bundle = sh(sb, f"printenv OPENSANDBOX_MERGED_CA || echo {AWS_CA_BUNDLE}").text.strip().splitlines()[-1]
    cases = (
        ("REQUESTS_CA_BUNDLE and SSL_CERT_FILE unset", f"env -u REQUESTS_CA_BUNDLE -u SSL_CERT_FILE {AWS_ENV} {STS}", False),
        ("only REQUESTS_CA_BUNDLE set", f"env -u SSL_CERT_FILE {AWS_ENV} {STS}", True),
        ("only SSL_CERT_FILE set", f"env -u REQUESTS_CA_BUNDLE {AWS_ENV} {STS}", False),
        ("only AWS_CA_BUNDLE set", f"env -u REQUESTS_CA_BUNDLE -u SSL_CERT_FILE AWS_CA_BUNDLE={bundle} {AWS_ENV} {STS}", True),
    )
    observed = {}
    failure_text = ""
    for label, command, expected in cases:
        out = sh(sb, command, timeout=60)
        ok = "InvalidClientTokenId" in out.text
        observed[label] = (ok, expected)
        if not ok and not failure_text:
            failure_text = out.text
        print(f"  {label:60} {'trusts the CA' if ok else 'FAILS'}")
        print(f"        {first_error(out.text)}")

    heading("result")
    checks.check("curl trusts the proxy CA out of the box", results["curl"])
    checks.check("Python requests trusts it (REQUESTS_CA_BUNDLE / SSL_CERT_FILE)", results["requests"])
    checks.check("Node trusts it (NODE_EXTRA_CA_CERTS)", results["node"])
    checks.check("the AWS CLI v2 trusts it with the bootstrap's environment", aws_default_ok)
    for label, (ok, expected) in observed.items():
        checks.check(f"AWS CLI v2, {label}: {'trusts' if expected else 'fails'}", ok == expected)
    print("  exact AWS CLI v2 failure when REQUESTS_CA_BUNDLE is unset (for the post):")
    show(failure_text, limit=4)
    os_store, req_only, ssl_only, aws_only = (observed[c[0]][0] for c in cases)
    if not os_store and req_only and not ssl_only and aws_only:
        note("what the five runs show: the AWS CLI v2 trusts the proxy CA when REQUESTS_CA_BUNDLE or AWS_CA_BUNDLE points at the merged bundle, and does not when only SSL_CERT_FILE is set or none of the three is.")
        note("so here it works because of REQUESTS_CA_BUNDLE (set by the bootstrap); the runs do not test the OS store separately. An entrypoint or image that unsets or overrides that variable needs AWS_CA_BUNDLE=$OPENSANDBOX_MERGED_CA.")
    else:
        note("the five runs above do not match the expected pattern; read them as they are, no mechanism is claimed.")
except Exception as error:
    checks.check(f"script ran to the end ({type(error).__name__})", False, str(error)[:300])
finally:
    heading("clean up")
    safe_cleanup(RUN)

checks.finish()
