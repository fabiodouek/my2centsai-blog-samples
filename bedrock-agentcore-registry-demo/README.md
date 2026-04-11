# Bedrock AgentCore Registry Demo

Companion scripts for the [AWS Bedrock AgentCore Registry: First Impressions from the Preview](https://my2centsai.com/deep-dive/aws-agentcore-registry) blog post on my2cents.ai.

These scripts walk through the full Agent Registry lifecycle: creating a registry, registering different resource types (MCP server, A2A agent, skill), running the approval workflow, and testing semantic search.

## Prerequisites

- AWS CLI v2.34.29+ with `bedrock-agentcore-control` and `bedrock-agentcore` service support
- An AWS account in a supported region (us-east-1, us-west-2, ap-northeast-1, ap-southeast-2, eu-west-1)
- IAM permissions for `bedrock-agentcore-control:*` and `bedrock-agentcore:SearchRegistryRecords`
- `jq` installed for JSON parsing
- Python 3.10+ and `boto3` (for the Python SDK example)

## Quick Start

```bash
# 1. Create a registry (JWT auth with Cognito — requires a Cognito User Pool ID)
./scripts/create-registry.sh my2cents-demo <cognito-user-pool-id> us-east-1

# Or use IAM auth for a simpler setup
./scripts/create-registry.sh my2cents-demo IAM us-east-1

# 2. Register sample resources (MCP server, A2A agent, skill)
./scripts/register-resources.sh <registry-id> us-east-1

# 2b. Register via URL-based auto-sync (optional)
./scripts/register-url-sync.sh <registry-id> us-east-1

# 3. Run the approval workflow
./scripts/approval-workflow.sh <registry-id> us-east-1

# 4. Test semantic search (bash)
./scripts/discovery.sh <registry-id> us-east-1

# 4b. Test semantic search (Python SDK)
python scripts/search_registry.py <registry-id> us-east-1

# 5. Clean up
./scripts/cleanup.sh <registry-id> us-east-1
```

## Scripts

| Script | Description |
|--------|-------------|
| `scripts/create-registry.sh` | Creates an Agent Registry with JWT (Cognito) or IAM auth and manual approval |
| `scripts/register-resources.sh` | Registers three resource types: MCP server, A2A agent, and skill |
| `scripts/approval-workflow.sh` | Submits records for approval and approves them |
| `scripts/discovery.sh` | Tests keyword, semantic, and filtered search |
| `scripts/search_registry.py` | Runs the same 5 search tests using the Python SDK (boto3) |
| `scripts/register-url-sync.sh` | Registers an MCP server via URL-based auto-sync |
| `scripts/cleanup.sh` | Deletes all records and the registry |

## What You'll Learn

- How to create and configure an Agent Registry via CLI
- How to register MCP servers, A2A agents, and skills with proper schemas
- The draft-to-approved lifecycle with approval workflows
- Semantic search vs. keyword search behavior
- Search filters for scoping results by resource type

## Cost

Agent Registry is **free during the preview period**. No charges for creating registries, records, or running searches.

## Regional Availability

The Agent Registry preview is available in 5 AWS regions:

- US East (N. Virginia) - `us-east-1`
- US West (Oregon) - `us-west-2`
- Asia Pacific (Tokyo) - `ap-northeast-1`
- Asia Pacific (Sydney) - `ap-southeast-2`
- Europe (Ireland) - `eu-west-1`
