# Cloudflare MCP Server Portals: companion code

Companion code for the my2cents.ai deep dive on Cloudflare MCP Server Portals. Two small stateless MCP servers on
Workers, a third with 40 tools, and ten scripts that put a portal in front of them through the API and then test it:
what a client sees, what the extra hop costs, tool curation, the direct-URL bypass and its fix, policies, logs,
context size and failure modes.

A portal is one hostname (`https://<host>/mcp`) in front of several remote MCP servers. Cloudflare Access handles who may
connect, and the portal namespaces the tools as `{server_id}_{tool}`.

Video walkthrough: https://youtu.be/4iPjKWTWAro

## Requirements

- Python 3.11+, Node 20+
- A Cloudflare account on the Free plan with Zero Trust turned on, and a domain that is an active zone in that account
- An account API token with Workers Scripts, Access (Apps and Policies, Service Tokens), MCP Portals and DNS write permissions
- A workers.dev subdomain on the account

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install .
cp .env.example .env     # CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID, LAB_DOMAIN, WORKERS_SUBDOMAIN; never commit it
```

The portal is created at `mcp-portal-demo.<LAB_DOMAIN>`. Everything the scripts create is listed by, and removed by,
`99_cleanup.py`. Service-token secrets are kept in `.state.json` (gitignored) while the scripts run.

## Scripts

Run them in order; each one prints what it does and writes its numbers to `results/`.

| Script | What it shows |
|---|---|
| `01_deploy_workers.sh` | Deploys `notes-mcp` and `tickets-mcp` to workers.dev |
| `02_create_portal.py` | Servers, service tokens, policy, portal, DNS record and Access apps through the API, with timings |
| `03_unauth_probe.py` | The 401 and its OAuth discovery metadata, a blocked service token, an allowed one |
| `04_tools_and_latency.py` | Tool names through the portal; 30 calls direct vs through the portal |
| `05_curate.py` | Hide a tool, rename one, pick up a new upstream tool on sync, and how long each takes to show |
| `06_bypass.py` | Call the Worker's own URL around the portal, then close that door with a self-hosted Access app and a bearer server |
| `07_policy.py` | Two service tokens, one portal, different views of the servers |
| `08_logs.py` | The portal log, the analytics counts and the Access log |
| `09_context.py` | Adds `catalog-mcp` (40 tools) and measures the tool list with `optimize_context` and `codemode` |
| `10_failures.py` | A 65 second tool against the 60 second timeout; a server whose upstream is gone |
| `99_cleanup.py` | Deletes everything above and lists what is left. Keeps the account, token and domain |

## What my runs showed

Run on 2026-11-15 against a Free account, from a home connection in Europe. The numbers vary from run to run.

| Question | Result |
|---|---|
| Time from the API calls to a portal that answers | about 65 s; the name did not resolve for the first seconds, then the edge answered 403 (error 1014), then 401 |
| Extra latency of the hop (30 calls, `get_note`) | median 14 ms direct, 105 ms through the portal |
| Time for a curation change to reach a new session | 1 to 16 s over my runs |
| Hidden tool, called directly on the Worker's URL | works, with no credentials |
| After the fix (Access app on a custom domain, bearer server) | workers.dev URL 404, custom domain without a token 403, through the portal 200 |
| Tool list size, 49 tools | 13,306 characters (about 3,300 tokens); `search_and_execute` 2,058 (about 510); `codemode` 4,784 |
| A 65 second tool call | the portal gives up at 60 s with error `-32001` |

`results/` holds the JSON files from these runs.

## Notes

- Servers registered with `auth_type` `bearer` default to per-user authorization in the portal mapping. A service-token
  session never sees them unless the mapping says `"on_behalf": false`. The scripts set it everywhere.
- `PUT` on a server cannot change its hostname or `auth_type`; `06_bypass.py` deletes and registers the server again.
- `common.py` falls back to DNS over HTTPS for the lab domain, because a resolver that looked the name up before its record
  existed can keep answering NXDOMAIN for half an hour.
