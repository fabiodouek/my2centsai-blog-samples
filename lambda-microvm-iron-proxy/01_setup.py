"""Setup: fill the Secrets Manager secrets iron-proxy injects (from .env), check that iron-proxy is healthy
behind the NLB in the proxy VPC, show the sandbox VPC's guard rails (routes, DNS Firewall), and build the
sandbox image with the demo CA baked in."""

import os
import secrets
import time

from common import (banner, build_image, check, ec2, ecs, firewall_association, sm, start_log, summary,
                    target_health, tf, wait_proxy_ready)

start_log("01_setup")
for k in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GITHUB_REPO"):
    if not os.environ.get(k):
        raise SystemExit(f"{k} missing from .env")

banner("Secrets (values come from .env and go only to Secrets Manager)")
t = tf()
values = {
    "anthropic": os.environ["ANTHROPIC_API_KEY"],
    "github": os.environ["GITHUB_TOKEN"],
    # Not a real credential: a visible stand-in so Test 2 can show exactly what gets swapped
    "demo": f"demo-REAL-value-{secrets.token_hex(4)}",
}
for name, arn in t["secret_arns"].items():
    sm.put_secret_value(SecretId=arn, SecretString=values[name])
    print(f"{name:10} -> {arn}  ({len(values[name])} chars)")
print("demo secret value (fake, shown on purpose):", values["demo"])

banner("iron-proxy on Fargate behind the NLB (proxy VPC)")
print(f"rollout complete and targets healthy after {wait_proxy_ready(time.perf_counter()):.0f}s")
svc = ecs.describe_services(cluster=t["ecs_cluster"], services=[t["ecs_service"]])["services"][0]
print(f"service: desired {svc['desiredCount']} running {svc['runningCount']} | task def {svc['taskDefinition'].split('/')[-1]}")
th = target_health()
for tg, h in th.items():
    print(f"target group {tg:9} {h}")
check("every NLB target group has a healthy iron-proxy target", th and all(any(s == "healthy" for _, s in v) for v in th.values()))
print("NLB static IPs:", t["nlb_ips"], "| strict:", t["strict"])

banner("Sandbox VPC: no internet gateway, no NAT, DNS rewritten to the proxy")
rt = ec2.describe_route_tables(RouteTableIds=[t["sandbox_route_table_id"]])["RouteTables"][0]
routes = [(r.get("DestinationCidrBlock"), r.get("VpcPeeringConnectionId") or r.get("GatewayId") or "?") for r in rt["Routes"]]
print("routes:", ", ".join(f"{d} -> {g}" for d, g in routes))
check("sandbox routes: local + the two NLB /32s", {d for d, _ in routes} == {t["sandbox_vpc_cidr"]} | {f"{ip}/32" for ip in t["nlb_ips"]})
a = firewall_association() or {}
print(f"DNS Firewall association: {a.get('Status')}, mutation protection {a.get('MutationProtection')}; "
      f"every name -> CNAME {t['proxy_name'].rstrip('.')} -> {', '.join(t['nlb_ips'])}")
check("DNS Firewall associated with the sandbox VPC", a.get("Status") == "COMPLETE", a.get("Status"))

banner("Sandbox image (Claude Code, gh, git, AWS CLI, demo CA)")
b = build_image()
print(f"{b['image']['state']} in {b['seconds']}s, latest version {b['image'].get('latestActiveImageVersion')}")
check("image built", "FAIL" not in b["image"]["state"] and bool(b["image"].get("latestActiveImageVersion")), b["image"]["state"])
summary()
