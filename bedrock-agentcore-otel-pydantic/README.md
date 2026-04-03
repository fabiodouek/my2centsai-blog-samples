# Pydantic AI Agent with CloudWatch OpenTelemetry Observability

A sample Pydantic AI agent instrumented with OpenTelemetry, exporting traces and metrics directly to Amazon CloudWatch OTLP endpoints. Uses `requests-auth-aws-sigv4` for SigV4 authentication with no dependency on ADOT or the `opentelemetry-instrument` wrapper.

Built to accompany the blog post: [Pydantic AI + CloudWatch OTEL: Agent Observability on AWS](https://my2centsai.com/deep-dive/agentcore-otel-pydantic)

## What it does

A simple AI agent powered by Claude (via Amazon Bedrock) with three tools:

- **get_weather** - Returns weather data for a city
- **calculate** - Evaluates math expressions
- **get_current_time** - Returns current time in a timezone

Every agent run, model request, and tool call is traced with OpenTelemetry `gen_ai.*` semantic conventions and exported to CloudWatch.

## Prerequisites

- Python 3.10+
- AWS account with Bedrock model access (Claude Sonnet)
- AWS credentials configured (`aws configure` or environment variables)
- CloudWatch Transaction Search enabled (one-time setup, see below)

## One-time AWS setup

### Enable CloudWatch Transaction Search

This allows CloudWatch to receive and index OpenTelemetry traces.

**Option A: AWS CLI**

```bash
# Create resource policy for X-Ray access
aws logs put-resource-policy \
  --policy-name TransactionSearchPolicy \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Sid": "TransactionSearchXRayAccess",
      "Effect": "Allow",
      "Principal": {"Service": "xray.amazonaws.com"},
      "Action": "logs:PutLogEvents",
      "Resource": [
        "arn:aws:logs:*:*:log-group:aws/spans:*",
        "arn:aws:logs:*:*:log-group:/aws/application-signals/data:*"
      ]
    }]
  }'

# Route traces to CloudWatch Logs
aws xray update-trace-segment-destination --destination CloudWatchLogs

# Optional: increase sampling (1% is free)
aws xray update-indexing-rule --name "Default" \
  --rule '{"Probabilistic": {"DesiredSamplingPercentage": 100}}'
```

**Option B: Console**

1. Open CloudWatch console
2. Go to **Settings > X-Ray traces > Transaction Search**
3. Enable Transaction Search, set sampling percentage

## Installation

```bash
cd sample-code/agentcore-otel-pydantic

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

## Configuration

```bash
cp .env.example .env
# Edit .env with your AWS credentials and region
```

Or export variables directly:

```bash
export AWS_ACCESS_KEY_ID=your-key
export AWS_SECRET_ACCESS_KEY=your-secret
export AWS_DEFAULT_REGION=us-east-1
```

## Running

```bash
# Source your .env file
set -a && source .env && set +a

# Run directly (no wrapper needed)
python main.py
```

You should see agent output in the terminal and traces flowing to CloudWatch within a few minutes.

## Viewing traces in CloudWatch

1. Open the [CloudWatch console](https://console.aws.amazon.com/cloudwatch/)
2. Navigate to **X-Ray traces > Transaction Search**
3. Look in the `aws/spans` log group
4. Filter by `service.name = pydantic-ai-agent-demo`
5. Click a trace to see the span hierarchy:
   - **Agent run** (top-level span)
   - **Model request** (child span per LLM call, with `gen_ai.request.model`, token counts)
   - **Tool call** (child span per tool, with `gen_ai.tool.name`)

For the GenAI Observability dashboard (requires AgentCore Runtime deployment):
- Go to **CloudWatch > GenAI Observability > Bedrock AgentCore**

## Architecture

```
main.py
  │
  ├── setup_otel()
  │     ├── TracerProvider + OTLPSpanExporter
  │     │     └── requests.Session with AWSSigV4("xray")
  │     └── MeterProvider + OTLPMetricExporter
  │           └── requests.Session with AWSSigV4("monitoring")
  │
  ├── Agent.instrument_all()     ← Emits gen_ai.* OTEL spans
  │
  ├── BedrockConverseModel       ← Claude via Bedrock Converse API
  │
  └── Tools (weather, calc, time)
        │
        ▼
CloudWatch OTLP endpoints       ← Traces, metrics
  ├── xray.{region}.amazonaws.com/v1/traces
  └── monitoring.{region}.amazonaws.com/v1/metrics
```

## How SigV4 auth works

CloudWatch OTLP endpoints require AWS SigV4 authentication. Instead of using ADOT (which brings a large dependency tree and the `opentelemetry-instrument` wrapper), this sample uses [`requests-auth-aws-sigv4`](https://pypi.org/project/requests-auth-aws-sigv4/). It attaches SigV4 signing to a `requests.Session`, which is then passed to the standard `OTLPSpanExporter` and `OTLPMetricExporter` via their `session=` parameter:

```python
import requests
from requests_auth_aws_sigv4 import AWSSigV4

session = requests.Session()
session.auth = AWSSigV4("xray", region="us-east-1")

exporter = OTLPSpanExporter(
    endpoint="https://xray.us-east-1.amazonaws.com/v1/traces",
    session=session,
)
```

The SigV4 service names are `xray` for traces and `monitoring` for metrics.

## Notes

- CloudWatch native OTLP metrics support is in public preview (April 2026), free during preview. Available in us-east-1, us-west-2, ap-southeast-2, ap-southeast-1, eu-west-1.
- OpenTelemetry GenAI semantic conventions are experimental. Span attributes may change in future releases.
