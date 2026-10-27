# Sample Code

Code samples for blog posts on [my2cents.ai](https://my2cents.ai).

## Samples

### [e2b-sandbox-lifecycle](./e2b-sandbox-lifecycle)

Nine Python scripts that walk an E2B sandbox through its full lifecycle: create, commands and files, timeouts, pause/resume, auto-pause and auto-resume, list and connect, fork and snapshots, a custom template, and cleanup. Each script prints timings.

Blog post: [E2B Sandboxes: Hands-On with the Full Sandbox Lifecycle](https://my2cents.ai/deep-dive/e2b-sandboxes)

### [bedrock-agentcore-otel-pydantic](./bedrock-agentcore-otel-pydantic)

A Pydantic AI agent instrumented with OpenTelemetry, exporting traces and metrics to Amazon CloudWatch OTLP endpoints using SigV4 authentication. Uses Claude on Amazon Bedrock AgentCore with three sample tools (weather, calculator, time).

Blog post: [Pydantic AI + CloudWatch OTEL: Agent Observability on AWS](https://my2cents.ai/deep-dive/agentcore-otel-pydantic)

### [bedrock-agentcore-registry-demo](./bedrock-agentcore-registry-demo)

Shell and Python scripts that walk through the full AWS Bedrock AgentCore Agent Registry lifecycle: creating a registry, registering different resource types (MCP server, A2A agent, skill), running the approval workflow, and testing semantic search.

Blog post: [AWS Bedrock AgentCore Registry: Hands-On with Centralized Agent Governance](https://my2centsai.com/deep-dive/aws-agentcore-registry)

### [bedrock-agentcore-code-interpreter-cli](./bedrock-agentcore-code-interpreter-cli)

A Go CLI for executing code and shell commands in AWS Bedrock AgentCore Code Interpreter sessions. Supports Python, JavaScript, and TypeScript with both one-shot and session-based workflows. Cross-compiles for macOS, Linux, and Windows.

### [aws-devops-agent-demo](./aws-devops-agent-demo)

A serverless API (Lambda + API Gateway + DynamoDB) with chaos engineering scripts to demonstrate AWS DevOps Agent root cause analysis. Includes CloudFormation IaC, DynamoDB capacity starvation injection, and CloudWatch alarm integration.

Blog post: [AWS DevOps Agent Demo](https://my2cents.ai/deep-dive/aws-devops-agent)
