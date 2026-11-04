# Lambda MicroVM Workspace

A deployed browser workspace on AWS Lambda MicroVMs. Sign in, create a sandbox, and get a terminal with Claude Code ready to run. Every sandbox reaches the internet only through part 2's iron-proxy, without knowing it, and what it does there stays on screen:

- every request, and which credential was swapped in
- credential use, with a Revoke button
- Claude Code's tokens and cost
- the MicroVM's vitals and timers
- every DNS lookup from the sandbox VPC

Admins can change the proxy's policy from the browser, for every sandbox at once: the allowlist, the credentials it injects (including new ones), and the AWS access sandbox calls are signed with: one of four roles (view-only by default), optionally narrowed with a session policy picked and searched from AWS's full Service Authorization Reference. Changes apply in about 2 s through iron-proxy's reload API, without a restart.

The terminal runs inside tmux in the MicroVM, so closing the tab, a network blip or a suspend doesn't lose the session.

Companion code for [Lambda MicroVM: Hands-On Exploration of Self-Service Sandboxes](https://my2cents.ai/deep-dive/lambda-microvms-workspace), part 3 of the AWS Lambda MicroVMs series. It builds on [part 2's companion code](../lambda-microvm-iron-proxy), which must be applied first with `control_plane=true`.

## How it fits together

Part 2 owns the network and the proxy; this repo adds the workspace around them.

- **Part 2's two VPCs.** The sandbox VPC holds only the MicroVM connector: no internet gateway, no NAT, routes only to iron-proxy's load balancer in the proxy VPC, and DNS Firewall answering every name with a CNAME to it. So sandboxes have no proxy settings at all (transparent interception): `curl`, `git`, `gh`, the AWS CLI and Claude Code connect to iron-proxy believing they talk to the real host.
- **Part 2's control plane** (`control_plane=true`). iron-proxy reads its config from an S3 bucket through a sidecar that reloads it in place, and signs sandbox AWS calls as one of four roles (`view-only`, `read-only`, `data-reader`, `builder`). The workspace edits that config and the role choice.
- **This repo:** CloudFront and S3 for the app, an ALB for `/api` and `/ws`, the API on ECS Fargate (in part 2's proxy VPC and cluster), Cognito, DynamoDB, ECR, and the sandbox image.

One iron-proxy serves every sandbox, so the policy, the proxy feed, the credential counters and the DNS tab cover every sandbox on it. iron-proxy sees requests from the load balancer and the sandboxes share the connector's addresses, so neither can tell sandboxes apart; the only per-sandbox label AWS sees is `app/ws-<sandbox name>` in the user agent.

## Layout

| Path | What |
|---|---|
| [`images/sandbox/`](images/sandbox/README.md) | The sandbox image: part 2's image plus tmux, tini, Claude Code that starts with no prompts, an agent with `/stats` and `/claude-usage`, and a `/validate` warm-up |
| [`backend/`](backend/README.md) | FastAPI on ECS Fargate: Cognito JWT auth; sandbox ownership in DynamoDB; run, suspend, resume and terminate; the terminal bridge to each MicroVM's shell WebSocket; vitals and Claude usage; credential revoke and restore; the proxy policy; the CloudWatch Logs Live Tail feed for iron-proxy's audit log and the sandbox VPC's query log. The contract is in [`backend/API.md`](backend/API.md). |
| [`frontend/`](frontend/README.md) | React, TypeScript, Vite and xterm.js: the sidebar, terminal, five panels and the Proxy policy page, with a mock mode for development |
| [`infra/`](infra/README.md) | Terraform: CloudFront with S3 for the app and an ALB for `/api` and `/ws`, the ECS service, Cognito, DynamoDB, ECR. Part 2 is read through `terraform_remote_state`. |
| `scripts/` | `deploy.sh`, `destroy.sh`, `create_user.sh`; build the sandbox image (`build_sandbox_image.py`) and smoke-test it (`smoke_sandbox.py`) |
| [`SPEC.md`](SPEC.md) | The build spec the components were written against |

## Deploy

```bash
# 1. part 2, from its directory: terraform -chdir=infra apply -var control_plane=true (and its secrets, see its README)
# 2. then, from here:
bash scripts/deploy.sh                                   # image, Terraform, backend, frontend (~10 min the first time)
bash scripts/create_user.sh --admin you@example.com      # prompts for a password; --admin may edit the proxy policy
```

`deploy.sh` checks that part 2 is the two-VPC stack and whether its control plane is on, builds the sandbox image when there is none or when it was built for another CA (a new part 2 has a new one), and prints the CloudFront URL. It's safe to re-run. Use `SKIP_BACKEND=1`, `SKIP_FRONTEND=1` or `SKIP_IMAGE=1` to skip a part. A bare `terraform apply` scales the API service to 0, so always deploy through the script. `scripts/smoke_sandbox.py` runs one MicroVM to check the image: Claude Code, suspend and resume.

## Things to know

- **No exec API.** Lambda MicroVMs have no exec API, and any endpoint or shell token is root in the sandbox. So the API mints short-lived shell tokens itself and bridges the terminal; the browser never holds a MicroVM token.
- **No proxy settings in the sandbox.** The run hook payload carries placeholders, placeholder AWS keys, the CA variables, `SANDBOX_NAME`, `AWS_SDK_UA_APP_ID=ws-<name>` and `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` (iron-proxy refuses the CLI's default chunked uploads), and nothing else.
- **Feeds aren't per sandbox.** The proxy and DNS feeds cover every sandbox on the proxy: iron-proxy sees the load balancer's addresses, and the Resolver sees the connector's.
- **The proxy policy is global.** The Proxy policy page (admins only, Cognito group `admins`) edits the allowlist, the injected credentials and the AWS access for every sandbox on the proxy. A new credential's value goes straight to Secrets Manager; sandboxes get a placeholder, and only sandboxes created after it was added get it in their environment. Secrets Manager is never reachable through iron-proxy, whatever the role.
- **Revoke takes seconds.** It denies the proxy's identities access to the secret and writes a new config version; the reload drops iron-proxy's cached copy. For a leaked key, revoke it at its source as well.
- **Watching keeps the sandbox awake.** Any request to the in-VM agent is inbound traffic, which keeps the sandbox from idle-suspending. Live stats and Claude usage are polled only while a terminal is attached.
- **Live Tail is billed per minute,** so the API runs it only while a browser is showing the Proxy or DNS panel: the side panel is open on one of those tabs and the page is visible. It stops 60 s after the last such panel closes or its page is hidden. Reopening a feed panel backfills what was logged meanwhile, up to 10 minutes back.
- **Permission prompts are off.** Claude Code in the sandbox runs in bypass-permissions mode: the MicroVM is the sandbox, and its keys are placeholders. See `images/sandbox/README.md` to turn prompts back on.

## Cost and cleanup

us-east-1 list prices, per hour:

| | Per hour |
|---|---|
| This stack: ALB $0.0225, its 2 public IPv4 addresses $0.010, the API task (0.5 vCPU / 1 GB ARM) $0.0198 | ≈ $0.052 |
| Part 2 (NAT gateway, its NLB and iron-proxy task, public IPv4, secrets) | ≈ $0.097 |
| **Both, no sandbox running** | **≈ $0.15** (≈ $3.60 a day) |
| Each running 2 GB sandbox | ≈ $0.126 |
| **Both, four sandboxes running** (the account's 8 GB quota) | **≈ $0.65** |

On top, by use: CloudWatch Logs Live Tail ($0.01 a minute while someone has a feed panel open), CloudFront, S3, DynamoDB, Cognito (Essentials free tier), ECR, log ingestion and NAT data.

```bash
bash scripts/destroy.sh    # terminate the sandboxes, force-delete added credentials, terraform destroy, delete the image
```

Then destroy part 2 from its own directory.

## License

Apache-2.0
