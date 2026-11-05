# My2cents.ai Lambda MicroVM Playground: build spec (part 3)

> The brief the playground was first built from, corrected where the build went another way. For how it works now,
> read [README.md](README.md) and [backend/API.md](backend/API.md). The main changes since the brief: transparent
> interception only (part 2's two VPCs; no proxy settings or resolv.conf in sandboxes), four sandbox images (one per
> size), the memory quota read from Service Quotas, revoke by config reload when part 2's control plane is on, the
> admin-only proxy policy (allowlist, credentials, AWS access, factory reset), and Resolver query logging owned by
> part 2.

A deployed web playground where a signed-in user manages several Lambda MicroVM sandboxes, opens a
terminal in each (Claude Code included), and watches what the sandbox does through iron-proxy.
It builds on part 2's network and proxy, which already exist and must not be modified except where
this spec says so.

Repo root for this part: `~/Code/lambda-microvm-workspace/lambda-microvm-workspace/` (branch
`lambda-microvm-workspace` of the my2centsai-blog-samples repo). Never push. Region us-east-1.

## What already exists (part 2)

- Terraform state: `~/Code/lambda-microvm-iron-proxy/lambda-microvm-iron-proxy/infra/terraform.tfstate`
  (local backend). The outputs this part reads (`terraform output -json` in that dir, or `terraform_remote_state`)
  include the two VPCs (`proxy_vpc_id`, `sandbox_vpc_id`), `public_subnet_id` (one public subnet, AZ a),
  `public_route_table_id`, `proxy_subnet_ids` (2 private subnets routed to a NAT gateway; good for Fargate),
  `proxy_sg_id`, `ecs_cluster` (`my2cents-ip`, also runs iron-proxy), `connector_arn` (the sandbox VPC's egress
  connector for MicroVMs), `nlb_ips` (["10.50.10.10","10.50.11.10"]), `proxy_log_group` (iron-proxy's JSON audit),
  `query_log_group` (the sandbox VPC's Resolver query log), `placeholders` and `secret_arns` ({anthropic, github,
  demo}), `build_role_arn`, `artifacts_bucket`, `ca_cert_pem`, and the control plane's (`control_plane`,
  `control_bucket`, ...). There is one proxy mode: transparent.
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
  egressNetworkConnectors [connector_arn] (an empty list isn't offline: Lambda attaches its managed INTERNET_EGRESS
  connector, direct internet), idlePolicy {autoResumeEnabled, maxIdleDurationSeconds (>= 60), suspendedDurationSeconds (>= 0)},
  maximumDurationInSeconds (<= 28800, counts suspended time), runHookPayload (<= 4096 bytes), no tags,
  no execution role (we don't attach one), clientToken (idempotent).
- States: PENDING, RUNNING, SUSPENDING, SUSPENDED, TERMINATING, TERMINATED. run → RUNNING ~2 s;
  suspend ~0.5 s; resume ~0.8 s; auto-resume on endpoint traffic ~0.3-0.8 s.
- Idle only counts inbound endpoint traffic. Any request the backend makes to the MicroVM endpoint
  (agent HTTP or shell WS) keeps it awake. So: only poll the in-VM agent while a terminal for that
  sandbox is attached in some browser; otherwise use get_microvm (control plane, doesn't count).
- Open TCP/WebSocket connections die on suspend/resume. Clients must reconnect.
- Account quota: total MicroVM memory, suspended ones count: Service Quotas `lambda` / L-CD1C0CC4 ("Max allocated
  ARM_64 MicroVM memory", GB, adjustable per account; 8 GB here). The backend reads it (cached an hour, QUOTA_GB=8 if
  the call fails). RunMicrovm ~1/s. The quota is an estimate and never blocks: every built size can be picked, create
  always calls RunMicrovm, and AWS decides (its accounting may differ or change).
- Size comes from the image (`resources[].minimumMemoryInMiB`; vCPU = memory / 2 GB, bursting to 4x), so each size is
  its own image from the same code: 1 GB / 0.5 vCPU, 2 GB / 1 vCPU (the default), 4 GB / 2 vCPU, 8 GB / 4 vCPU (all of
  an 8 GB quota). Inside, `nproc` and `free -m` show the 4x burst ceiling, not the size.
- With autoResumeEnabled false, endpoint traffic to a suspended MicroVM gets HTTP 502 at once (agent and shell) and
  doesn't wake it; only resume_microvm does.
- suspendedDurationSeconds applies to idle suspends only: 60 s terminated an idle-suspended MicroVM 60 s later (state
  reason "MicroVM exceeded maximum lifetime."), while one suspended with suspend_microvm stayed SUSPENDED for 10 minutes.
- Endpoint auth: `create_microvm_auth_token(microvmIdentifier, expirationInMinutes, allowedPorts=[{"port":8080}])`
  → `["authToken"]["X-aws-proxy-auth"]`, header `X-aws-proxy-auth`, optional `X-aws-proxy-port`.
  Only ever mint tokens for port 8080 (the hooks listen on 9001 and must not be reachable).
- Shell: `create_microvm_shell_auth_token(microvmIdentifier, expirationInMinutes)` → `["authToken"]["X-aws-proxy-auth"]`.
  Connect `wss://<endpoint>/shell` with header `X-aws-proxy-auth: <shell token>`. First frame from the
  server is text JSON `{"type":"session_init","session_id":...}`. Client sends resize as text JSON
  `{"type":"resize","rows":R,"cols":C}` and keystrokes as BINARY frames. Output arrives as binary
  (decode UTF-8) or text. The shell is bash as root in the app container. Closing it sends
  `1000 shell exited`. Browsers can't set headers on WebSockets, so the backend bridges.
- Placeholders reach the VM via the run-hook payload JSON `{"env": {...}, "nameserver": null}`; the agent writes
  `/etc/sandbox.env` (export lines). No proxy settings and no resolv.conf (it's read-only in a MicroVM): part 2's
  sandbox VPC answers every name with a CNAME to iron-proxy's load balancer, so interception is transparent.

## Components and owners

### 1. Sandbox image v2 (`images/sandbox/`)

Start from part 2's `images/sandbox/Dockerfile` and `agent.py` (copy them). Add:
- tmux, plus `/root/.bashrc` that sources `/etc/sandbox.env` (placeholders and extra variables) and a short MOTD.
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
- `scripts/build_sandbox_image.py`: builds four images from the same code, one per size: `my2cents-ws-sandbox-1g`,
  `my2cents-ws-sandbox` (2 GB, the default), `-4g` and `-8g` (`resources=[{"minimumMemoryInMiB": 1024 | 2048 | 4096 |
  8192}]`), with hooks (port 9001, all six enabled), build role + artifacts bucket from part 2's outputs, CA from
  `ca_cert_pem`, and `VALIDATE_CMD` env var that warms `claude --version`, tmux and node (part 1 showed
  /validate warms the lazily-loaded snapshot disk).
- Prove it end to end with a script `scripts/smoke_sandbox.py`: run a MicroVM on the connector, open the
  shell WS, run `tmux new -A -s main`, start `claude` interactively, confirm the TUI renders with no
  onboarding prompt, send a prompt, see a reply, suspend, resume, reconnect, reattach, and see the same
  Claude screen. Also call /stats and /claude-usage. Terminate at the end. Use at most 1 MicroVM at a time.

### 2. Backend (`backend/`): FastAPI on ECS Fargate (ARM64)

Python 3.12, FastAPI + uvicorn, boto3 1.43.123, websockets. Dockerfile for linux/arm64.
Config from env (backend/README.md has the full list): TABLE_NAME, COGNITO_USER_POOL_ID, COGNITO_CLIENT_ID,
AWS_REGION, IMAGE_ARN and IMAGE_ARNS_JSON (one image per size), CONNECTOR_ARN, PLACEHOLDERS_JSON, SECRET_ARNS_JSON,
TASK_ROLE_ARN, PROXY_LOG_GROUP_ARN, DNS_LOG_GROUP_ARN (part 2's Resolver query log), the control plane's bucket and
keys, ORIGIN_SECRET (requests without header `X-Origin-Verify: <ORIGIN_SECRET>` → 403, except /healthz).

Auth: Cognito ID token (JWT, RS256) verified against the pool JWKS (iss, aud=client id, token_use=id, exp).
REST: `Authorization: Bearer <id token>`. WebSockets: the first client message must be
`{"type":"auth","token":"<id token>"}` within 5 s, else close 4401.

REST (all JSON, all scoped to the caller's `sub`; ownership in DynamoDB):
- `GET /healthz` → 200 (no auth, for the ALB).
- `GET /api/me` → `{"sub","email"}`
- `GET /api/config` → public settings for the UI: `{"proxy_mode":"transparent","quota_gb","quota_source",
  "max_duration_s","pricing":{...}}` (`quota_gb` is the account's MicroVM memory quota from Service Quotas), plus
  the create options: sizes (memory, vCPU, $/h, whether the image is built), lifetime/idle/suspended limits, the
  env-variable rules and reserved names, and the run hook payload's bytes in use (backend/API.md).
- `GET /api/sandboxes` → list of `{"id","name","state","created_at","started_at","max_duration_s","idle_minutes","image_version"}`
  (state from get_microvm; drop TERMINATED ones from DDB lazily, or keep them flagged for 1 hour).
- `POST /api/sandboxes` `{"name","size"="medium","idle_minutes"=15,"max_minutes"=480,"suspended_minutes"=null,
  "auto_resume"=true,"env"={}}` → run_microvm (the size's image, HTTP_INGRESS+SHELL_INGRESS, connector,
  idlePolicy {autoResumeEnabled: auto_resume, maxIdleDurationSeconds: idle*60, suspendedDurationSeconds:
  suspended*60 or until max}, maximumDurationInSeconds: max_minutes*60, runHookPayload settings JSON with the extra
  env, clientToken), store item (size and settings too), return it. Idle must be shorter than the lifetime; env names
  are validated and reserved ones refused (backend/API.md). No quota pre-check. On ServiceQuotaExceededException
  return 409 that quotes AWS's message, names the size and gives the current estimate (used/free of the quota).
- `POST /api/sandboxes/{id}/suspend`, `POST /api/sandboxes/{id}/resume`, `DELETE /api/sandboxes/{id}` (terminate).
- `GET /api/sandboxes/{id}/vitals?live=0|1` → control-plane state + timers (`started_at`, `max_duration_s`,
  `expires_at`, idle policy) always; with live=1 also agent `/stats` (this wakes/keeps awake the VM; the UI only
  sets live=1 while a terminal is attached).
- `GET /api/sandboxes/{id}/claude-usage` → agent `/claude-usage` plus `est_cost_usd` per session and total, using a
  pricing table for the models seen (load the `claude-api` skill to get current per-model prices; put them in one
  dict with a comment saying where they came from and the date).
- `GET /api/quota` → `{"used_gb","quota_gb"}` summed from each live MicroVM's size (the user's from their records,
  others' from the image they run), since the quota is per account; plus one segment per MicroVM for the bar.
- `GET /api/credentials` → per secret (anthropic, github, demo): `{"name","placeholder_prefix","revoked":bool,
  "uses_15m":n,"hosts":{host:n},"last_used":ts}` computed from the proxy log group (filter_log_events, last 15 min,
  cache 10 s).
- `POST /api/credentials/{name}/revoke` / `.../restore` (admins) → add/remove a deny statement on the secret's
  resource policy for GetSecretValue. With part 2's control plane on it names the iron-proxy task role and the four
  scoped roles, and applies by reload: a new config version, live at the sidecar's next check (within about 30 s).
  With it off, it names the task role and restarts iron-proxy. The response warns this affects every sandbox on the
  proxy.
- Proxy policy, AWS access (control plane on; backend/API.md has the shapes): the role (view-only, read-only,
  data-reader, builder) and an optional session policy of picks. Picks: single actions, `<service>:*` and
  `Get*`/`List*`/`Describe*`; an action outside the role can be picked (no effect with it, kept across a role switch).
  Refused: unknown, unmapped, anything in secretsmanager, ssm:GetParameter* on its own. A pick that covers
  ssm:GetParameter* adds an explicit Deny for it to the session policy. `POST /api/policy/aws/preview` returns, besides
  the policy, `coverage` per pick (N of M covered actions effective with the role, tone all/partial/none, why) and
  `effective` per service (role ∩ session policy, computed from the role's policies; with no session policy, the
  role's own allowed actions). iron-proxy's allowed_services and hosts hold only services with at least one
  effective action, so a no-effect service gets iron-proxy's 403.

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
  `start_live_tail` (one shared tail per backend process for both log groups, only while a browser shows a feed
  panel, fan out to subscribers; restart it
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
  RUNNING and no terminal is attached), "New sandbox" (name; size with memory, vCPU and $/h, every built size
  selectable, with a non-blocking hint when the estimate says it may exceed the quota; idle suspend; lifetime in minutes; terminate when suspended; resume on connect;
  extra env variables with the bytes left and a not-for-secrets warning), quota bar (used of the account's quota,
  one cell per GB, informational).
- Main: terminal (xterm, fills the space, reconnect overlay states: connecting, suspended → "Resume" button,
  reconnecting with countdown, exited). Toolbar: Suspend, Resume, Terminate (confirm), "Start Claude Code" (checks the
  tmux pane first and refuses if something other than a shell runs there, clears the prompt's pending text, types
  `claude`, and watches for it for 15 s, always coming back), copy-paste friendly.
- Right panel (tabs or stacked cards):
  1. Proxy feed: live list of requests (time, method, host+path, status, allow/deny badge, key-swapped badge with the
     secret name), filters (all / denied / with secret), a requests-per-minute sparkline by host, banner explaining
     the feed covers all sandboxes on this proxy.
  2. Credentials: one card per secret (uses in 15 min, hosts, last used), Revoke/Restore with a confirm dialog that
     says it affects every sandbox.
  3. Claude Code: tokens per session (input, output, cache read, cache creation), turns, model, estimated cost, total;
     refresh every 10 s only while a terminal is attached.
  4. Vitals & timers: state, uptime, CPU % and memory vs the size's baseline / its 4x burst (small charts, sampled every
     5 s while attached), time until the lifetime cap, idle policy, the sandbox's settings (size, lifetime, egress,
     env variable names), lifecycle timeline (run, suspend, resume events from /ws/events).
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
- Route 53 Resolver query logging for the sandbox VPC is part 2's (its `query_log_group`); this part only reads it.
- ALB (internet-facing, 2 public subnets) with SG allowing tcp/80 only from the CloudFront origin-facing managed
  prefix list; listener 80: forward to the backend target group only if header `X-Origin-Verify` equals a random
  secret, else fixed 403. Idle timeout 3600 s (WebSockets).
- ECS Fargate service `workspace-api` (ARM64, 0.5 vCPU / 1 GB, desired 1) on part 2's cluster, in the proxy subnets,
  SG allowing 8000 from the ALB SG; health check `/healthz`. Task role: lambda-microvms actions on the four
  sandbox images and their MicroVMs (Run/Get/List/Suspend/Resume/Terminate/CreateMicrovmAuthToken/
  CreateMicrovmShellAuthToken) plus `lambda:PassNetworkConnector` on the connector and the managed connectors
  (`arn:aws:lambda:us-east-1:aws:network-connector:aws-network-connector:*`); `servicequotas:GetServiceQuota` on the
  MicroVM memory quota (L-CD1C0CC4); dynamodb on the tables; logs:StartLiveTail/FilterLogEvents on the two log
  groups; secretsmanager Put/DeleteResourcePolicy + GetResourcePolicy on the three part 2 secrets, and the custom
  credentials' secrets (`my2cents-ip/user/*`); the control bucket's objects (infra/README.md has the list). IAM action names: check the service authorization reference
  (part 1 research notes say `lambda:RunMicroVm` with capital V appears in one template; verify by testing).
- S3 bucket for the SPA (private) + CloudFront distribution: default behavior → S3 via OAC (SPA fallback: 403/404
  → /index.html 200); `/api/*` and `/ws/*` → ALB origin over HTTP, CachingDisabled, AllViewerExceptHostHeader
  origin request policy, all methods, origin custom header X-Origin-Verify. WebSockets work through CloudFront.
- Outputs: cloudfront_domain, spa_bucket, ecr_repo_url, user_pool_id, client_id, cognito_domain, table, alb_dns,
  dns_log_group, origin_secret (sensitive).
- `scripts/deploy.sh`: build the four sandbox images in the background when missing or built for another CA,
  build+push backend image (docker buildx linux/arm64), terraform apply, build frontend, write config.json, s3 sync,
  CloudFront invalidation, force ECS deployment, then wait for the images. `scripts/create_user.sh <email>`:
  admin-create-user + admin-set-user-password (permanent) for testing.

## Conventions

- Account-specific values never hard-coded; read from Terraform outputs/env. Redact the account ID in anything
  printed for the blog.
- Keep secrets out of logs, state and the browser. The real Anthropic/GitHub values only exist in Secrets Manager
  and inside iron-proxy.
- Each component gets a short README section; the top-level README is assembled at the end.
