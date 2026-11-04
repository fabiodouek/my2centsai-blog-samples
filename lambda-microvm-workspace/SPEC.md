# Lambda MicroVM Workspace: build spec (part 3)

A deployed web workspace where a signed-in user manages several Lambda MicroVM sandboxes, opens a
terminal in each (Claude Code included), and watches what the sandbox does through iron-proxy.
It builds on part 2's network and proxy, which already exist and must not be modified except where
this spec says so.

Repo root for this part: `~/Code/lambda-microvm-workspace/lambda-microvm-workspace/` (branch
`lambda-microvm-workspace` of the my2centsai-blog-samples repo). Never push. Region us-east-1.

## What already exists (part 2)

- Terraform state: `~/Code/lambda-microvm-iron-proxy/lambda-microvm-iron-proxy/infra/terraform.tfstate`
  (local backend). Outputs (read them with `terraform output -json` in that dir, or `terraform_remote_state`):
  `vpc_id`, `vpc_cidr`, `igw_id`, `public_subnet_id` (one public subnet, AZ a), `public_route_table_id`,
  `proxy_subnet_ids` (2 private subnets routed to a NAT gateway; good for Fargate), `sandbox_subnet_ids`,
  `proxy_sg_id`, `azs`, `ecs_cluster` / `ecs_cluster_arn` (cluster `my2cents-ip`, also runs iron-proxy),
  `connector_arn` (the VPC egress connector for MicroVMs), `nlb_ips` (["10.50.10.10","10.50.11.10"]),
  `proxy_mode` ("explicit" or "dns"), `proxy_log_group` (`/my2cents/iron-proxy`, iron-proxy JSON audit),
  `placeholders` ({anthropic, github, demo}), `secret_arns` ({anthropic, github, demo}),
  `build_role_arn`, `artifacts_bucket`, `ca_cert_pem`.
- Part 2 companion code (reuse ideas, don't import): `~/Code/lambda-microvm-iron-proxy/lambda-microvm-iron-proxy/`
  (`common.py` has run(), settings() for the run-hook payload, VM wrapper, audit parsing;
  `images/sandbox/` has the sandbox Dockerfile + agent.py).
- iron-proxy audit record shape (one JSON line per request in the log group):
  `{"time":..., "level":"INFO|WARN", "msg":"request", "audit":{"host","method","path","remote_addr","sni","mode","action":"allow|reject","status_code","duration_ms"}, "rejected_by"?: "allowlist|aws_auth|...", "request_transforms":[{"name":"secrets","action","annotations"?:{"swapped":[{"secret":"<arn>","locations":["header:Authorization"]}]}}], "tunnel"?:{...}}`.
  `remote_addr` is always the NLB IP, so requests can't be attributed to a MicroVM. Show the feed as
  "all sandboxes on this proxy" and say why in the UI.

## Lambda MicroVM facts the code must respect (measured in parts 1 and 2)

- API: boto3 `lambda-microvms` (boto3 1.43.123). Image and run calls need the image ARN.
- run_microvm params: imageIdentifier, ingressNetworkConnectors (use HTTP_INGRESS + SHELL_INGRESS:
  `arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:HTTP_INGRESS` / `...:SHELL_INGRESS`),
  egressNetworkConnectors [connector_arn], idlePolicy {autoResumeEnabled, maxIdleDurationSeconds, suspendedDurationSeconds},
  maximumDurationInSeconds (<= 28800, counts suspended time), runHookPayload (<= 4096 bytes), no tags,
  no execution role (we don't attach one), clientToken (idempotent).
- States: PENDING, RUNNING, SUSPENDING, SUSPENDED, TERMINATING, TERMINATED. run → RUNNING ~2 s;
  suspend ~0.5 s; resume ~0.8 s; auto-resume on endpoint traffic ~0.3-0.8 s.
- Idle only counts inbound endpoint traffic. Any request the backend makes to the MicroVM endpoint
  (agent HTTP or shell WS) keeps it awake. So: only poll the in-VM agent while a terminal for that
  sandbox is attached in some browser; otherwise use get_microvm (control plane, doesn't count).
- Open TCP/WebSocket connections die on suspend/resume. Clients must reconnect.
- Account quota: 8 GB total MicroVM memory (4 x 2 GB), suspended ones count. RunMicrovm ~1/s.
- Endpoint auth: `create_microvm_auth_token(microvmIdentifier, expirationInMinutes, allowedPorts=[{"port":8080}])`
  → `["authToken"]["X-aws-proxy-auth"]`, header `X-aws-proxy-auth`, optional `X-aws-proxy-port`.
  Only ever mint tokens for port 8080 (the hooks listen on 9001 and must not be reachable).
- Shell: `create_microvm_shell_auth_token(microvmIdentifier, expirationInMinutes)` → `["authToken"]["X-aws-proxy-auth"]`.
  Connect `wss://<endpoint>/shell` with header `X-aws-proxy-auth: <shell token>`. First frame from the
  server is text JSON `{"type":"session_init","session_id":...}`. Client sends resize as text JSON
  `{"type":"resize","rows":R,"cols":C}` and keystrokes as BINARY frames. Output arrives as binary
  (decode UTF-8) or text. The shell is bash as root in the app container. Closing it sends
  `1000 shell exited`. Browsers can't set headers on WebSockets, so the backend bridges.
- Placeholders + proxy env reach the VM via the run-hook payload JSON `{"env": {...}, "nameserver": ip|null}`;
  the agent writes `/etc/sandbox.env` (export lines) and, in dns mode, `/etc/resolv.conf`.
  Build the payload like part 2's `common.settings()`.

## Components and owners

### 1. Sandbox image v2 (`images/sandbox/`)

Start from part 2's `images/sandbox/Dockerfile` and `agent.py` (copy them). Add:
- tmux, plus `/root/.bashrc` that sources `/etc/sandbox.env` (proxy vars + placeholders) and a short MOTD.
- Claude Code pre-configured so the interactive TUI starts with no prompts: `/root/.claude.json` with
  onboarding completed, the placeholder API key pre-approved (Claude Code stores the last 20 chars of an
  approved custom key under `customApiKeyResponses.approved`), and `/root/work` trusted. Verify the exact
  keys by testing; don't guess.
- `/root/work` as the working directory, git identity, the CA trust env vars from part 2.
- Agent additions (port 8080): `GET /stats` → `{"cpu_pct", "cpu_count", "mem_total_mb", "mem_used_mb", "load1",
  "disk_used_mb", "disk_total_mb", "uptime_s", "procs"}` (cpu % from two /proc/stat samples 200 ms apart);
  `GET /claude-usage` → parse `/root/.claude/projects/**/*.jsonl`, return per session
  `{"session_id","project","model","turns","input_tokens","output_tokens","cache_read_tokens","cache_creation_tokens","first_ts","last_ts"}`
  and totals; robust to partial lines.
- `scripts/build_sandbox_image.py`: builds image `my2cents-ws-sandbox` with hooks (port 9001, all six enabled),
  `resources=[{"minimumMemoryInMiB": 2048}]`, build role + artifacts bucket from part 2's outputs, CA from
  `ca_cert_pem`, and `VALIDATE_CMD` env var that warms `claude --version`, tmux and node (part 1 showed
  /validate warms the lazily-loaded snapshot disk).
- Prove it end to end with a script `scripts/smoke_sandbox.py`: run a MicroVM on the connector, open the
  shell WS, run `tmux new -A -s main`, start `claude` interactively, confirm the TUI renders with no
  onboarding prompt, send a prompt, see a reply, suspend, resume, reconnect, reattach, and see the same
  Claude screen. Also call /stats and /claude-usage. Terminate at the end. Use at most 1 MicroVM at a time.

### 2. Backend (`backend/`): FastAPI on ECS Fargate (ARM64)

Python 3.12, FastAPI + uvicorn, boto3 1.43.123, websockets. Dockerfile for linux/arm64.
Config from env: TABLE_NAME, COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID, AWS_REGION, IMAGE_ARN,
CONNECTOR_ARN, PROXY_MODE, NLB_IPS (comma list), PLACEHOLDERS_JSON, SECRET_ARNS_JSON, PROXY_LOG_GROUP_ARN,
DNS_LOG_GROUP_ARN, ORIGIN_SECRET (requests without header `X-Origin-Verify: <ORIGIN_SECRET>` → 403, except /healthz).

Auth: Cognito ID token (JWT, RS256) verified against the pool JWKS (iss, aud=client id, token_use=id, exp).
REST: `Authorization: Bearer <id token>`. WebSockets: the first client message must be
`{"type":"auth","token":"<id token>"}` within 5 s, else close 4401.

REST (all JSON, all scoped to the caller's `sub`; ownership in DynamoDB):
- `GET /healthz` → 200 (no auth, for the ALB).
- `GET /api/me` → `{"sub","email"}`
- `GET /api/config` → public settings for the UI: `{"proxy_mode","quota_gb":8,"max_duration_s","pricing":{...}}`
- `GET /api/sandboxes` → list of `{"id","name","state","created_at","started_at","max_duration_s","idle_minutes","image_version"}`
  (state from get_microvm; drop TERMINATED ones from DDB lazily, or keep them flagged for 1 hour).
- `POST /api/sandboxes` `{"name","idle_minutes"=15,"max_hours"=8}` → run_microvm (HTTP_INGRESS+SHELL_INGRESS,
  connector, idlePolicy {autoResumeEnabled:true, maxIdleDurationSeconds:idle*60, suspendedDurationSeconds: until max},
  maximumDurationInSeconds, runHookPayload settings JSON, clientToken), store item, return it.
  On ServiceQuotaExceededException return 409 with a clear message.
- `POST /api/sandboxes/{id}/suspend`, `POST /api/sandboxes/{id}/resume`, `DELETE /api/sandboxes/{id}` (terminate).
- `GET /api/sandboxes/{id}/vitals?live=0|1` → control-plane state + timers (`started_at`, `max_duration_s`,
  `expires_at`, idle policy) always; with live=1 also agent `/stats` (this wakes/keeps awake the VM; the UI only
  sets live=1 while a terminal is attached).
- `GET /api/sandboxes/{id}/claude-usage` → agent `/claude-usage` plus `est_cost_usd` per session and total, using a
  pricing table for the models seen (load the `claude-api` skill to get current per-model prices; put them in one
  dict with a comment saying where they came from and the date).
- `GET /api/quota` → `{"used_gb","quota_gb"}` from the user's non-terminated MicroVMs × 2 GB (and all MicroVMs on
  the image, since the quota is per account).
- `GET /api/credentials` → per secret (anthropic, github, demo): `{"name","placeholder_prefix","revoked":bool,
  "uses_15m":n,"hosts":{host:n},"last_used":ts}` computed from the proxy log group (filter_log_events, last 15 min,
  cache 10 s).
- `POST /api/credentials/{name}/revoke` / `.../restore` → put/delete a Secrets Manager resource policy denying
  the iron-proxy task role (`task_role_arn` from part 2 outputs; pass as env TASK_ROLE_ARN) GetSecretValue.
  Response warns this affects every sandbox on the proxy.

WebSockets:
- `/ws/terminal/{id}`: after auth and ownership check, mint a shell token, connect to `wss://<endpoint>/shell`
  with the header, wait for session_init, then bridge: browser binary → MicroVM binary; browser text JSON
  `{"type":"resize",...}` → MicroVM text JSON; MicroVM output → browser binary. On connect, send
  `tmux new -A -s main\n` (after a resize). If the MicroVM is SUSPENDED and autoResume is on, connecting
  resumes it (verify; if not, call resume_microvm first). Send control messages to the browser as text JSON
  `{"type":"status","state":...,"detail":...}` (connecting, attached, reconnecting, suspended, exited).
  If the upstream closes unexpectedly, retry with backoff up to 60 s while telling the browser.
- `/ws/events`: after auth, pushes text JSON events:
  `{"type":"proxy","ts","action","status","method","host","path","rejected_by","secrets":[names swapped]}`,
  `{"type":"dns","ts","query","qtype","rcode","srcaddr"}`, `{"type":"lifecycle","id","state","prev"}`
  (poll get_microvm for the user's sandboxes every 5 s and emit changes). Use CloudWatch Logs
  `start_live_tail` (one shared tail per backend process for both log groups, fan out to subscribers; restart it
  when it ends, it has a max session length) or fall back to filter_log_events polling every 3 s if Live Tail
  isn't available.

Map secret ARNs to names (anthropic/github/demo) for the UI. Never send real secret values anywhere.
Unit-test the JWT verification and the shell frame bridging with fakes. Run locally with `uvicorn` against
real AWS (use the local AWS profile) to test REST + terminal bridging against a real MicroVM (1 at a time).

### 3. Frontend (`frontend/`): React + TypeScript + Vite, built to static files in S3

Use the `impeccable` skill for design quality. Dark-first, polished, information-dense but calm, works at
1280-1920 px wide (laptop) and degrades sensibly to tablet; no phone layout needed beyond not breaking.
Libraries: xterm.js (+ fit and web-links addons), a small charting lib (uPlot or Recharts), oidc PKCE via
`oidc-client-ts` or a minimal hand-rolled PKCE against the Cognito managed login (domain + client id from
`/config.json`, which the deploy script writes: `{"region","userPoolId","clientId","cognitoDomain","apiBase":""}`).

Layout:
- Left sidebar: the user's sandboxes (name, state pill with live colour, time left of 8 h, idle countdown when
  RUNNING and no terminal is attached), "New sandbox" (name, idle minutes), quota bar (used/8 GB).
- Main: terminal (xterm, fills the space, reconnect overlay states: connecting, suspended → "Resume" button,
  reconnecting with countdown, exited). Toolbar: Suspend, Resume, Terminate (confirm), "Start Claude Code"
  (sends `claude\n` into the terminal), copy-paste friendly.
- Right panel (tabs or stacked cards):
  1. Proxy feed: live list of requests (time, method, host+path, status, allow/deny badge, key-swapped badge with the
     secret name), filters (all / denied / with secret), a requests-per-minute sparkline by host, banner explaining
     the feed covers all sandboxes on this proxy.
  2. Credentials: one card per secret (uses in 15 min, hosts, last used), Revoke/Restore with a confirm dialog that
     says it affects every sandbox.
  3. Claude Code: tokens per session (input, output, cache read, cache creation), turns, model, estimated cost, total;
     refresh every 10 s only while a terminal is attached.
  4. Vitals & timers: state, uptime, CPU % and memory vs the 2 GB baseline / 8 GB burst (small charts, sampled every
     5 s while attached), time until the 8 h cap, idle policy, lifecycle timeline (run, suspend, resume events
     from /ws/events).
  5. DNS: live list of DNS queries from the VPC (name, type, rcode), highlight NXDOMAIN and names not on the proxy
     allowlist, with a note that queries can't be tied to one sandbox.
- Empty states, loading skeletons, error toasts, keyboard focus, sensible copy. No marketing fluff.
Develop against a mock API (MSW or a tiny mock server) so it can be built before the backend exists, then switch
to the real backend. `npm run build` must produce `dist/`.

### 4. Infra (`infra/`): Terraform, reads part 2 via terraform_remote_state (local path variable)

- Second public subnet in AZ b (`10.50.1.0/24`) associated with part 2's public route table (ALB needs 2 AZs).
- DynamoDB table `my2cents-ws-sandboxes` (pk `sub`, sk `id`, on-demand).
- Cognito user pool (email sign-in), app client (public, no secret, PKCE, code flow, scopes openid email),
  managed login domain prefix `my2cents-ws-<random>`, callback/logout URL = `https://<cloudfront domain>/`.
- ECR repo `my2cents-ws-backend`.
- Route 53 Resolver query logging config → CloudWatch log group `/my2cents/dns-queries`, associated with part 2's VPC.
- ALB (internet-facing, 2 public subnets) with SG allowing tcp/80 only from the CloudFront origin-facing managed
  prefix list; listener 80: forward to the backend target group only if header `X-Origin-Verify` equals a random
  secret, else fixed 403. Idle timeout 3600 s (WebSockets).
- ECS Fargate service `workspace-api` (ARM64, 0.5 vCPU / 1 GB, desired 1) on part 2's cluster, in the proxy subnets,
  SG allowing 8000 from the ALB SG; health check `/healthz`. Task role: lambda-microvms actions on the
  `my2cents-ws-sandbox` image and its MicroVMs (Run/Get/List/Suspend/Resume/Terminate/CreateMicrovmAuthToken/
  CreateMicrovmShellAuthToken) plus `lambda:PassNetworkConnector` on the connector and the managed connectors
  (`arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:*`); dynamodb on the table;
  logs:StartLiveTail/FilterLogEvents on the two log groups; secretsmanager Put/DeleteResourcePolicy +
  GetResourcePolicy on the three part 2 secrets. IAM action names: check the service authorization reference
  (part 1 research notes say `lambda:RunMicroVm` with capital V appears in one template; verify by testing).
- S3 bucket for the SPA (private) + CloudFront distribution: default behavior → S3 via OAC (SPA fallback: 403/404
  → /index.html 200); `/api/*` and `/ws/*` → ALB origin over HTTP, CachingDisabled, AllViewerExceptHostHeader
  origin request policy, all methods, origin custom header X-Origin-Verify. WebSockets work through CloudFront.
- Outputs: cloudfront_domain, spa_bucket, ecr_repo_url, user_pool_id, client_id, cognito_domain, table, alb_dns,
  dns_log_group, origin_secret (sensitive).
- `scripts/deploy.sh`: build+push backend image (docker buildx linux/arm64), terraform apply, build frontend,
  write config.json, s3 sync, CloudFront invalidation, force ECS deployment. `scripts/create_user.sh <email>`:
  admin-create-user + admin-set-user-password (permanent) for testing.

### 5. Part 4 prep (`~/Code/lambda-microvm-stress/lambda-microvm-stress/`), code only

Separate later part; see the brief given to that agent.

## Conventions

- Account-specific values never hard-coded; read from Terraform outputs/env. Redact the account ID in anything
  printed for the blog.
- Keep secrets out of logs, state and the browser. The real Anthropic/GitHub values only exist in Secrets Manager
  and inside iron-proxy.
- Each component gets a short README section; the top-level README is assembled at the end.
