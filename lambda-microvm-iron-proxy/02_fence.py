"""Test 1: the fence. The sandbox VPC has no internet gateway and no NAT gateway. Its only routes besides
`local` are the NLB's two /32s over the peering connection, and the connector's security group may only reach
the NLB's security group (referenced across the peering) on 80, 443 and 8080. Every name resolves to the NLB
(DNS Firewall, Test 11), so the only way to even try a direct connection is a raw IP: those time out, and so
does everything else in the proxy VPC and every IPv6 destination. The proxy path works."""

import ipaddress
import time

from common import (audit_line, banner, check, ec2, is_request, laptop, proxy_audit, proxy_task_ips, real_ip, run,
                    start_log, summary, tf)

start_log("02_fence")
t = tf()
nlb = t["nlb_ips"]
NLB = set(nlb)
nat_ip = t["nat_private_ip"]
task_ips = proxy_task_ips()

banner("The sandbox VPC: routes, gateways, the connector's security group")
rt = ec2.describe_route_tables(RouteTableIds=[t["sandbox_route_table_id"]])["RouteTables"][0]
for r in rt["Routes"]:
    print(f"  route {r.get('DestinationCidrBlock', r.get('DestinationIpv6CidrBlock')):18} -> "
          f"{r.get('VpcPeeringConnectionId') or r.get('GatewayId') or r.get('NatGatewayId') or '?'} ({r['State']})")
dests = {r.get("DestinationCidrBlock") for r in rt["Routes"]}
check("sandbox routes: local + the two NLB /32s, nothing else", dests == {t["sandbox_vpc_cidr"]} | {f"{ip}/32" for ip in nlb},
      ", ".join(sorted(d for d in dests if d)))
igw = ec2.describe_internet_gateways(Filters=[{"Name": "attachment.vpc-id", "Values": [t["sandbox_vpc_id"]]}])["InternetGateways"]
nat = ec2.describe_nat_gateways(Filters=[{"Name": "vpc-id", "Values": [t["sandbox_vpc_id"]]},
                                         {"Name": "state", "Values": ["available", "pending"]}])["NatGateways"]
print(f"  internet gateways: {len(igw)}, NAT gateways: {len(nat)}")
check("sandbox VPC: no internet gateway, no NAT gateway", not igw and not nat, f"{len(igw)} IGW, {len(nat)} NAT")
for r in ec2.describe_security_group_rules(Filters=[{"Name": "group-id", "Values": [t["connector_sg_id"]]}])["SecurityGroupRules"]:
    ref = r.get("ReferencedGroupInfo") or {}
    print(f"  connector SG {'egress ' if r['IsEgress'] else 'ingress'} {r['IpProtocol']} {r.get('FromPort')}-{r.get('ToPort')} -> "
          f"{ref.get('GroupId') or r.get('CidrIpv4') or r.get('CidrIpv6')}"
          f"{' (NLB SG, across peering ' + ref['VpcPeeringConnectionId'] + ')' if ref.get('VpcPeeringConnectionId') else ''}")
enis = ec2.describe_network_interfaces(Filters=[{"Name": "vpc-id", "Values": [t["sandbox_vpc_id"]]}],
                                       IncludeManagedResources=True)["NetworkInterfaces"]
print("  connector ENIs (Lambda-managed, listed only with IncludeManagedResources): "
      + "; ".join(f"{e['PrivateIpAddress']} {e.get('AvailabilityZone')}" for e in enis))

since = int(time.time() * 1000)
vm = run("02-fence")
print(f"\nMicroVM {vm.id} on {t['connector_arn'].split(':')[-1]} | proxy env: {vm.sh('echo ${HTTPS_PROXY:-none}')} | NLB {nlb}")

CURL = "curl -s -m 8 -o /dev/null -w '%{{http_code}}' {args} || echo ' FAIL rc='$?"


def tcp(host: str, port: int, secs: int = 5) -> str:
    """Open a TCP connection with bash's /dev/tcp: 'open', 'timeout' or 'refused'."""
    out = vm.sh(f"timeout {secs} bash -c 'exec 3<>/dev/tcp/{host}/{port}' 2>&1; echo rc=$?", timeout=secs + 15)
    rc = out.rsplit("rc=", 1)[-1].strip()
    return "open" if rc == "0" else "timeout" if rc == "124" else f"refused ({out.splitlines()[0][:50]})"


def probe(label: str, cmd: str, timeout: int = 40) -> str:
    out = vm.sh(cmd, timeout=timeout)
    first = out.splitlines()[0] if out else ""
    print(f"{label:48} {first}")
    return out


def tcp_probe(label: str, host: str, port: int) -> str:
    res = tcp(host, port)
    print(f"{label:48} {res}")
    check(f"{label}: no connection", res != "open", res)
    return res


banner("Probes: what the fence allows (the NLB on 80, 443, 8080)")
for ip in nlb:
    res = {p: tcp(ip, p) for p in (80, 443, 8080)}
    print(f"{'NLB ' + ip:48} " + ", ".join(f"{p} {r}" for p, r in res.items()))
    check(f"NLB {ip} reachable on 80, 443 and 8080", all(r == "open" for r in res.values()), str(res))

banner("Probes: direct egress")
for name, scheme in (("httpbin.org", "https"), ("example.com", "http")):
    ip = real_ip(name)
    if ip:
        port = 443 if scheme == "https" else 80
        out = probe(f"direct {scheme.upper()} to {name}'s real IP (--resolve)",
                    CURL.format(args=f"--resolve {name}:{port}:{ip} {scheme}://{name}/"))
        check(f"direct {scheme} to {name}'s real IP fails", "FAIL" in out, out)
out = probe("direct raw IP 1.1.1.1:443", CURL.format(args="https://1.1.1.1"))
check("direct raw IP 1.1.1.1:443 fails", "FAIL" in out, out)
tcp_probe("TCP 1.1.1.1:22", "1.1.1.1", 22)
tcp_probe("TCP 140.82.112.3:443 (a GitHub address)", "140.82.112.3", 443)
tcp_probe("TCP github.com:22 (git over SSH)", "github.com", 22)
tcp_probe("TCP example.com:25 (SMTP)", "example.com", 25)
out = probe("DNS to 1.1.1.1:53 (UDP)", "dig +time=3 +tries=1 +short @1.1.1.1 example.com 2>&1 | head -1")
check("UDP DNS to 1.1.1.1 fails", "timed out" in out or "no servers" in out, out)
out = probe("DNS to 8.8.8.8:53 (TCP)", "dig +time=3 +tries=1 +tcp @8.8.8.8 example.com 2>&1 | grep -E 'timed out|status|refused|reached' | head -1")
check("TCP DNS to 8.8.8.8 fails", "status:" not in out, out)

banner("Probes: the rest of the proxy VPC")
out = probe(f"NLB {nlb[0]} on another port (9090)", CURL.format(args=f"http://{nlb[0]}:9090/healthz"))
check("NLB 9090 (health port) unreachable", "FAIL" in out, out)
tcp_probe(f"NLB {nlb[1]}:22", nlb[1], 22)
tcp_probe(f"NAT gateway ENI {nat_ip}:443", nat_ip, 443)
for ip in task_ips:
    res = {p: tcp(ip, p) for p in (80, 443, 8080, 9090)}
    print(f"{'iron-proxy task ' + ip + ', bypassing the NLB':48} " + ", ".join(f"{p} {r}" for p, r in res.items()))
    check(f"iron-proxy task {ip} unreachable directly", all(r != "open" for r in res.values()), str(res))
out = probe("proxy VPC resolver 10.50.0.2:53 (UDP)", "dig +time=3 +tries=1 +short @10.50.0.2 example.com 2>&1 | head -1")
check("the proxy VPC's resolver is unreachable", "timed out" in out or "no servers" in out, out)

banner("Probes: IPv6")
# Lambda gives the MicroVM a global IPv6 address and a default route, whatever the VPCs do (neither has IPv6)
print(vm.sh("a=$(ip -6 addr show scope global 2>/dev/null | grep -oE 'inet6 [0-9a-f]+:[0-9a-f]+:' | head -1); "
            "echo \"global IPv6: ${a:-none}${a:+…/128}\"; echo \"default IPv6 route: $(ip -6 route show default 2>&1 | head -1 | sed -E 's/src [0-9a-f:]+/src <its global IPv6>/')\""))
out = probe("IPv6 HTTPS to [2606:4700:4700::1111]", "curl -6 -s -m 6 -o /dev/null -w '%{http_code}' https://[2606:4700:4700::1111] || echo ' FAIL rc='$?")
check("IPv6 HTTPS fails", "FAIL" in out, out)
for host, port in [("2001:4860:4860::8888", 443), ("2001:4860:4860::8888", 53), ("2606:4700:4700::1111", 80)]:
    tcp_probe(f"IPv6 TCP [{host}]:{port}", host, port)
out = probe("IPv6 DNS (UDP) to 2001:4860:4860::8888", "dig +time=3 +tries=1 +short @2001:4860:4860::8888 example.com 2>&1 | head -1")
check("IPv6 UDP DNS fails", "status:" not in out and not out[:1].isdigit(), out)
# The MicroVM's IPv6 address is in AWS space, so try AWS's own dual-stack endpoints too (resolved on this machine)
for name in ("sts.us-east-1.api.aws", "s3.dualstack.us-east-1.amazonaws.com"):
    v6 = laptop(f"dig +short AAAA {name} @1.1.1.1 | grep : | head -1")
    if v6:
        out = probe(f"IPv6 HTTPS to {name}", f"curl -6 -sk -m 6 -o /dev/null -w '%{{http_code}}' https://[{v6}]/ || echo ' FAIL rc='$?")
        check(f"IPv6 HTTPS to {name} fails", "FAIL" in out, out)

banner("Probes: through the proxy")
out = probe("through the proxy, allowlisted host", CURL.format(args="https://httpbin.org/get").replace("-m 8", "-m 15"))
check("allowlisted host through the proxy: 200", out.strip() == "200", out)
out = probe("egress IP seen by httpbin (via proxy)", "curl -s -m 15 https://httpbin.org/ip | tr -d ' \\n'")
check("egress through the proxy VPC's NAT gateway", "<nat-eip>" in out or t["nat_public_ip"] in out, out)
out = probe("through the proxy, not allowlisted", CURL.format(args="https://example.com").replace("-m 8", "-m 15"))
check("off-allowlist host: 403 from the proxy", out.strip() == "403", out)
out = probe("system resolver: public name", "getent hosts example.com")
check("example.com resolves to the NLB", {w for w in out.split() if w[:1].isdigit()} <= NLB and out, out)
vpc_resolver = str(ipaddress.ip_network(t["sandbox_vpc_cidr"])[2])
out = probe(f"VPC resolver {vpc_resolver} directly", f"dig +time=3 +tries=1 +short @{vpc_resolver} example.com | tr '\\n' ' '")
check("the sandbox VPC's resolver answers with the override", "proxy.sandbox.internal" in out, out)
out = probe("IMDS role (no execution role)", "T=$(curl -s -m 3 -X PUT http://169.254.169.254/latest/api/token "
            "-H 'X-aws-ec2-metadata-token-ttl-seconds: 60'); curl -s -m 3 -H \"X-aws-ec2-metadata-token: $T\" "
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/ || echo none")
check("no execution role at the metadata endpoint", "Resource not found" in out or out.strip() in ("", "none"), out[:80])
print(f"\n(NAT gateway public IP: {t['nat_public_ip']})")

banner("iron-proxy audit log for this test")
recs = [a for a in proxy_audit(since) if is_request(a)]
for a in recs:
    print(audit_line(a))
check("the proxy saw the allowlisted request", any(a["audit"].get("host") == "httpbin.org" for a in recs))
vm.terminate()
summary()
