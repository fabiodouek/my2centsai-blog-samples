# AWS DevOps Agent Demo: Serverless Chaos Engineering

A simple serverless API (Lambda + API Gateway + DynamoDB) with a chaos engineering script to demonstrate [AWS DevOps Agent](https://aws.amazon.com/devops-agent/) root cause analysis.

Companion code for [this blog post](https://my2cents.ai/deep-dive/aws-devops-agent).

## Architecture

```
Client -> API Gateway -> Lambda -> DynamoDB
                                      |
CloudWatch Alarms <-- CloudWatch Metrics
       |
AWS DevOps Agent (investigates)
```

## Repository Structure

```
cloudformation/stack.yaml    - CloudFormation template (all AWS resources)
scripts/deploy.sh            - Deploy the stack
scripts/test-api.sh          - Smoke test the API
scripts/cleanup.sh           - Delete the stack
chaos/capacity-starvation.sh - Inject DynamoDB throttling
chaos/restore.sh             - Restore DynamoDB to on-demand
app/handler.py               - Lambda function source (reference copy)
```

> **Note:** The Lambda code is deployed via inline `ZipFile` in the CloudFormation template. `app/handler.py` is a reference copy for readability.

## Prerequisites

- AWS CLI v2 configured with credentials
- An AWS account in a [supported region](https://docs.aws.amazon.com/devopsagent/latest/userguide/about-aws-devops-agent-supported-regions.html) (us-east-1 recommended)
- AWS DevOps Agent enabled with an Agent Space (see the blog post for setup steps)
- Python 3 (used by the test script for JSON formatting)
- curl

## Quick Start

### 1. Clone and deploy

```bash
git clone https://github.com/fabiodouek/my2centsai-blog-samples.git
cd my2centsai-blog-samples/aws-devops-agent-demo

chmod +x scripts/*.sh chaos/*.sh
./scripts/deploy.sh devops-agent-demo us-east-1
```

This creates: a DynamoDB table, a Lambda function, an API Gateway endpoint, a CloudWatch dashboard, and three CloudWatch alarms. All resources are tagged with `devopsagent=true` for Agent discovery.

Deployment takes about 2 minutes.

### 2. Test the API

```bash
# Get the API URL from the deploy output, then:
./scripts/test-api.sh https://YOUR-API-ID.execute-api.us-east-1.amazonaws.com/prod
```

### 3. Run the chaos script

```bash
./chaos/capacity-starvation.sh \
  https://YOUR-API-ID.execute-api.us-east-1.amazonaws.com/prod
```

This switches DynamoDB from on-demand to provisioned (1 RCU/1 WCU) and blasts 1,000 requests to trigger throttling.

> **Note:** The chaos scripts hardcode the table name `devops-agent-demo-items`. If you deploy with a different stack name, update the table name in `chaos/capacity-starvation.sh` and `chaos/restore.sh`.

### 4. Investigate with DevOps Agent

Once CloudWatch alarms fire, start a DevOps Agent investigation from the console or CLI. See the blog post for the full walkthrough.

### 5. Restore and clean up

```bash
# Restore DynamoDB to on-demand
./chaos/restore.sh

# Delete all resources
./scripts/cleanup.sh devops-agent-demo us-east-1
```

## What the chaos script does

1. Records the current DynamoDB billing mode
2. Switches the table to PROVISIONED with 1 RCU / 1 WCU (minimal capacity)
3. Fires concurrent POST requests to the API
4. DynamoDB throws `ProvisionedThroughputExceededException`
5. Lambda errors cascade, API Gateway returns 5xx
6. CloudWatch alarms fire

DevOps Agent should trace: API Gateway 5xx errors -> Lambda errors -> DynamoDB throttling -> recent table configuration change.

## Cost

- The CloudFormation stack costs near-zero when idle (DynamoDB on-demand, Lambda pay-per-invoke)
- The chaos script generates a small number of Lambda invocations and DynamoDB requests
- DevOps Agent charges [$0.0083/agent-second](https://aws.amazon.com/devops-agent/pricing/) during investigation
- Remember to run `cleanup.sh` when done
