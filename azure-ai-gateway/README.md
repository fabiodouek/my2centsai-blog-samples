# Azure AI Gateway

Companion code for the my2cents.ai deep dive on Azure AI Gateway (GA 2026-11-17).

> **Status: first draft, not yet run.** The scripts were written from the Azure/aigateway CLI reference
> and labs. Flags and response shapes are checked against `--help` and the real service in the
> hands-on run; expect edits.

One gateway in front of one Foundry model, then the policies measured one at a time: alias, failover,
token limit, cost limit, Entra ID against a key.

```text
python client (requests)
   │  Authorization: Bearer <gateway key or Entra token>, model = "chat"
   ▼
Azure AI Gateway  (Microsoft.ApiManagement/aigateways, eastus2)
   ├─ alias "chat" ─► priority 0: dead custom endpoint (failover test)
   │               └► priority 1: Foundry gpt-5-mini (managed identity)
   └─ policies: tokenLimit, costLimit
```

## Requirements

- An Azure subscription, Contributor on it, and the `Microsoft.ApiManagement` and
  `Microsoft.CognitiveServices` providers registered
- Azure CLI 2.75+ (`az login` done) and the `az aigateway` extension (`scripts/00_install_cli.sh`)
- Python 3.11+ (`pip install -r requirements.txt`)
- Quota for `gpt-5-mini` (GlobalStandard, capacity 10) in your region

## Scripts

| Script | What it does |
|---|---|
| `00_install_cli.sh` | Installs the `az aigateway` extension from the Azure/aigateway release wheel and prints its version |
| `01_create_gateway.sh` | Resource group, gateway, waits until created, prints the create time |
| `02_foundry_backend.sh` | Foundry account and `gpt-5-mini` deployment, imports it as a provider, creates alias `chat` and a gateway key |
| `03_first_call.py` | One call through the alias with the gateway key; prints the answer, usage and response headers |
| `04_failover.sh` | Adds a dead custom provider at priority 0 under the same alias and runs 20 calls, reporting status and latency |
| `05_limits.py` | Applies a token limit then a cost limit and sends a burst; shows the 429 and 403 and which call crossed the line |
| `06_entra_auth.py` | Calls with an Entra token (`DefaultAzureCredential`) and shows the 401 and 403 cases |
| `99_teardown.sh` | Deletes the gateway, waits, deletes the resource group and purges soft-deleted records |

Settings live in `scripts/env.sh` (names, region, model). Run the scripts in order.

## Cost

The gateway is free up to 10,000 requests a month and paid billing starts on 2027-01-01. The runs
use a few hundred calls with small prompts on `gpt-5-mini`, so the model cost is cents.
