"""Delete everything these scripts created. Safe to run twice. Leaves the account, the API token and the domain alone."""
import subprocess

from common import *

st = load_state()


def try_(label, method, path, body=None):
    status, data = cf(method, path, body)
    print(f"{label:38} HTTP {status}")
    return status, data


try_("portal", "DELETE", f"access/ai-controls/mcp/portals/{PORTAL_ID}")
for sid in ("notes", "tickets", "catalog", "broken"):
    try_(f"server {sid}", "DELETE", f"access/ai-controls/mcp/servers/{sid}")
for name, app_id in (st.get("apps") or {}).items():
    try_(f"access app {name}", "DELETE", f"access/apps/{app_id}")
for key in ("policy_agent", "policy_both", "policy_origin"):
    if st.get(key):
        try_(f"policy {key}", "DELETE", f"access/policies/{st[key]}")
for name, tok in (st.get("service_tokens") or {}).items():
    if tok:
        try_(f"service token {name}", "DELETE", f"access/service_tokens/{tok['id']}")

zone = cf_ok("GET", f"{API}/zones", params={"name": DOMAIN})[0]["id"]
for dom in cf_ok("GET", "workers/domains"):
    if dom["hostname"] == f"tickets.{DOMAIN}":
        try_("worker custom domain", "DELETE", f"workers/domains/{dom['id']}")
for rec in cf_ok("GET", f"{API}/zones/{zone}/dns_records", params={"per_page": 100}):
    if rec["name"] in (PORTAL_HOST, f"tickets.{DOMAIN}"):
        try_(f"dns {rec['type']} {rec['name'].split('.')[0]}", "DELETE", f"{API}/zones/{zone}/dns_records/{rec['id']}")
for w in ("notes-mcp", "tickets-mcp", "catalog-mcp"):
    try_(f"worker {w}", "DELETE", f"workers/scripts/{w}?force=true")

STATE.unlink(missing_ok=True)

print("\nWhat is left in the account:")
checks = {
    "portals": cf_ok("GET", "access/ai-controls/mcp/portals"),
    "servers": cf_ok("GET", "access/ai-controls/mcp/servers"),
    "access apps": cf_ok("GET", "access/apps"),
    "access policies": cf_ok("GET", "access/policies"),
    "service tokens": cf_ok("GET", "access/service_tokens"),
    "workers": cf_ok("GET", "workers/scripts"),
    "dns records": [r for r in cf_ok("GET", f"{API}/zones/{zone}/dns_records", params={"per_page": 100})],
}
for k, v in checks.items():
    print(f"  {k:16} {len(v or [])}")
