# Backend: workspace API

FastAPI on ECS Fargate (ARM64). It runs MicroVMs for signed-in users, bridges their shell to the browser,
and streams the iron-proxy audit log and Route 53 Resolver query log to the UI. API shapes: SPEC.md
section 2 plus [API.md](API.md).

```
app/config.py       env settings; refuses DEV_AUTH when ORIGIN_SECRET is set
app/auth.py         Cognito ID token verification (RS256, JWKS, iss/aud/token_use/exp)
app/store.py        ownership records: DynamoDB (pk sub, sk id) or in-memory for local dev
app/microvm.py      lambda-microvms control plane, run-hook payload, endpoint/shell tokens
app/terminal.py     browser <-> MicroVM shell bridge (resize, tmux, suspend/resume, reconnect)
app/events.py       one shared CloudWatch Logs Live Tail per process, run while a page shows a feed panel
                    (polling fallback), fan-out
app/audit.py        iron-proxy / DNS log lines -> UI events (secret ARN -> name)
app/credentials.py  per-secret usage from the audit log; revoke/restore via a resource policy
app/policy.py       proxy control plane: the policy in part 2's control bucket (allowlist, credentials,
                    AWS scope), apply status from the sidecar, the change log
app/aws_reference.py  AWS's Service Authorization Reference and the ReadOnlyAccess ceiling, fetched and
                    cached for an hour (stale-on-error)
app/aws_catalog.py  the AWS access catalog: levels, per-role ceiling, IAM prefix -> signing names and hosts,
                    search, session policy, the Secrets Manager guard
app/aws_roles.py    the sandbox roles' policies and the local evaluator
app/aws_endpoints.py  boto3 client -> signing name and us-east-1 host, generated from botocore at image build
                    (app/data/aws_endpoints.json)
app/pricing.py      Claude token prices for the cost estimate (source and date in the file)
app/main.py         routes, origin-header check, WebSocket handlers
```

## Run locally

```bash
uv sync
uv run pytest                      # unit tests: JWT, ownership, payload, terminal bridge, audit, pricing, log feed,
                                   # proxy policy (fake S3, Secrets Manager and change log)
DEV_AUTH=1 IMAGE_ARN=... CONNECTOR_ARN=... \
  PLACEHOLDERS_JSON='{...}' SECRET_ARNS_JSON='{...}' PROXY_LOG_GROUP_ARN=arn:aws:logs:...:log-group:/my2cents/iron-proxy \
  uv run uvicorn app.main:create_app --factory --port 8000
uv run python scripts/integration.py   # real AWS end to end (one MicroVM), writes ../results/backend_integration.log
uv run python scripts/cognito_check.py # production-mode auth against the real Cognito pool (throwaway user)
```

`DEV_AUTH=1` makes every caller the fake user `dev-user-0000` and is refused when `ORIGIN_SECRET` is set.
Without `TABLE_NAME` the ownership store is in memory. In production `ORIGIN_SECRET`,
`COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID` and `TABLE_NAME` are required, or the app won't start.

## Environment

| Variable | Meaning |
|---|---|
| `TABLE_NAME` | DynamoDB table (pk `sub`, sk `id`) |
| `COGNITO_USER_POOL_ID`, `COGNITO_CLIENT_ID`, `AWS_REGION` | ID token verification |
| `IMAGE_ARN`, `CONNECTOR_ARN` | MicroVM image and VPC egress connector |
| `PLACEHOLDERS_JSON` | the run-hook payload's placeholders. There are no proxy settings: part 2's sandbox VPC sends every name to iron-proxy (`PROXY_MODE` is `transparent`, the only value) |
| `SECRET_ARNS_JSON`, `TASK_ROLE_ARN` | credential names, and the iron-proxy role that revoke denies |
| `PROXY_ECS_CLUSTER`, `PROXY_ECS_SERVICE` | iron-proxy's ECS service, restarted by revoke (default `my2cents-ip` / `iron-proxy`) |
| `PROXY_LOG_GROUP_ARN`, `DNS_LOG_GROUP_ARN` | iron-proxy's audit log and part 2's sandbox-VPC Resolver query log, for /ws/events and /api/credentials (ARN or name) |
| `ORIGIN_SECRET` | required `X-Origin-Verify` value (all paths except `/healthz`) |
| `SERVICE_REFERENCE_URL`, `CATALOG_TTL_S`, `CATALOG_CACHE_DIR` | the AWS access catalog's source (default AWS's Service Authorization Reference), cache lifetime (3600) and disk cache (`/tmp/aws-catalog`; empty for none) |
| `CHANGES_TABLE`, `ADMIN_GROUP` | proxy policy change log (pk `pk`, sk `sk`), and the Cognito group allowed to change the policy (`admins`) |
| `CONTROL_BUCKET`, `CONTROL_KEYS_JSON`, `ROLES_JSON`, `SCOPE_BASELINE_JSON`, `USER_SECRET_PREFIX` | part 2's control plane outputs; without `CONTROL_BUCKET` the policy routes answer 503 |
| optional | `DEV_AUTH`, `QUOTA_GB` (8), `VM_MEMORY_GB` (2), `MAX_DURATION_S` (28800), `PROXY_ALLOWLIST`, `CORS_ORIGINS`, `TERMINAL_KEEPALIVE_S` (20) |

## Behaviour worth knowing

- Only control-plane calls (`get_microvm`) run in the background. Agent calls (`/stats`, `/claude-usage`)
  and the shell count as endpoint traffic and keep a MicroVM awake, so they happen only on request.
  `vitals?live=1` skips the agent when the sandbox isn't RUNNING.
- The run-hook payload carries `SANDBOX_NAME` (the sandbox name, reduced to characters that are
  safe in a shell and in tmux formats) next to the proxy variables and placeholders.
- After a suspend or resume, the backend polls `get_microvm` every 0.25 s until the end state. It pushes
  each change to `/ws/events` and wakes the attached terminal. Measured through CloudFront, the pill
  showed SUSPENDED after about 0.5 s and the terminal reattached about 0.7 s after Resume
  (`scripts/measure_deployed.py`).
- An attached terminal keeps the MicroVM awake: the upstream shell WebSocket sends keepalive pings
  every 20 s.
- Opening a terminal on a SUSPENDED sandbox calls `resume_microvm` first. When a suspend happens while
  the terminal is attached, the bridge reports `suspended`. It waits until the sandbox is RUNNING again,
  then reconnects and reattaches tmux. Other unexpected closes are retried with backoff for up to 60 s.
- **Live Tail costs money, so it only runs while someone is looking at a feed.** CloudWatch charges
  $0.01 per minute of Live Tail session after 1,800 free minutes a month (CloudWatch pricing page,
  checked 2026-11-03). A session left running 24/7 would cost about $14/day. An open `/ws/events`
  connection alone starts nothing: each one says whether its page shows a feed panel
  (`{"type":"feeds","on":true|false}`; the browser sends `true` while the side panel is open on Proxy or
  DNS and the page is visible). One shared session per backend starts when the first connection says
  `true` and stops 60 s after the last one says `false` or disconnects (INFO lines `log feed: starting
  Live Tail`, `log feed: no feed panel open for 60 s`, `log feed: stopping Live Tail`). Lifecycle events
  flow either way. When it restarts, `FilterLogEvents` backfills what was written in the gap (up to 10
  minutes back) at once, before `StartLiveTail` answers, and again once the session is up, so the feed has
  no hole and no duplicates. A session that ends on its own is restarted while interest remains. If Live
  Tail errors or hangs (`StartLiveTail` sometimes takes 10+ s to answer), the backend polls
  `FilterLogEvents` every 3 s for 2 minutes and then tries Live Tail again.
- The Credentials panel doesn't use the tail. `GET /api/credentials` runs its own `FilterLogEvents` query
  (the proxy log's last 15 minutes, filtered on `swapped`, cached 10 s), which the panel polls every 15 s
  while it's shown and the page is visible.
- **Revoke actually restarts the proxy.** Part 2 measured that iron-proxy keeps injecting its cached value
  when a refresh fails, so a deny policy alone changes nothing. `POST .../revoke` puts the deny statement
  (Sid `My2centsWorkspaceRevoke`; other statements are kept), then forces a new deployment of part 2's
  `iron-proxy` service (`ecs:UpdateService`, allowed on that one service only). If the restart can't be
  started, the policy is removed again and the call fails. Status is `revoking` until that deployment is
  the only one and COMPLETED, then `revoked`. The new task starts without the secret, forwards the
  placeholder unswapped, and the upstream rejects it (Anthropic answers 401).
- **Restore** only removes the deny statement, with no restart. Status is `restoring` until the audit log
  shows a swap of that secret again, or 90 s pass.
- The backend never reads secret values. It has no `GetSecretValue` permission.
- **With part 2's control plane on**, revoke and restore don't restart anything: the deny names both the task
  role and all four sandbox roles (iron-proxy fetches secrets as whichever is in use), and the backend writes a new
  config version. The sidecar's `POST /v1/reload` rebuilds iron-proxy's secrets cache, so the cached value is
  gone within a few seconds. Status is `revoking` until the sidecar reports that version applied.

## Proxy policy (control plane)

Admins (Cognito group `admins`, from the ID token's `cognito:groups`) edit iron-proxy's policy; everyone else
can read it. Every change is global: it applies to every sandbox on the proxy. Routes and shapes are in
[API.md](API.md#proxy-policy-control-plane).

- **Edits.** The backend reads `iron-proxy.yaml` from part 2's control bucket, changes only the allowlist's
  `domains`, the `secrets` list or `aws_auth.allowed_services` (round-tripped with `ruamel.yaml`, so comments
  and formatting stay), bumps the version in the first line and writes it back with `If-Match` on the ETag it
  read. A concurrent edit is retried once, then refused with 409.
- **Apply status.** Part 2's control sidecar polls the bucket every 2 s, swaps the file in, calls iron-proxy's
  `POST /v1/reload` and writes `status.json`. `GET /api/policy/status` compares that with what's in the
  bucket and reports the latency from the write to the reload.
- **Credentials.** The value goes from the request straight to `secretsmanager:CreateSecret` as
  `my2cents-ip/user/<name>`: the request model keeps it in a `SecretStr`, validation errors never echo input,
  and it isn't logged or stored anywhere else. The backend generates a placeholder, adds a replace-mode
  `aws_sm` entry (30 s ttl, 5 s failure ttl) and keeps the env var name in `credentials.json`. New sandboxes
  get the placeholder in that env var. Rotate is `put_secret_value` only (no reload: iron-proxy refetches
  within its 30 s ttl). Delete removes the entry and schedules the secret's deletion (7-day recovery).
- **AWS access.** Sandbox AWS calls are signed as one of part 2's four roles: `view-only` (the default),
  `read-only`, `data-reader` or `builder` (PowerUserAccess with guardrails; it can create billable resources). By
  default no session policy is sent, so the session has the role's full permissions; an admin can switch one on to
  narrow it to chosen actions. The catalog is every service and action in AWS's
  [Service Authorization Reference](https://servicereference.us-east-1.amazonaws.com/) (457 services, 22,367 actions
  on 2026-11-04), fetched in parallel (about 17 MB), kept in memory and in `/tmp/aws-catalog` for an hour, and
  evaluated for the selected role: the backend reads each role's attached managed and inline policies
  (`iam:ListAttachedRolePolicies`, `GetPolicyVersion`, `ListRolePolicies`, `GetRolePolicy`, cached an hour too) and
  evaluates every action locally (`Action`/`NotAction` wildcards; only unconditional, account-wide Denies grey an
  action out; Allows tied to resources or conditions mark it `limited`). Secrets Manager actions and
  `ssm:GetParameter*` are never selectable. Services map to iron-proxy through the reference's `Operations[].SDK`
  entries (or a client named after the prefix) and botocore's signing names and us-east-1 endpoints (only
  `amazonaws.com` ones, which is what `aws_auth` re-signs here).
  **The Secrets Manager guard:** whatever the role and the session policy, `aws_auth.allowed_services` never contains
  `secretsmanager` and the allowlist never holds a Secrets Manager endpoint. With no session policy the backend writes
  explicit lists (every signing name and host it can map, minus Secrets Manager), because iron-proxy 0.55.0 takes the
  service from the client's credential scope and reads an empty list as every service; the config writer checks again
  whatever it is given. With a session policy, both follow the chosen services, and the policy's minified size is
  checked against STS's 2,048 characters.
- **Change log.** Every write records who, when, what and the versions in DynamoDB; never a value.
