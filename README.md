# Sample Code

Code samples for blog posts on [my2cents.ai](https://my2cents.ai).

## Samples

### [daytona-sandbox-credentials](./daytona-sandbox-credentials)

Claude Code in a Daytona sandbox that holds only placeholders for its credentials: per-user Daytona Secrets that are substituted in HTTPS headers at egress. Python scripts set up the secrets, test the substitution, run Claude Code on the Anthropic API and on Bedrock, clone and push with git and gh, attempt to leak the credentials, rotate them live, give a second user a GitHub App user token through the OAuth device flow, sign S3 requests, and clean up.

Blog post: [Daytona Sandbox Secrets: Hands-On with Claude Code, Bedrock and GitHub](https://my2cents.ai/deep-dive/daytona-sandbox-credentials)

### [daytona-sandbox-lifecycle](./daytona-sandbox-lifecycle)

Nine Python scripts that walk a Daytona sandbox through its whole lifecycle: create, commands and files, lifecycle timers, stop/start and archive, pause and fork on a Linux VM, list and reconnect, snapshots, a custom snapshot, and cleanup. Each script prints timings.

Blog post: [Daytona Sandboxes: Hands-On with the Full Sandbox Lifecycle](https://my2cents.ai/deep-dive/daytona-sandboxes)

### [langsmith-sandbox-credentials](./langsmith-sandbox-credentials)

Claude Code in a LangSmith sandbox that never holds the Anthropic key or the GitHub token: workspace secrets injected by the sandbox auth proxy. Python scripts set up the secrets and proxy rules, test the injection mechanics, run Claude Code, clone and push with git and gh, open a PR end to end, attempt to leak the credentials, rotate them live, try AWS through an IAM role, and clean up.

Blog post: [LangSmith Sandbox Credentials: Hands-On with Claude Code, GitHub and the Auth Proxy](https://my2cents.ai/deep-dive/langsmith-sandbox-credentials)

### [langsmith-sandbox-lifecycle](./langsmith-sandbox-lifecycle)

Ten Python scripts that walk a LangSmith sandbox through its whole lifecycle: create, commands and files, lifetime settings and resizing, stop/start with and without memory, idle stop and auto-wake, list and reconnect, snapshots and forks, a custom snapshot, the quota and the bill, and cleanup. Each script prints timings.

Blog post: [LangSmith Sandboxes: Hands-On with the Full Sandbox Lifecycle](https://my2cents.ai/deep-dive/langsmith-sandboxes)

### [e2b-sandbox-credentials](./e2b-sandbox-credentials)

Claude Code in an E2B sandbox that never holds the Anthropic key or the GitHub token: E2B Secrets plus per-host egress header injection. Eight Python scripts set up the secrets, test the injection mechanics, run Claude Code, clone and push with git and gh, open a PR end to end, attempt to leak the credentials, rotate them live, and clean up.

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
