"""Test 11: DNS in the sandbox VPC. Route 53 Resolver DNS Firewall blocks every name except
proxy.sandbox.internal with an OVERRIDE: a CNAME to proxy.sandbox.internal, whose private-zone A record holds
both NLB IPs. The Resolver follows the CNAME itself, so one answer carries the CNAME and both addresses, and
nothing recurses to the internet.

First the proof: the answer for a public name, every record type, every resolver address over UDP and TCP,
names whose real answer is predictable (nip.io, sslip.io), TXT records only an authoritative server could give,
and the firewall action on each query in the Resolver query log.

Then the misconfiguration this depends on not happening: with mutation protection on, removing the firewall
association is refused; with it switched off, removal works, recursion comes back within about a second, and
the EventBridge alarm fires. The association is restored with a targeted `terraform apply` (protection back
on), in a `finally` so a failed run doesn't leave the sandboxes with recursive DNS.

  python 12_dns_firewall.py                  # the proof, then the misconfiguration check
  python 12_dns_firewall.py --no-misconfig   # the proof only (changes nothing)"""

import ipaddress
import json
import re
import sys
import time

from common import (HERE, banner, check, digs, firewall_association, fmt_answers, laptop, logs, qlog_line, query_log,
                    r53r, rand_label, run, start_log, summary, terraform, tf)

MISCONFIG = "--no-misconfig" not in sys.argv
start_log("12_dns_firewall")
t = tf()
NLB = set(t["nlb_ips"])
PROXY = t["proxy_name"].rstrip(".") + "."
since = int(time.time() * 1000)
vm = run("12-dns", max_duration=2400)
print(f"MicroVM {vm.id} | proxy env: {vm.sh('echo ${HTTPS_PROXY:-none}')} | NLB {sorted(NLB)}")
print(vm.sh("echo resolv.conf:; grep -v '^#' /etc/resolv.conf"))

banner("One answer: the override CNAME and both NLB addresses")
print("$ dig api.github.com")
d = digs(vm, ["api.github.com"])["api.github.com"]
print("\n".join(l for l in d["raw"].splitlines() if l.startswith((";; ->>", ";; flags", ";; SERVER", ";; Query time")) or
                ("\tIN\t" in l and not l.startswith(";"))))
cname = [a for a in d["answers"] if a[1] == "CNAME"]
check("dig: override CNAME to proxy.sandbox.internal", bool(cname) and cname[0][2] == PROXY, fmt_answers(cname))
check("dig: both NLB A records in the same answer", {a[2] for a in d["answers"] if a[1] == "A"} == NLB)
r = rand_label()
for label, cmd in {
    f"getent hosts {r}.example.com": f"getent hosts {r}.example.com || echo 'getent failed rc='$?",
    "python3 socket.getaddrinfo('api.github.com')": "python3 -c \"import socket;print(sorted({a[4][0] for a in socket.getaddrinfo('api.github.com',443)}))\"",
    "node dns.lookup (getaddrinfo)": "node -e \"require('dns').lookup('api.github.com',{all:true},(e,a)=>console.log(e?e.code:a.map(x=>x.address).sort().join(' ')))\"",
    "node dns.resolve4 (c-ares, its own resolver)": "node -e \"require('dns').resolve4('api.github.com',(e,a)=>console.log(e?e.code:a.sort().join(' ')))\"",
}.items():
    out = vm.sh(cmd, timeout=40)
    print(f"{label:46} {out.splitlines()[0] if out else ''}")
    ips = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", out))
    check(f"{label}: the NLB IPs only", ips and ips <= NLB, " ".join(sorted(ips)) or out[:80])

banner("Every record type gets only the override")
rr = rand_label()
res = digs(vm, [f"{n} {ty}" for n in ("api.github.com", f"{rr}.example.com") for ty in ("A", "AAAA", "TXT", "MX", "TYPE65", "NS")]
           + ["-x 1.1.1.1", "-x 140.82.112.3"])
for q, v in res.items():
    print(f"{q.replace(rr, '<random>'):34} {v['status']:9} {fmt_answers(v['answers'])}")
    real = [a for a in v["answers"] if a[1] not in ("CNAME", "A") or a[2] not in NLB | {PROXY}]
    check(f"dig {q.replace(rr, '<random>')}: override only", not real and v["status"] == "NOERROR", fmt_answers(real) if real else v["status"])

banner("Every resolver address, over UDP and TCP")
rr = rand_label()
servers = [str(ipaddress.ip_network(t["sandbox_vpc_cidr"])[2]), "169.254.169.253", "127.0.0.2", "169.254.53.53"]
qs = [f"@{s} {rr}-{i}.example.com A" for i, s in enumerate(servers)] + [f"@{s} google.com TXT" for s in servers] + \
     [f"@{s} +tcp {rr}-tcp{i}.example.com A" for i, s in enumerate(servers[:2])]
for q, v in digs(vm, qs).items():
    print(f"{q.replace(rr, '<random>'):46} {v['status']:9} {fmt_answers(v['answers'])}")
    if v["status"] == "TIMEOUT":
        check(f"dig {q.replace(rr, '<random>')}: no answer at all", True, "timed out")
    else:
        real = [a for a in v["answers"] if a[1] == "TXT" or (a[1] == "A" and a[2] not in NLB)]
        check(f"dig {q.replace(rr, '<random>')}: override only", not real and v["status"] == "NOERROR", fmt_answers(real) if real else v["status"])


def magic_names() -> dict:
    rr = rand_label()
    return {"10-1-2-3.nip.io": "10.1.2.3", f"{rr}.10.1.2.3.nip.io": "10.1.2.3", f"{rr}-10-1-2-3.nip.io": "10.1.2.3",
            "1.2.3.4.sslip.io": "1.2.3.4", f"{rr}.1.2.3.4.sslip.io": "1.2.3.4"}


banner("Nothing recursed: names whose real answer is predictable")
magic = magic_names()
res = digs(vm, [f"{n} A" for n in magic])
for n, real in magic.items():
    v = res[f"{n} A"]
    # @1.1.1.1: a home resolver may drop private answers like 10.1.2.3 (DNS-rebinding protection)
    on_laptop = laptop(f"dig +short +time=3 +tries=1 @1.1.1.1 {n} A")
    got = {a[2] for a in v["answers"] if a[1] == "A"}
    print(f"{n:34} MicroVM: {', '.join(sorted(got)) or v['status']:26} this machine: {on_laptop or '(nothing)'}")
    check(f"{n}: the NLB IPs, not the embedded {real}", got == NLB, ", ".join(sorted(got)))

banner("Nothing recursed: TXT records only an authoritative server can give")
for name in ("google.com", "o-o.myaddr.l.google.com"):
    v = digs(vm, [f"{name} TXT"])[f"{name} TXT"]
    lap = laptop(f"dig +short +time=3 +tries=1 {name} TXT")
    txt = [a for a in v["answers"] if a[1] == "TXT"]
    print(f"{name} TXT\n  MicroVM:      {v['status']} {fmt_answers(v['answers'])}")
    # Google answers o-o.myaddr with the IP of the resolver that asked; don't print this machine's
    print(f"  this machine: {len(lap.splitlines())} TXT record(s)" + ("" if name.startswith("o-o.") else ": " + lap.replace("\n", " | ")[:300]))
    check(f"{name} TXT: no real TXT data in the MicroVM", not txt and bool(lap), f"MicroVM {len(txt)} TXT, this machine {len(lap.splitlines())}")


def recursion_probe() -> set:
    """Resolve a fresh <random>.10.1.2.3.nip.io: the NLB IPs with the override, 10.1.2.3 without it."""
    out = vm.sh(f"dig +short +time=2 +tries=1 {rand_label()}.10.1.2.3.nip.io A", timeout=30)
    return {w for w in out.split() if w[:1].isdigit()}


until = int(time.time() * 1000)
if MISCONFIG:
    banner("Misconfiguration: remove the firewall association")
    assoc_id = t["firewall_association_id"]
    a = firewall_association(assoc_id)
    print(f"association {a['Status']}, mutation protection {a['MutationProtection']}")
    since_alarm = int(time.time() * 1000) - 5000
    changed = accepted = False
    try:
        if a["MutationProtection"] == "ENABLED":
            try:
                r53r.disassociate_firewall_rule_group(FirewallRuleGroupAssociationId=assoc_id)
                refused, changed, accepted = None, True, True
            except Exception as e:
                refused = f"{e.response['Error']['Code']}: {e.response['Error']['Message'][:120]}" if hasattr(e, "response") else str(e)
            print("disassociate with protection on ->", refused or "ACCEPTED")
            check("mutation protection refuses DisassociateFirewallRuleGroup", refused is not None, refused or "accepted")
            if refused:
                t0 = time.perf_counter()
                r53r.update_firewall_rule_group_association(FirewallRuleGroupAssociationId=assoc_id, MutationProtection="DISABLED")
                changed = True
                while time.perf_counter() - t0 < 300:
                    cur = firewall_association(assoc_id) or {}
                    if cur.get("Status") == "COMPLETE" and cur.get("MutationProtection") == "DISABLED":
                        break
                    time.sleep(2)
                print(f"mutation protection switched off ({time.perf_counter() - t0:.0f}s)")
        else:
            print("mutation protection is already DISABLED (firewall_mutation_protection); skipping the refusal check")

        t0 = time.perf_counter()
        if not accepted:
            changed = True
            r53r.disassociate_firewall_rule_group(FirewallRuleGroupAssociationId=assoc_id)
        print("DisassociateFirewallRuleGroup called")
        gone = exposed = None
        while time.perf_counter() - t0 < 600 and (gone is None or exposed is None):
            if gone is None and firewall_association(assoc_id) is None:
                gone = time.perf_counter() - t0
                print(f"  [{gone:5.0f}s] association gone")
            if exposed is None:
                got = recursion_probe()
                if "10.1.2.3" in got:
                    exposed = time.perf_counter() - t0
                    print(f"  [{exposed:5.0f}s] <random>.10.1.2.3.nip.io -> {sorted(got)}: recursion is back")
            time.sleep(1 if exposed is None else 3)
        check("without the association, recursion returns (10.1.2.3.nip.io -> 10.1.2.3)", exposed is not None,
              f"after {exposed:.0f}s" if exposed else "not within 600 s")
        for q, v in digs(vm, ["google.com TXT", f"{rand_label()}.1.2.3.4.sslip.io A"]).items():
            print(f"  {q:36} {v['status']} {fmt_answers(v['answers'])[:160]}")
        out = vm.sh("curl -s -m 8 -o /dev/null -w '%{http_code}' https://example.com || echo ' FAIL rc='$?", timeout=30)
        print(f"  curl https://example.com (now its real IP; the routes still block it): {out}")
        check("the fence still blocks the real IP", "FAIL" in out, out)

        banner("The EventBridge alarm")
        found, seen_after = [], None
        while time.perf_counter() - t0 < 600:
            evs = logs.filter_log_events(logGroupName=t["alarm_log_group"], startTime=since_alarm).get("events", [])
            found = [json.loads(e["message"]) for e in evs]
            if any(f["detail"].get("eventName") == "DisassociateFirewallRuleGroup" and not f["detail"].get("errorCode") for f in found):
                seen_after = time.perf_counter() - t0
                break
            time.sleep(10)
        for f in sorted(found, key=lambda f: f["time"]):
            dd = f["detail"]
            print(f"  {f['time']}  {dd.get('eventName'):36} {dd.get('errorCode') or 'succeeded'}")
        check("EventBridge alarm fired on the disassociation",
              any(f["detail"].get("eventName") == "DisassociateFirewallRuleGroup" and not f["detail"].get("errorCode") for f in found),
              f"readable {seen_after:.0f}s after the call" if seen_after else f"{len(found)} events")
    finally:
        if changed:
            banner("Restore: terraform apply (the association, protection back on)")
            (HERE / "infra" / "logs").mkdir(exist_ok=True)
            t2 = time.perf_counter()
            applied = terraform("apply", "-auto-approve", "-target=aws_route53_resolver_firewall_rule_group_association.sandbox",
                                log=HERE / "infra" / "logs" / "12_restore.log")
            print(f"terraform apply: {applied:.0f}s")
            restored = None
            while time.perf_counter() - t2 < applied + 600:
                if recursion_probe() == NLB:
                    restored = time.perf_counter() - t2
                    break
                time.sleep(3)
            print(f"override back {restored:.0f}s after the apply started" if restored else "override NOT back")
            check("override restored by terraform apply", restored is not None, f"{restored:.0f}s" if restored else "")
            a = firewall_association(tf(refresh=True)["firewall_association_id"]) or {}
            print(f"new association: {a.get('Status')}, mutation protection {a.get('MutationProtection')}")
            check("the new association has mutation protection on", a.get("MutationProtection") == "ENABLED", a.get("MutationProtection"))
vm.terminate()

banner("Resolver query log for the sandbox VPC: the firewall action on each query")
recs = query_log(since, until_ms=until, wait=180, min_count=10)
seen = {}
for q in recs:
    seen.setdefault((q.get("query_name"), q.get("query_type")), q)
print(f"{len(recs)} records, {len(seen)} unique name/type pairs (the Resolver logs only what it didn't answer from cache)")
for q in list(seen.values())[:25]:
    print(qlog_line(q))
actions = {}
for q in recs:
    actions[q.get("firewall_rule_action", "none")] = actions.get(q.get("firewall_rule_action", "none"), 0) + 1
print("firewall_rule_action counts:", actions)
check("every logged query carries a firewall action", recs and all(q.get("firewall_rule_action") in ("BLOCK", "ALLOW") for q in recs),
      str(actions))
summary()
