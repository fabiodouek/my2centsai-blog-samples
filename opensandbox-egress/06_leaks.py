"""Trying to get a credential out of a sandbox. Every attempt, with its result.

Setup: a deny-default sandbox that may reach the two echo Services, a vault with two DUMMY credentials
(never the real GitHub or Anthropic token), one per binding:

  bind-a   apiKey x-binding-a: <dummy A>   host echo.opensandbox.svc.cluster.local      (listener 8080)
  bind-b   apiKey x-binding-b: <dummy B>   host echo-alt.opensandbox.svc.cluster.local  (listener 8081)

Attempts:
  1. read it:  the environment, /proc/*/environ, /proc/*/cmdline and the small files under /etc, /root,
               /home, /tmp, /opt and /run, searched for the dummy values (the trusted side does the search)
  2. reach the sidecar:  its API on 127.0.0.1:18080 without the egress token
  3. IMDS 169.254.169.254 and the EKS Pod Identity agent 169.254.170.23: both unreachable under deny-default
  4. direct-IP dial of an address no rule allows, and of the echo ClusterIP before any DNS lookup
  5. the Host-header spoof (#1758). Binding selection keys on the Host header, not on where the connection
     goes. The script connects to one echo Service and sends the Host of the other, both ways round, and
     prints which injected headers reached which listener. Verdict LEAK means a credential bound to one
     host was delivered on a connection to another; no leak means it was not; inconclusive means a request got no HTTP answer at all.

Attempts 1 to 4 FAIL the run if a dummy credential is readable, the sidecar API answers without the egress token, or
IMDS / the Pod Identity agent answer. Attempt 5 reports a
finding for the pinned release and exits 0 either way (the enforced test lives with the SigV4 addon in
part 5). Needs ./00_echo.sh and ./00_image.sh. Kills only its own sandboxes.
"""

import uuid

from common import (
    ECHO_ALT_HOST,
    ECHO_HOST,
    Checks,
    binding,
    safe_cleanup,
    create_sandbox,
    credential,
    deny_policy,
    dummy,
    echo_cluster_ip,
    echo_reply,
    ensure_port_forward,
    header,
    heading,
    note,
    probe,
    require_server,
    sh,
    show,
)

SCRIPT = "06_leaks"
RUN = uuid.uuid4().hex[:10]
checks = Checks()
LISTENER = {8080: "echo", 8081: "echo-alt"}

ensure_port_forward()
require_server()
try:
    dummy_a, dummy_b = dummy("a"), dummy("b")
    sb = create_sandbox(SCRIPT, RUN, policy=deny_policy(ECHO_HOST, ECHO_ALT_HOST), proxy=True,
                        env={"CARRIER_A": "osb-placeholder-a", "CARRIER_B": "osb-placeholder-b"})
    sb.credential_vault.create(
        credentials=[credential("cred-a", dummy_a), credential("cred-b", dummy_b)],
        bindings=[
            binding("bind-a", [ECHO_HOST], {"type": "apiKey", "name": "x-binding-a", "credential": "cred-a"}, schemes=["http"]),
            binding("bind-b", [ECHO_ALT_HOST], {"type": "apiKey", "name": "x-binding-b", "credential": "cred-b"}, schemes=["http"]),
        ],
    )
    print(f"  sandbox {sb.id}; dummy credentials bound to {ECHO_HOST} (A) and {ECHO_ALT_HOST} (B)")

    heading("1. read it from inside")
    env = sh(sb, "env | grep CARRIER | sort")
    show(env.text)
    dump = sh(sb, "env; cat /proc/*/environ /proc/*/cmdline 2>/dev/null | tr '\\0' '\\n'; "
                  "find /etc /root /home /tmp /opt /run -xdev -type f -size -64k -readable -exec cat {} + 2>/dev/null; true", timeout=300)
    files = sh(sb, "find /etc /root /home /tmp /opt /run -xdev -type f -size -64k -readable 2>/dev/null | wc -l; "
                   "find /etc /root /home /tmp /opt /run -xdev -type f -size -64k -readable -exec cat {} + >/dev/null 2>&1; echo \"find_rc=$?\"", timeout=120)
    print(f"  file search: {' '.join(files.text.split())} (file count, then find/cat exit status)")
    print(f"  searched {len(dump.text):,} characters of environment, process and file contents")
    checks.check("env holds only placeholders", "osb-placeholder-a" in env.text and dummy_a not in env.text and dummy_b not in env.text)
    checks.check("neither dummy credential is in env, /proc/*/environ, /proc/*/cmdline or small files", dummy_a not in dump.text and dummy_b not in dump.text)

    heading("2. the sidecar's API from inside the pod (shared network namespace)")
    for path in ("/credential-vault", "/credential-vault/_active", "/policy"):
        out = sh(sb, f"curl -s -m 5 -o /tmp/r -w '%{{http_code}}' http://127.0.0.1:18080{path}; echo; head -c 300 /tmp/r", timeout=20)
        status = out.text.splitlines()[0] if out.text else "?"
        print(f"  GET 127.0.0.1:18080{path:26} -> HTTP {status}")
        checks.check(f"the sidecar API refuses a caller without the egress token ({path}: 401 or 403)", status in ("401", "403") and dummy_a not in out.text and dummy_b not in out.text)
    note("the API answers only with the egress auth token, which the sandbox container does not have; the vault's read side never returns values.")

    heading("3. instance metadata and the Pod Identity agent")
    for label, url in (("IMDS 169.254.169.254", "http://169.254.169.254/latest/api/token"),
                       ("Pod Identity agent 169.254.170.23", "http://169.254.170.23/v1/credentials")):
        out = sh(sb, f"rm -f /tmp/r; curl -sS -m 6 -o /tmp/r -w 'http=%{{http_code}}' -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' {url} 2>/dev/null; echo \" curl_rc=$?\"; head -c 200 /tmp/r 2>/dev/null", timeout=25)
        first = out.text.splitlines()[0] if out.text else "?"
        body = "\n".join(out.text.splitlines()[1:])
        print(f"  {label:36} {first}")
        checks.check(f"{label} gives no credentials", "http=2" not in first and "AccessKeyId" not in body and "Token" not in body, out.text[:100])

    heading("4. direct-IP dials")
    ok, detail = probe(sb, "https://1.1.1.1/")
    print(f"  1.1.1.1 (no rule)                    {'reached' if ok else 'refused'} ({detail})")
    checks.check("a direct dial to an address no rule allows fails", not ok)
    ok, detail = probe(sb, f"http://{echo_cluster_ip()}/probe")
    print(f"  echo ClusterIP before any DNS lookup {'reached' if ok else 'refused'} ({detail})")
    note("from the source (not shown by this run): the DNS proxy feeds the addresses it resolves for an allowed name into nftables, which is what opens them.")

    heading("5. the Host-header spoof (#1758): which injected headers reach which listener")

    def request(connect_to: str, host_header: str | None) -> dict:
        extra = f"-H 'Host: {host_header}'" if host_header else ""
        out = sh(sb, f"curl -sS -m 15 -w '\\n@@http=%{{http_code}}' {extra} 'http://{connect_to}/spoof'", timeout=40)
        reply = echo_reply(out.text)
        reply["_http"] = out.text.rsplit("@@http=", 1)[-1].strip() if "@@http=" in out.text else f"rc{out.rc}"
        return reply

    def describe(reply: dict) -> str:
        if "listener" not in reply:
            return f"no echo reply ({'HTTP ' + reply['_http'] if reply['_http'].isdigit() and reply['_http'] != '000' else 'no HTTP answer: ' + reply['_http']})"
        got = [n for n in ("x-binding-a", "x-binding-b") if header(reply, n)]
        return f"reached listener {reply['listener']} ({LISTENER.get(reply['listener'], '?')}), Host {header(reply, 'host')}, injected {got or 'nothing'}"

    control_a = request(ECHO_HOST, None)
    control_b = request(ECHO_ALT_HOST, None)
    print(f"  control, connect {ECHO_HOST}: {describe(control_a)}")
    print(f"  control, connect {ECHO_ALT_HOST}: {describe(control_b)}")
    checks.check("control: each host draws its own credential", header(control_a, "x-binding-a") == dummy_a and header(control_b, "x-binding-b") == dummy_b)

    spoofs = (("connect to echo-alt, Host: echo", ECHO_ALT_HOST, ECHO_HOST, "x-binding-a", dummy_a, 8081),
              ("connect to echo, Host: echo-alt", ECHO_HOST, ECHO_ALT_HOST, "x-binding-b", dummy_b, 8080))
    leaked = False
    replies = []
    for label, connect_to, spoof_host, foreign_header, foreign_value, wrong_listener in spoofs:
        reply = request(connect_to, spoof_host)
        replies.append(reply)
        print(f"  spoof, {label}: {describe(reply)}")
        drawn = header(reply, foreign_header) == foreign_value and reply.get("listener") == wrong_listener
        leaked = leaked or drawn
        if drawn:
            print(f"        the credential bound to {spoof_host} arrived on the connection to {connect_to}")
    def conclusive(r: dict) -> bool:  # an echo reply, or any real HTTP status (a rejection is an answer too)
        return "listener" in r or (r["_http"].isdigit() and r["_http"] != "000")

    verdict = "LEAK" if leaked else ("no leak" if all(conclusive(r) for r in replies) else "inconclusive")
    print(f"\n  HOST-SPOOF VERDICT (#1758, release 1.2.0): {verdict}")
    if leaked:
        note("binding selection keys on the Host header, so a sandbox that may reach host X can draw the credential bound to host Y onto its connection to X.")
        note("mitigation: let a sandbox reach only hosts whose bindings it may use together, or pin the upstream to the verified SNI in an addon (part 5).")
    elif verdict == "inconclusive":
        note("at least one spoofed request got no HTTP answer at all (timeout or connection error), so no verdict is claimed; run again.")
    else:
        note("the spoofed requests drew no foreign credential on this release; the finding is recorded as it ran.")
except Exception as error:
    checks.check(f"script ran to the end ({type(error).__name__})", False, str(error)[:300])
finally:
    heading("clean up")
    safe_cleanup(RUN)

checks.finish()
