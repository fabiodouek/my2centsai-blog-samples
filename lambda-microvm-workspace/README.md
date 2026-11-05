# My2cents.ai Lambda MicroVM Playground

My2cents.ai Lambda MicroVM Playground is a browser playground deployed on AWS Lambda MicroVMs. Sign in, create a sandbox (1, 2, 4 or 8 GB, with its own idle policy, lifetime and environment variables), and get a terminal with Claude Code ready to run. Every sandbox reaches the internet only through part 2's iron-proxy, without knowing it, and what it does there stays on screen:

- every request, and which credential was swapped in
- credential use, with a Revoke button
- Claude Code's tokens and cost
- the MicroVM's vitals and timers
- every DNS lookup from the sandbox VPC

Admins can change the proxy's policy from the browser, for every sandbox at once: the allowlist, the credentials it injects (including new ones), and the AWS access sandbox calls are signed with: one of four roles (view-only by default), optionally narrowed with a session policy picked and searched from AWS's full Service Authorization Reference: single actions, `<service>:*` or `Get*`/`List*`/`Describe*`, each shown with how much of it the role makes effective, next to an "Effective access" table (role ∩ session policy, per service). Changes go live at the proxy's next check for them, within about 30 s, through iron-proxy's reload API, without a restart. Admins also get a Config view: the proxy's config as it is in S3 against what iron-proxy applied, with an in-sync badge.

The terminal runs inside tmux in the MicroVM, so closing the tab, a network blip or a suspend doesn't lose the session.

Companion code for [Lambda MicroVM: Hands-On Exploration of Self-Service Sandboxes](https://my2cents.ai/deep-dive/lambda-microvms-workspace), part 3 of the AWS Lambda MicroVMs series. It builds on [part 2's companion code](../lambda-microvm-iron-proxy), which must be applied first with `control_plane=true`.

Video walkthrough: https://youtu.be/Jlqd6fZ01vU

## How it fits together

Part 2 owns the network and the proxy; this repo adds the playground around them.

- **Part 2's two VPCs.** The sandbox VPC holds only the MicroVM connector: no internet gateway, no NAT, routes only to iron-proxy's load balancer in the proxy VPC, and DNS Firewall answering every name with a CNAME to it. So sandboxes have no proxy settings at all (transparent interception): `curl`, `git`, `gh`, the AWS CLI and Claude Code connect to iron-proxy believing they talk to the real host.
- **Part 2's control plane** (`control_plane=true`). iron-proxy reads its config from an S3 bucket through a sidecar that reloads it in place, and signs sandbox AWS calls as one of four roles (`view-only`, `read-only`, `data-reader`, `builder`). The playground edits that config and the role choice.
- **This repo:** CloudFront and S3 for the app, an ALB for `/api` and `/ws`, the API on ECS Fargate (in part 2's proxy VPC and cluster), Cognito, DynamoDB, ECR, and the sandbox images (one per size).

One iron-proxy serves every sandbox, so the policy, the proxy feed, the credential counters and the DNS tab cover every sandbox on it. iron-proxy sees requests from the load balancer and the sandboxes share the connector's addresses, so neither can tell sandboxes apart; the only per-sandbox label AWS sees is `app/ws-<sandbox name>` in the user agent.

## Layout

| Path | What |
|---|---|
| [`images/sandbox/`](images/sandbox/README.md) | The sandbox image: part 2's image plus tmux, tini, Claude Code that starts with no prompts, an agent with `/stats` and `/claude-usage`, and a `/validate` warm-up |
| [`backend/`](backend/README.md) | FastAPI on ECS Fargate: Cognito JWT auth; sandbox ownership in DynamoDB; run, suspend, resume and terminate; the terminal bridge to each MicroVM's shell WebSocket; vitals and Claude usage; credential revoke and restore; the proxy policy; the CloudWatch Logs Live Tail feed for iron-proxy's audit log and the sandbox VPC's query log. The contract is in [`backend/API.md`](backend/API.md). |
| [`frontend/`](frontend/README.md) | React, TypeScript, Vite and xterm.js: the sidebar, terminal, five panels and the Proxy policy page, with a mock mode for development |
| [`infra/`](infra/README.md) | Terraform: CloudFront with S3 for the app and an ALB for `/api` and `/ws`, the ECS service, Cognito, DynamoDB, ECR. Part 2 is read through `terraform_remote_state`. |
| `scripts/` | `deploy.sh`, `destroy.sh`, `create_user.sh`; build the sandbox images, one per size (`build_sandbox_image.py`), and smoke-test the medium one (`smoke_sandbox.py`) |
| [`SPEC.md`](SPEC.md) | The build spec the components were written against |

## Deploy

```bash
# 1. part 2, from its directory: terraform -chdir=infra apply -var control_plane=true (and its secrets, see its README)
# 2. then, from here:
bash scripts/deploy.sh                                   # images, Terraform, backend, frontend (at least ~13 min the first time: four sandbox images, about 3 min each, built alongside the rest)
bash scripts/create_user.sh --admin you@example.com      # prompts for a password; --admin may edit the proxy policy
```

`deploy.sh` checks that part 2 is the two-VPC stack and whether its control plane is on, builds each size's sandbox image when there is none or when it was built for another CA (a new part 2 has a new one), one after another (about 3 minutes each), and prints the CloudFront URL. It's safe to re-run. Use `SKIP_BACKEND=1`, `SKIP_FRONTEND=1` or `SKIP_IMAGE=1` to skip a part. A bare `terraform apply` scales the API service to 0, so always deploy through the script. `scripts/smoke_sandbox.py` runs one MicroVM to check the image: Claude Code, suspend and resume.

## Things to know

- **No exec API.** Lambda MicroVMs have no exec API, and any endpoint or shell token is root in the sandbox. So the API mints short-lived shell tokens itself and bridges the terminal; the browser never holds a MicroVM token.
- **No proxy settings in the sandbox.** The run hook payload carries placeholders, placeholder AWS keys, the CA variables, `SANDBOX_NAME`, `AWS_SDK_UA_APP_ID=ws-<name>` and `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` (iron-proxy refuses the CLI's default chunked uploads), and nothing else.
- **Feeds aren't per sandbox.** The proxy and DNS feeds cover every sandbox on the proxy: iron-proxy sees the load balancer's addresses, and the Resolver sees the connector's.
- **The proxy policy is global.** The Proxy policy page (admins only, Cognito group `admins`) edits the allowlist, the injected credentials and the AWS access for every sandbox on the proxy. A new credential's value goes straight to Secrets Manager; sandboxes get a placeholder, and only sandboxes created after it was added get it in their environment. Secrets Manager is never reachable through iron-proxy, whatever the role.
- **Revoke takes up to about 30 s.** It denies the proxy's identities access to the secret and writes a new config version; at its next check of the bucket (every 30 s, part 2's `control_poll_s`) the sidecar reloads iron-proxy, which drops its cached copy. Until then the key is still swapped in. For a leaked key, revoke it at its source as well.
- **A pick can have no effect.** The AWS access editor lets you pick actions outside the selected role (marked "no effect with <role>"), so a policy can be built before switching role, and a selection survives the switch. Wildcards show "N of M effective" (filled, half or empty dot), and the Effective access tab lists, per service, what the role and the session policy together allow, computed from the role's policies. iron-proxy is told only about services with at least one effective action: a service whose picks have no effect gets iron-proxy's bare 403. A wildcard that covers `ssm:GetParameter*` (`ssm:*`, `ssm:Get*`) never grants it: the session policy adds an explicit Deny. Secrets Manager can't be picked at all.
- **Sizes are images.** A MicroVM's memory comes from its image, so there are four images built from the same code: Small (1 GB, 0.5 vCPU, ≈ $0.063/h), Medium (2 GB, 1 vCPU, ≈ $0.126/h, the default), Large (4 GB, 2 vCPU, ≈ $0.252/h) and X-Large (8 GB, 4 vCPU, ≈ $0.504/h), which fills an 8 GB quota alone. The quota is read from Service Quotas ("Max allocated ARM_64 MicroVM memory", L-CD1C0CC4: 8 GB in this account, adjustable, so it differs per account) and cached for an hour; the bar's tooltip says where it came from. The quota is our estimate, so it never blocks: every built size stays selectable, the New form warns when a size may not fit what's left (suspended sandboxes count), and create always asks AWS. When AWS refuses with ServiceQuotaExceededException, the error quotes AWS's message, names the size and gives the estimate.
- **The idle policy is per sandbox.** Suspend after N idle minutes (at least 1), terminate after N minutes suspended (0 to the lifetime; by default only when the lifetime ends), and auto-resume on or off. Lambda counts the suspended time only after an idle suspend: a sandbox suspended with the Suspend button stays until its lifetime ends. Off, opening the terminal or calling the agent doesn't wake a suspended sandbox: the terminal says "Suspended: press Resume". The lifetime is 1 to 480 minutes, suspended time included, and must be longer than the idle time.
- **Extra environment variables aren't for secrets.** They go in the run hook payload to `/etc/sandbox.env`, are visible to everything in the sandbox and are stored with its record. Names the sandbox sets itself (placeholders, `AWS_*`, the CA variables, `SANDBOX_NAME`...) are refused, values can't hold `$`, backquotes, `"` or backslashes, and the payload stays under 4,096 bytes. Keys go under Proxy policy → Credentials.
- **Every sandbox goes through the proxy; there is no offline mode.** An empty `egressNetworkConnectors` list doesn't cut a MicroVM off. We ran one with `[]`: Lambda accepted it, the MicroVM was RUNNING in 1.4 s with its run hook done, and `get_microvm` listed Lambda's managed `aws-network-connector:INTERNET_EGRESS` as its egress. Inside, `api.github.com` resolved to a real GitHub address, `curl https://api.github.com/zen` answered straight from GitHub and a raw IP connected: no allowlist, no key swap, no audit log. AWS calls and Claude Code failed only because their keys were placeholders. So the API refuses to run a sandbox without part 2's connector (`POST /api/sandboxes` answers 503 if `CONNECTOR_ARN` is missing or is `INTERNET_EGRESS`).
- **Inside, a sandbox sees its burst ceiling, not its size.** Sizes are baselines: what's billed and what counts against the quota. `nproc` and `free -m` show 4x: Small 2 vCPU / 3998 MB, Medium 4 / 8016 MB, Large 8 / 16052 MB, X-Large 16 / 32123 MB.
- **Factory reset.** Admins can put the proxy policy back to part 2's deploy-time defaults from the Config tab: the seeded allowlist, the built-in credentials active (deny statements removed), AWS access back to view-only with no session policy, and `iron-proxy.yaml` regenerated from part 2's `iron-proxy.default.yaml`. Custom credentials leave the config but their secrets stay in Secrets Manager, listed in the dialog so they can be added again. It takes typing `reset`, and goes live at the proxy's next check.
- **Watching keeps the sandbox awake.** Any request to the in-VM agent is inbound traffic, which keeps the sandbox from idle-suspending. Live stats and Claude usage are polled only while a terminal is attached.
- **Live Tail is billed per minute,** so the API runs it only while a browser is showing the Proxy or DNS panel: the side panel is open on one of those tabs and the page is visible. It stops 60 s after the last such panel closes or its page is hidden. Reopening a feed panel backfills what was logged meanwhile, up to 10 minutes back.
- **Permission prompts are off.** Claude Code in the sandbox runs in bypass-permissions mode: the MicroVM is the sandbox, and its keys are placeholders. See `images/sandbox/README.md` to turn prompts back on.

## Cost and cleanup

us-east-1 list prices, per hour:

| | Per hour |
|---|---|
| This stack: ALB $0.0225, its 2 public IPv4 addresses $0.010, the API task (0.5 vCPU / 1 GB ARM) $0.0198 | ≈ $0.052 |
| Part 2 with its control plane (NAT gateway, its NLB and iron-proxy task, public IPv4, Secrets Manager including the control plane's token, private zone; plus about $0.55 a month of S3 requests from the sidecar) | ≈ $0.096 |
| **Both, no sandbox running** | **≈ $0.15** (≈ $3.55 a day) |
| Each running sandbox: Small 1 GB / Medium 2 GB / Large 4 GB / X-Large 8 GB | ≈ $0.063 / $0.126 / $0.252 / $0.504 |
| **Both, 8 GB of sandboxes running** (the account's quota, any mix of sizes) | **≈ $0.65** |

On top, by use: CloudWatch Logs Live Tail ($0.01 a minute while someone has a feed panel open), CloudFront, S3, DynamoDB, Cognito (Essentials free tier), ECR, log ingestion and NAT data.

```bash
bash scripts/destroy.sh    # terminate the sandboxes, force-delete added credentials, terraform destroy, delete the four images
```

Then destroy part 2 from its own directory.

## License

Apache-2.0
