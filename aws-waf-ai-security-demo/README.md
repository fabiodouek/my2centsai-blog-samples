# AWS WAF AI Security demo

Companion code for the my2cents.ai deep dive [AWS WAF AI Security: Hands-On with Shadow MCP Servers](https://www.my2cents.ai/deep-dive/aws-waf-ai-security/).

It deploys a small, deliberately sloppy estate of AI endpoints (an MCP server, an A2A agent and an OpenAI-style chat endpoint), lets API Gateway's **API Portal** discover them, puts the **AWS WAF AI Security** managed rule group in front, and then measures what the rule group catches, what it misses, what it adds in latency and what it costs.

All data is fake. There is no model behind the chat endpoint: its replies are canned so every run is repeatable.

## What gets created

| Resource | Name | Notes |
|---|---|---|
| 3 Lambda functions | `waf-ai-demo-mcp`, `-a2a`, `-llm` | Python 3.13, code in `lambdas/` |
| API Gateway REST API | `waf-ai-demo-estate` | Public, stage `prod` |
| Application Load Balancer | `waf-ai-demo-alb` | HTTP on port 80, open to your IP only |
| 2 AWS WAF web ACLs | `waf-ai-demo-apigw`, `waf-ai-demo-alb` | One rule: `AWSManagedRulesAISecurityRuleSet` |
| IAM role | `waf-ai-demo-waf-bedrock` | Trusts `wafv2.amazonaws.com`, allows `bedrock:InvokeGuardrailChecks` |
| CloudWatch log group | `aws-waf-logs-waf-ai-demo` | WAF logs, 7 days |
| API Portal discovery configuration | one per account and Region | API Gateway and ALB sources, AI discovery on |

Everything is tagged `project=waf-ai-security`.

**Cost.** A run of a few hours costs a few dollars: the ALB and the web ACLs are billed by the hour, and each checked message costs a fraction of a cent in Amazon Bedrock (`10_cost.py` prints the numbers). The one item to watch is API Portal discovery: it has a monthly minimum per Region ($40 in us-east-1 when I ran this), prorated daily, so run `teardown.py` when you are done.

## Requirements

- An AWS account and credentials that can create the resources above (I used an administrator profile in a sandbox account). Region `us-east-1` by default; set `AWS_REGION` to one of the Regions where AI Security is available.
- Python 3.12 or newer and a recent `boto3` (1.44.8 or newer: it has the `apiportal` client and the AI Security rule group configuration).
- A default VPC in the Region (for the ALB).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Run it

Run the scripts in order. Each one prints what it did.

| Script | What it does |
|---|---|
| `01_deploy.py` | Lambdas, REST API, ALB. No WAF yet. `--code-only` re-uploads the Lambda code. |
| `02_baseline.py` | Sends the request matrix (`cases.py`) to both front doors. Everything returns 200. |
| `03_discovery.py enable` | Turns on API Portal discovery with AI discovery, and starts it. |
| `03_discovery.py watch` | Sends client traffic and prints the inventory every 10 minutes until the endpoints carry MCP, A2A and LLM labels. `status` prints it once. |
| `04_protect.py --mode count` | Creates the role, the log group and the web ACLs, with the four blocking rules overridden to Count. |
| `05_attacks.py` | Sends the matrix through WAF and reads the labels from the WAF logs. |
| `04_protect.py --mode enforce` | The rule group's default actions. Run `05_attacks.py --show-block` again. |
| `06_responses.py` | Response inspection: sends the four response cases and reads the CloudWatch label metrics. |
| `07_failure_mode.py` | Breaks the IAM role and shows fail-open, then fail-closed with an override, then restores it. |
| `08_latency.py` | Latency with no WAF, WAF with Bedrock off, request checks, request and response checks. |
| `09_detection.py` | 40 benign and 40 hostile prompts (`corpus.py`), each sent twice. `--big` tests large bodies. |
| `10_cost.py` | Calls `InvokeGuardrailChecks` directly to read the text units per message and prices 1M messages. |
| `teardown.py` | Deletes everything, including the discovery configuration, and lists what is left. |

`04_protect.py` takes more options: `--guardrails both|request|disable`, `--fail-closed`, `--no-scope-down`.

My results from 2026-11-24 are in `results/`.

## Things that tripped me up

- `put-discovery-configuration` reported `ENABLED`, but every source stayed `STOPPED` for ten minutes until I called `start-discovery` with the configuration ARN. `03_discovery.py enable` does this for you.
- The rule group's `ManagedRuleGroupConfigs` block is mandatory. A web ACL that names the rule group without it is rejected.
- A new web ACL cannot be associated right away. The script retries for a few minutes.
- WAF logs arrive in CloudWatch Logs with a delay of up to a couple of minutes, and response-phase labels are never in them. `06_responses.py` reads CloudWatch metrics instead.
- A request to `/mcp` with no `MCP-Protocol-Version` header is "not recognized" and is not inspected at all. Your MCP server has to reject those itself.
- In API Gateway the path that WAF sees includes the stage (`/prod/mcp`), so the scope-down statement matches with `CONTAINS`.

## Clean up

```bash
.venv/bin/python teardown.py
```
