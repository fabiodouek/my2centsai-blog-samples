# Azure AI Gateway

Companion code for the my2cents.ai deep dive on [Azure AI Gateway](https://techcommunity.microsoft.com/blog/integrationsonazureblog/azure-ai-gateway-is-now-generally-available-ship-faster-stay-in-control/4572644) (GA 2026-11-17).

One gateway in front of an Azure OpenAI deployment, then the behaviour measured one piece at a time: alias
retargeting, failover, token limit, cost limit, Entra ID against a key, and MCP tool blocking. Every script
prints what it saw; the numbers in the post are from running these in order.

```text
python client (requests)
   │  Authorization: Bearer <gateway key or Entra token>, model = "chat"
   ▼
Azure AI Gateway  (Microsoft.ApiManagement/aigateways, East US 2)
   ├─ alias "chat" ─► deployment main   (Azure OpenAI, gpt-4.1-mini, managed identity)
   │               ├► deployment alt    (retarget test)
   │               └► deployment capped (capacity 1: answers 429, failover test)
   ├─ policies: tokenLimit, costLimit
   └─ tool server "learn" ─► https://learn.microsoft.com/api/mcp
```

## Requirements

- An Azure subscription where you are Contributor (or Owner, for the role assignment in script 07), with the
  `Microsoft.ApiManagement` and `Microsoft.CognitiveServices` resource providers registered
- Azure CLI 2.75+, logged in (`az login`)
- Python 3.11+: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, then put `.venv/bin` first on PATH
- Quota for `gpt-4.1-mini` (GlobalStandard, capacity 50 plus 50 plus 1) in the Foundry region

## Scripts

Run them in order from this folder. Settings are in `scripts/env.sh` (resource group `rg-aigw-demo`, gateway in
`eastus2`, Azure OpenAI account in `swedencentral`); random resource names are kept in `.state/names`.

| Script | What it does |
|---|---|
| `00_install_cli.sh` | Installs the `az aigateway` extension (1.0.0b34 when this was written) from the Azure/aigateway release |
| `01_create_gateway.sh` | Resource group, then the gateway with `--no-wait` and `wait`, and prints the create time (95 s in my run) |
| `02_foundry_backend.sh` | Azure OpenAI account with two deployments (`main`, `alt`), imported as one provider, alias `chat`, a gateway key saved to `.state/key` (git-ignored, never printed) |
| `03_first_call.py` | One chat call through the alias; prints status, latency, serving deployment, usage and headers |
| `04_retarget.py` | Retargets `chat` from `main` to `alt` while 25 calls run; counts failures and time to apply |
| `05_failover.py` | Adds the `capped` deployment (capacity 1). Four runs of calls: A) priority 0 `capped` answers 429, no failure rules; B) priority 0 is a host that does not exist, no rules; C) A again with a 429 failure rule; D) B again with the three rules the product docs recommend. The CLI has no options for failure rules, so they go in with `az rest` (see `common.alias_rules`) |
| `06_limits.py` | Token limit (300 a minute) then cost limit ($0.001 an hour), each with a burst; prints per-call tokens, the gateway's cost header and a list-price check |
| `07_entra_auth.py` | Calls with an Entra token: a management token (401), a gateway token with no role (403), then assigns AI Gateway User and polls until it works |
| `08_mcp.py` | Registers the public Microsoft Learn MCP server, lists its tools, blocks one, calls it anyway |
| `99_teardown.sh` | Deletes the gateway, the resource group, and purges the Azure OpenAI account |

## Notes

- `az aigateway` is beta (1.0.0b34). Flags can change. `az aigateway model list` without `--provider-name`
  hung for over two minutes in my exploration, so the scripts always pass a provider name.
- The gateway is in East US 2 and the model account in Sweden Central only because model deployments in my new
  subscription were refused with `(715-123420) unusual activity` for about an hour, first in East US 2. It was
  not planned, and the region was not the cause. Better: put both in one region. Sweden Central is one of the
  gateway's regions, so `LOCATION=swedencentral FOUNDRY_LOCATION=swedencentral` is a valid setup.
- Not covered: telemetry export, the Anthropic Messages API path, Model Router, private networking.

## Cost

The gateway is free up to 10,000 requests a month, and paid billing starts on 2027-01-01. A full run is a few
hundred requests and a few cents of `gpt-4.1-mini` tokens. The gateway exists for about two minutes of setup
plus the tests, then script 99 removes everything.
