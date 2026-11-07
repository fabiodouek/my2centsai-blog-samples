"""Egress rule types from one sandbox under deny-default, a runtime patch, and what a tightened
policy does to a connection that is already open.

  1. FQDN allow              pypi.org reachable, example.org (not listed) refused
  2. wildcard allow          *.github.com matches api.github.com but not the apex github.com
  3. CIDR allow              1.0.0.1/32 reachable, 1.1.1.2 (not listed) dropped
  4. port-scoped allow       1.1.1.1/32 on TCP 443 only: https works, http (port 80) dropped
  5. runtime patch           patch_egress_rules adds example.org; it resolves and connects
  6. established connection  a long response from the in-cluster echo server keeps streaming after the
                             policy no longer allows the echo server (#2329); a new connection by name is refused
  7. how to allow the echo   by FQDN (what works, recorded) and what the ClusterIP does after the removal

Plain dns+nft enforcement, no credential proxy: nothing here needs the proxy's CA.
Needs ./00_echo.sh and ./00_image.sh. Kills only its own sandboxes.
"""

import time
import uuid

from opensandbox.models.sandboxes import NetworkRule

from common import (
    ECHO_HOST,
    Checks,
    allow,
    safe_cleanup,
    create_sandbox,
    deny_policy,
    echo_cluster_ip,
    ensure_port_forward,
    heading,
    note,
    probe,
    require_server,
    resolves,
    sh,
)

SCRIPT = "02_rules"
RUN = uuid.uuid4().hex[:10]
checks = Checks()


def line(label: str, reached: bool, detail: str) -> None:
    print(f"  {label:44} {'reached' if reached else 'refused':8} ({detail})")


ensure_port_forward()
require_server()
try:
    policy = deny_policy("pypi.org", "*.github.com", "1.0.0.1/32", allow("1.1.1.1/32", [443]), ECHO_HOST)
    sb = create_sandbox(SCRIPT, RUN, policy=policy)
    print(f"  sandbox {sb.id}; policy: {sb.get_egress_policy().model_dump(by_alias=True, exclude_none=True)}")

    heading("1. FQDN rule")
    ok, d = probe(sb, "https://pypi.org/simple/")
    line("pypi.org (allowed)", ok, d)
    checks.check("allowed FQDN is reachable", ok)
    ok, d = probe(sb, "https://example.org/")
    line("example.org (not listed)", ok, d)
    checks.check("a host that is not listed is refused", not ok)
    print(f"  example.org resolves in the sandbox: {resolves(sb, 'example.org')} (the DNS proxy answers NXDOMAIN)")

    heading("2. wildcard *.github.com")
    ok, d = probe(sb, "https://api.github.com/zen")
    line("api.github.com (sub-label)", ok, d)
    checks.check("the wildcard matches a sub-label", ok)
    ok, d = probe(sb, "https://github.com/")
    line("github.com (the apex)", ok, d)
    checks.check("the wildcard does not match the apex", not ok)
    print(f"  github.com resolves: {resolves(sb, 'github.com')}")

    heading("3. CIDR rule 1.0.0.1/32 (a direct-IP dial)")
    ok, d = probe(sb, "https://1.0.0.1/")
    line("1.0.0.1 (in the CIDR)", ok, d)
    checks.check("an address inside the allowed CIDR is reachable", ok)
    ok, d = probe(sb, "https://1.1.1.2/")
    line("1.1.1.2 (not listed)", ok, d)
    checks.check("an address outside every rule is dropped", not ok)

    heading("4. port-scoped rule 1.1.1.1/32 TCP 443")
    ok, d = probe(sb, "https://1.1.1.1/")
    line("1.1.1.1:443 (allowed port)", ok, d)
    checks.check("the allowed port is reachable", ok)
    ok, d = probe(sb, "http://1.1.1.1/")
    line("1.1.1.1:80 (same IP, other port)", ok, d)
    checks.check("another port on the same IP is dropped", not ok)

    heading("5. runtime patch: add example.org")
    sb.patch_egress_rules([NetworkRule(action="allow", target="example.org")])
    print(f"  policy now: {[r.target for r in sb.get_egress_policy().egress or []]}")
    ok, d = probe(sb, "https://example.org/")
    line("example.org (after the patch)", ok, d)
    checks.check("a host added by patch_egress_rules works at once", ok)

    heading("6. an established connection outlives a tightened policy (#2329)")
    ok, d = probe(sb, f"http://{ECHO_HOST}/probe")
    line(f"{ECHO_HOST} by name", ok, d)
    checks.check("the in-cluster echo server is reachable by FQDN", ok, "allowing by FQDN works" if ok else "FQDN did not work")
    ticks = 40
    started = sh(sb, f"setsid curl -sN -m 120 'http://{ECHO_HOST}/stream?n={ticks}&interval=1' >/tmp/stream.out 2>&1 </dev/null & sleep 4; wc -l </tmp/stream.out")
    before = int(started.text.split()[-1]) if started.text.split() and started.text.split()[-1].isdigit() else -1
    print(f"  stream started: {before} line(s) after 4 s")
    sb.delete_egress_rules([ECHO_HOST])
    print(f"  policy tightened, echo server removed: {[r.target for r in sb.get_egress_policy().egress or []]}")
    ok, d = probe(sb, f"http://{ECHO_HOST}/probe")
    line("a NEW connection by name", ok, d)
    checks.check("a new connection by name is refused after the rule is removed", not ok)
    ok_ip, d_ip = probe(sb, f"http://{echo_cluster_ip()}/probe")
    line("a NEW connection by ClusterIP", ok_ip, d_ip)
    note(f"by IP, after the removal: {'still reached, the DNS-learned lease outlives the rule' if ok_ip else 'refused, the removal also closed the DNS-learned address'}. Recorded, not asserted.")
    time.sleep(ticks - 4 + 4)
    done = sh(sb, "wc -l </tmp/stream.out; tail -n 2 /tmp/stream.out")
    print(f"  the open stream after the removal:")
    for out_line in done.text.splitlines():
        print(f"        {out_line}")
    count = int(done.text.split()[0]) if done.text.split() and done.text.split()[0].isdigit() else 0
    checks.check(f"the established connection delivered all {ticks} ticks", count >= ticks, f"{count} lines")
    note("from the source (not shown by this run): the nftables chain accepts ct-established traffic before any rule, which is why tightening the policy does not cut an open connection.")
except Exception as error:
    checks.check(f"script ran to the end ({type(error).__name__})", False, str(error)[:300])
finally:
    heading("clean up")
    safe_cleanup(RUN)

checks.finish()
