# Sample Code

Code samples for blog posts on [my2cents.ai](https://my2cents.ai).

## Samples

### [lambda-microvm-workspace](./lambda-microvm-workspace)

A deployed browser workspace on AWS Lambda MicroVMs: sign in with Cognito, create a sandbox and get a terminal with Claude Code ready to run. Every sandbox reaches the internet only through part 2's iron-proxy, in transparent mode. Panels show the proxy feed, credential use with a Revoke button, Claude Code's tokens and cost, vitals and DNS lookups. Admins change the shared proxy policy from the browser: the allowlist, injected credentials, and which of four IAM roles (plus an optional session policy) signs sandbox AWS calls. Terraform for CloudFront, Cognito, the ALB, the API on ECS Fargate and DynamoDB, a FastAPI backend and a React frontend.

Blog post: [Lambda MicroVM: Hands-On Exploration of Self-Service Sandboxes](https://my2cents.ai/deep-dive/lambda-microvms-workspace)

### [lambda-microvm-iron-proxy](./lambda-microvm-iron-proxy)

Egress control and credential injection for AWS Lambda MicroVMs with iron-proxy on ECS Fargate, behind an internal NLB. Two VPCs: the proxy VPC (NAT, iron-proxy) and a sandbox VPC with no internet route, joined by peering, where Route 53 DNS Firewall answers every name with the proxy. Sandboxes have no proxy settings; iron-proxy swaps placeholder Anthropic and GitHub tokens for real ones from Secrets Manager and re-signs AWS SigV4 requests. Terraform plus Python test scripts, and an optional control plane (config in S3, a sidecar that reloads iron-proxy, four sandbox roles) used by the workspace.

Blog post: [Lambda MicroVM: Hands-On Egress Credential Injection with iron-proxy](https://my2cents.ai/deep-dive/lambda-microvms-iron-proxy)

### [lambda-microvm-lifecycle](./lambda-microvm-lifecycle)

Eleven Python scripts that walk AWS Lambda MicroVMs through the sandbox lifecycle: building an image, running MicroVMs and their defaults, the endpoint, tokens, files, streaming and the shell, suspend and resume, idle auto-suspend, the maximum duration, list and reconnect, many MicroVMs from one snapshot, egress and identity, the quota and the bill, and cleanup. Each script prints timings.

Blog post: [AWS Lambda MicroVMs: Hands-On with the Full Sandbox Lifecycle](https://my2cents.ai/deep-dive/lambda-microvms)

### [google-ax-stress](./google-ax-stress)

Stress tests for Google AX on the two-node EKS cluster from `google-ax-eks`: density ceiling, create/resume/suspend latency under concurrency, wake-on-request bursts, control-plane throughput, noisy neighbors and a snapshot storm, with CloudWatch Container Insights and kubelet metrics, and the scripts that draw the post's charts.

Blog post: [Google AX on Amazon EKS: Stress Testing Two Nodes](https://my2cents.ai/deep-dive/google-ax-stress)

### [google-ax-lifecycle](./google-ax-lifecycle)

Nine Python scripts that put Google AX tasks through their lifecycle on the EKS cluster from `google-ax-eks`: create, first resume, suspend/resume cycles, wake on request through Substrate's router, when snapshots reach S3, a 2 GB workspace, density on two workers, and failures. Each script prints timings.

Blog post: [Google AX Task Lifecycle: Hands-On on Amazon EKS](https://my2cents.ai/deep-dive/google-ax-lifecycle)

### [google-ax-credentials](./google-ax-credentials)

Claude Code in a Google AX task that never holds the Anthropic key or the GitHub token: Kubernetes Secrets plus Substrate's egress gateway, which swaps placeholder headers for the real values. Eight Python scripts cover the mechanics, Claude Code, git and gh, an end-to-end pull request, leak attempts, rotation and cleanup.

Blog post: [Google AX Credential Injection: Hands-On with Claude Code and GitHub](https://my2cents.ai/deep-dive/google-ax-credentials)

### [google-ax-eks](./google-ax-eks)

Google AX (Agent Executor) and Agent Substrate on Amazon EKS 1.37: Terraform for the cluster, S3 snapshot bucket, Pod Identity roles and an ECR create-on-push template, plus shell scripts that build Substrate and AX from source, install them with an EKS kustomize overlay and an 11-line `ate-setup` patch, create the gVisor WorkerPool and run AX's smoke test.

Blog post: [Google AX on Amazon EKS: Hands-On with a Self-Hosted Install](https://my2cents.ai/deep-dive/google-ax-eks)

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
