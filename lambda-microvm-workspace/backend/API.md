# Workspace API: concrete shapes

The routes and payloads in SPEC.md section 2 are implemented as written. This file pins down what the
spec leaves open and lists the few additions. Additions are extra fields or extra message types; a client
that ignores unknown fields and unknown `type`s works unchanged.

## Conventions

- Timestamps are ISO 8601 UTC strings with milliseconds and a `Z` (`2026-11-03T18:00:00.123Z`), or `null`.
- Errors are `{"detail": "<message>"}` with 401 (no/invalid token), 403 (missing `X-Origin-Verify`),
  404 (not found or not yours, so other users' sandboxes look the same as missing ones), 409 (quota,
  wrong state), 502/504 (in-VM agent failed or timed out), 503 (backend not configured). A 422 from
  request validation has FastAPI's shape: `detail` is a list of `{loc, msg, type}`.

## REST

`GET /api/config` adds `vm_memory_gb` (2), `default_idle_minutes` (15), `proxy_allowlist` (the proxy's
allowed domains, `*.` wildcards, for the DNS tab), `sandbox_cidrs` (part 2's connector subnets, the only addresses in
its sandbox VPC: every query in the DNS feed comes from one; `[]` when unknown), `feed_scope_note`, `secrets` (names)
and `dev_auth`. `proxy_mode` is `"transparent"`: sandboxes have no proxy settings, because part 2's sandbox VPC
answers every name with iron-proxy's load balancer (DNS Firewall).

The run hook payload (`POST /api/sandboxes`) carries the placeholders (built-ins and every custom credential's),
placeholder `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, `AWS_REGION`, the CA variables (`NODE_EXTRA_CA_CERTS`,
`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `AWS_CA_BUNDLE`), `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` (the CLI's
default aws-chunked uploads are refused by iron-proxy), `SANDBOX_NAME` and `AWS_SDK_UA_APP_ID=ws-<name>`; no proxy
variables and `nameserver: null`.
`pricing` = `{"source","as_of","note","unit","notes","models":{"<model id>":{"input","output","cache_read","cache_write"}}}`
in USD per million tokens. `source` is "Anthropic", `note` is the user-facing sentence ("Estimated from
Anthropic list prices as of 2026-11-03"); `/claude-usage` carries the same `pricing: {source, as_of, note}`.

Sandbox item (list, create, suspend, resume, delete, and the base of vitals):

```json
{"id": "microvm-...", "name": "alpha", "state": "RUNNING", "created_at": "...", "started_at": "...",
 "max_duration_s": 28800, "idle_minutes": 15, "image_version": "1.0",
 "expires_at": "...", "state_reason": null, "attached": false, "last_activity_at": null}
```

- Added: `expires_at` (started_at + max_duration_s), `state_reason` (the control plane's text; a terminate
  reports "Success."), `terminated_at` (when the backend saw it end; `null` unless TERMINATING/TERMINATED),
  `attached` (a terminal is open in
  this backend now), `last_activity_at` (last terminal attach/detach/keystroke or live agent call; use it
  with `idle_minutes` for the idle countdown; `null` = none since the backend started).
- TERMINATED sandboxes stay in the list for an hour, then disappear.
- `POST /api/sandboxes` returns 201 with state `PENDING`. 422 if `idle_minutes` >= the max duration.
- Suspend and resume wait (up to 3 s, polling the control plane every 0.25 s) for the end state and
  return it: `SUSPENDED` in ~0.5 s, `RUNNING` in ~0.8 s. Each change is pushed as a `lifecycle` event the
  moment it's seen, and an attached terminal is woken at once. Delete returns `TERMINATING` right away.
  If the end state isn't reached in time, the item shows the transition (`SUSPENDING`, `PENDING`) and a
  background poll (every 0.5 s, up to 30 s) pushes the rest. 409 `{"detail":"can't suspend now: ..."}` when the state doesn't allow it.

`GET /api/sandboxes/{id}/vitals?live=0|1` = sandbox item plus:

```json
{"sampled_at": "...", "remaining_s": 27000, "uptime_s": 1800,
 "idle_policy": {"auto_resume": true, "max_idle_s": 900, "suspended_duration_s": 28800},
 "stats": null, "stats_error": null, "pane": null}
```

With `live=1` and state RUNNING, `stats` is the agent's `/stats` object. Otherwise `stats` is `null` and
`stats_error` says why (not RUNNING: the backend won't wake a suspended sandbox for stats; or the agent
failed). `live=1` never returns an HTTP error for agent problems.

With `live=1` and state RUNNING, `pane` says what runs in the terminal's tmux pane (session `main`), read in the
same round trip through the agent's `/exec`: `{"command": "claude", "claude_running": true, "at_shell": false}`.
`command` is tmux's `pane_current_command`; `at_shell` means a shell prompt is in the foreground. `null` when no
terminal has started tmux yet, when not live, or when the probe failed. The UI disables Start Claude Code while
`claude_running` is true.

`GET /api/sandboxes/{id}/claude-usage` = the agent's payload with per-session `est_cost_usd` (null when
the model isn't in the pricing table) and `priced_as`, plus `totals.est_cost_usd`, `unpriced_models`,
`pricing: {source, as_of}`. 409 if the sandbox isn't RUNNING.

`GET /api/quota` = `{"used_gb","quota_gb"}` plus `vm_memory_gb`, `user_microvms`, `user_gb` (this
user's sandboxes that aren't TERMINATED, TERMINATING included, read from `get_microvm`), `account_microvms`,
`other_microvms` (live MicroVMs that aren't this user's, from a 10 s cached `list_microvms`) and `scope`
("account": `used_gb` counts every live MicroVM in the account, since the quota is per account). The user's own
sandboxes never come from the cached list, so one they just terminated isn't counted as another user's.

`GET /api/credentials` = a JSON array, one object per secret:
`{"name","placeholder_prefix","revoked","status","status_detail","status_since","uses_15m","hosts":{host:n},"last_used"}`.

- `status` is `active`, `revoking`, `revoked`, `restoring` or `unknown` (policy unreadable).
- `revoked` is `true`/`false`, `"pending"` while revoking, or `null` when unknown.
- `status_detail` explains a pending state, e.g. "iron-proxy restarting: new task 0/1 running, 1 old task(s) still
  serving the cached key". `status_since` is when the action started.
- Refresh about every 5 s while something is pending.

`POST /api/credentials/{name}/revoke` puts the deny policy and restarts iron-proxy. It returns
`{"name","state":"revoking","revoked":"pending","effective_after_s":180,"since","deployment_id","detail","warning"}`.
502 means nothing changed: if the restart couldn't be started, the policy is removed again.
`POST .../restore` removes the policy (no restart) and returns
`{"name","state":"restoring","revoked":false,"effective_after_s":60,"since","detail","warning"}`.

## WebSockets

Both: the first client message must be `{"type":"auth","token":"<id token>"}` within 5 s.
Close codes: `4401` auth missing/invalid/late, `4404` sandbox not found or not yours, `4410` sandbox
terminated, `1011` gave up reconnecting / internal error, `1000` shell exited normally.
The server sends `{"type":"heartbeat"}` every 25 s (CloudFront drops idle WebSockets after 60 s);
clients should ignore it and send `{"type":"ping"}` about every 25 s themselves (answered with
`{"type":"pong"}`).

`/ws/terminal/{id}`: browser sends binary keystrokes, `{"type":"resize","rows","cols"}` (send one right
after auth; the backend waits up to 1.5 s for it before starting tmux), or `{"type":"input","data":"..."}`
(text, same as binary keystrokes). Server sends binary output and
`{"type":"status","state":"connecting|attached|reconnecting|suspended|exited","detail":"..."}` with
optional `session_id` (attached), `attempt`, `retry_in_s`, `gives_up_in_s` (reconnecting), `vm_state`.
Opening a terminal on a SUSPENDED sandbox resumes it. A suspend seen while attached reports `suspended`
and waits; once the sandbox is RUNNING again (e.g. after `POST .../resume`) the backend reconnects and
reattaches tmux by itself, no new WebSocket needed. `exited` is followed by a close.

`/ws/events`: first message after auth is `{"type":"status","source":"idle|starting|live_tail|polling|waiting|disabled","log_groups":[...],"missing_log_groups":[...]}`,
then up to 300 recent proxy/DNS events (replayed), then live events. `proxy` events add `mode` and
`duration_ms`; `secrets` holds names (anthropic/github/demo, or `unknown`). `lifecycle` events add `name`,
`ts` and `initial` (true for the first snapshot of each sandbox, sent with `prev: null` on connect).

Lifecycle events flow from the start. The proxy/DNS log tail (CloudWatch Logs Live Tail, billed per minute)
runs only while at least one connection wants it. The client says so with `{"type":"feeds","on":true}` when
its page shows a feed panel (the UI: side panel open on Proxy or DNS, page visible) and
`{"type":"feeds","on":false}` when it stops. A new connection starts at `false`, so send the current value again
after every reconnect. Only a JSON `true` counts as on, and repeats are harmless. The backend starts the tail when
the first connection turns on and stops it 60 s after the last one turns off or disconnects; interest that
returns within those 60 s keeps the same session. Every connection gets a `status` event on each change:
`starting` while it backfills (on a restart, what was logged since the tail stopped, up to 10 minutes back;
on the first start, the last 10 minutes) and opens the Live Tail session, then `live_tail` (or `polling`,
`waiting`), and `idle` once stopped. While the tail runs, proxy and DNS events go to every connection,
including ones that didn't ask.

## Proxy policy (control plane)

Admins can change iron-proxy's policy from the workspace: the allowlist, the credentials it injects, and the
AWS access it signs sandbox calls with. Everything here is global: it applies to every sandbox on the proxy.
It needs part 2 applied with `control_plane=true` (iron-proxy then reads its config from an S3 bucket through a
`control` sidecar, which re-assumes the scoped role and calls iron-proxy's `POST /v1/reload`).

**Admins.** A caller is an admin when the Cognito ID token's `cognito:groups` claim contains `admins` (env
`ADMIN_GROUP`). `GET /api/me` adds `groups` (array) and `is_admin` (bool). Every write in this section, and
revoke/restore under `/api/credentials`, returns 403 `{"detail":"admins only: ..."}` for anyone else; reads are
open to every signed-in user. With `DEV_AUTH=1` the fake user is an admin.

**The seed.** Part 2 seeds `aws-scope.json` with the default role, no session policy and `seed: true`, and the config
with the `sts`, `s3`, `ecs` lists. The first time the backend has read the AWS catalog it writes the matching lists for
that role (every mappable service but Secrets Manager), once; the scope's `updated_by` becomes `workspace (seed)`.

**Control plane off.** `GET /api/policy` returns `{"enabled": false, "reason": "..."}`; every other
`/api/policy/*` route returns 503 with the same reason. Revoke/restore keep part 2's restart method.

**How a change applies.** The backend reads `iron-proxy.yaml` from S3, changes only the allowlist's
`domains`, the `secrets` list or `aws_auth.allowed_services`, keeps everything else (comments included), bumps the
version in the first line (`# my2cents control plane: config v13 ...`) and writes it back with
`If-Match` on the ETag it read (a concurrent edit gets 409 `"the policy changed meanwhile; reload and retry"`).
The sidecar polls the bucket about every 2 s, swaps the file in, and reloads iron-proxy. Every write response
carries `apply`:

```json
{"config_version": 13, "scope_version": 4, "requested_at": "2026-11-03T22:10:00.123Z"}
```

and the UI polls `GET /api/policy/status` until `applied` is true. Validation errors (422) never echo request
values: `detail` is a list of `{loc, msg, type}` without FastAPI's `input` and `ctx`.

### Change entry

Every write records one entry in DynamoDB (`CHANGES_TABLE`) and returns it as `change`:

```json
{"id": "2026-11-03T22:10:00.123Z#3f9a0c", "at": "2026-11-03T22:10:00.123Z", "actor": "demo@my2cents.ai",
 "kind": "allowlist.add", "summary": "Allowed example.com", "detail": {"host": "example.com"},
 "config_version": 13, "scope_version": 4}
```

`kind` is one of `allowlist.add`, `allowlist.remove`, `credential.create`, `credential.rotate`,
`credential.delete`, `credential.revoke`, `credential.restore`, `aws.scope`. No secret value is ever stored.

### `GET /api/policy`

```json
{"enabled": true, "editable": true, "note": "Changes apply to every sandbox on this proxy.",
 "config_version": 13, "scope_version": 4, "region": "us-east-1",
 "limits": {"policy_chars": 2048, "run_payload_bytes": 4096, "run_payload_bytes_used": 1312}}
```

`editable` is `is_admin`. `run_payload_bytes_used` is the size of a new sandbox's run hook payload with every
custom credential's placeholder in it.

### Allowlist

- `GET /api/policy/allowlist` → `{"version": 13, "hosts": [{"host": "api.anthropic.com", "managed": null},
  {"host": "sts.us-east-1.amazonaws.com", "managed": "aws", "service": "sts"}]}`. Hosts under
  `amazonaws.com` are managed by AWS access (`managed: "aws"`): they follow the chosen services and can't be
  added or removed here.
- `POST /api/policy/allowlist` `{"host": "example.com"}` → 201 `{"version", "host", "change", "apply"}`.
  The host is lower-cased and a trailing dot dropped. 422 when it is an IP literal, a bare `*`, a wildcard that
  isn't a leading `*.` over at least two labels or that covers a public suffix (`*.com`, `*.co.uk`,
  `*.github.io`...), has a scheme, port, path or invalid characters, is under `amazonaws.com`, or names a
  Secrets Manager endpoint. 409 when it is already on the list. `*.example.com` also matches `example.com`.
- `DELETE /api/policy/allowlist/{host}` → 200 `{"version", "host", "change", "apply"}`. 404 when it isn't on
  the list, 409 when it is AWS-managed.

### Credentials

`GET /api/policy/credentials` → array, built-ins first:

```json
{"name": "httpbin-test", "kind": "custom", "env": "TEST_KEY",
 "placeholder": "m2c-ph-httpbin-test-3f9a0c1d2e4b5a6f", "hosts": ["httpbin.org"],
 "headers": ["X-Test-Key"], "query": false, "body": false, "secret_name": "my2cents-ip/user/httpbin-test",
 "status": "active", "revoked": false, "status_detail": null, "on_allowlist": true,
 "created_at": "...", "created_by": "demo@my2cents.ai", "rotated_at": null, "rotated_by": null,
 "rotatable": true, "deletable": true}
```

Built-ins (`anthropic`, `github`, `demo`) have `kind: "builtin"`, their part 2 env var
(`ANTHROPIC_API_KEY`, `GH_TOKEN`, `DEMO_TOKEN`), `rotatable: false` and `deletable: false`: their values come
from part 2's `.env`. `headers: []` means iron-proxy scans every header. `status`/`revoked` follow the
`/api/credentials` rules below. A value is never returned.

- `POST /api/policy/credentials`
  `{"name", "env", "hosts": [...], "headers": [...], "query": false, "body": false, "value": "..."}` → 201
  `{"credential", "change", "apply", "warnings": [...]}`.
  - `name`: `^[a-z][a-z0-9-]{1,31}$`, not a built-in or existing name.
  - `env`: `^[A-Z_][A-Z0-9_]{0,63}$`, not used by another credential and not reserved (proxy, AWS, `PATH`,
    `HOME`, `SANDBOX_NAME`...).
  - `hosts`: 1 to 10, validated like allowlist entries (AWS hosts refused). A host that isn't on the allowlist
    gives a warning, since iron-proxy refuses it before it could swap anything.
  - `headers`: up to 10 header names (`[]` = every header); `query` and `body` add the URL query and the body.
  - `value`: 1 to 8192 characters. It goes straight into Secrets Manager as `my2cents-ip/user/<name>` and
    is never logged, stored elsewhere or echoed.
  - The backend generates a placeholder (`m2c-ph-<name>-<16 hex>`, not secret). New sandboxes get it in `env`.
    409 when the extra placeholder would push a new sandbox's run hook payload past 4,096 bytes.
- `POST /api/policy/credentials/{name}/rotate` `{"value": "..."}` → 200
  `{"name", "rotated_at", "effective_within_s": 30, "change"}`. `put_secret_value` only: no reload, iron-proxy
  refetches within its 30 s ttl. Custom credentials only (409 for built-ins).
- `DELETE /api/policy/credentials/{name}` → 200 `{"name", "deleted": true, "recovery_window_days": 7, "change",
  "apply"}`: out of the config, secret scheduled for deletion. Creating the same name again within the
  recovery window restores and overwrites it. Custom only.
- Revoke/restore stay at `POST /api/credentials/{name}/revoke|restore` (now admin-only, and they accept custom
  names). With the control plane on, they add the scoped role to the deny policy's principals and write a new
  config version instead of restarting iron-proxy: the reload rebuilds its secrets cache. The response adds
  `"method": "reload"` and `apply`, `effective_after_s` drops to 10, and `status` stays `revoking`/`restoring`
  until the sidecar reports that config version applied.

`GET /api/credentials` lists custom credentials too (`kind` added), and proxy feed events name them.

### AWS access

Sandbox AWS calls are re-signed by iron-proxy as one of four roles from part 2, picked here:

| Role | Permissions | Default |
|---|---|---|
| `view-only` | AWS `ViewOnlyAccess` (list and describe, no data reads) | yes |
| `read-only` | AWS `ReadOnlyAccess` (reads, including object and item data) | |
| `data-reader` | part 2's demo S3 bucket and DynamoDB tables `my2cents-demo-*`, read-only; nothing else | |
| `builder` | AWS `PowerUserAccess` with guardrails: us-east-1 only, no changes to the workspace's own stack (its buckets, ECS, network, load balancers, connectors, MicroVMs, tables, images, logs, parameters, CloudFront, Cognito, Resolver), no `sts:AssumeRole`. It can create billable resources. | |

Every role also has, inline: `secretsmanager:GetSecretValue` on the proxy's own secrets (iron-proxy fetches them with
the same session), an explicit Deny on `ssm:GetParameter*`, and Denies on every other Secrets Manager action or
secret. The scope picks a `role` and, optionally, a **session policy**: off (the default) sends no `Policy` to STS, so
the session has the role's full permissions; on, it narrows the role to the chosen actions (plus the baseline).

**Secrets Manager guard.** Whatever the role and policy, `aws_auth.allowed_services` never contains `secretsmanager`
and the allowlist never contains a Secrets Manager endpoint (iron-proxy takes the service from the client's credential
scope, and an empty list means every service). With the session policy off, the backend writes an explicit list:
every signing name the catalog can map, minus `secretsmanager`, and every mapped `amazonaws.com` host except Secrets
Manager's. With it on, both follow the chosen services.

**Catalog source.** Every service and action comes from AWS's machine-readable Service Authorization Reference
(`https://servicereference.us-east-1.amazonaws.com/`: an index of services, one JSON file each), fetched by the
backend. Access levels come from each action's annotations: `Permissions` (IsPermissionManagement), `Tagging`
(IsTaggingOnly), `Write` (IsWrite), `List` (IsList), otherwise `Read`.

**Ceiling = the selected role.** The backend reads each role's attached managed policies and inline policies
(`iam:ListAttachedRolePolicies`, `GetPolicyVersion`, `ListRolePolicies`, `GetRolePolicy`, cached for an hour) and
evaluates every action locally: `Action`/`NotAction` with wildcards, case-insensitive. An action is `allowed` when an
Allow matches it and no unconditional, account-wide (`Resource: "*"`) Deny does. `limited: true` means every Allow
that matches is tied to specific resources or conditions (data-reader's bucket, for instance), so it works only
there. Resource- or condition-specific Denies (builder's stack guardrails, the region lock) don't grey an action out.
Not allowed, with a `reason`: outside the role ("outside the view-only role's permissions"), denied by the role
("denied by the builder role (NoAssumeRole)"), every `secretsmanager` action and `ssm:GetParameter*` (blocked here
too), and services that can't be mapped.

**Mapping.** The reference uses IAM prefixes; iron-proxy needs SigV4 signing names and hosts. Each prefix is
joined to boto3 clients through the reference's own `Operations[].SDK` entries (operations whose first authorized
action belongs to that prefix), and each client to botocore's `signingName` and its us-east-1 endpoint (a file
generated from the installed botocore at build time). A service with no such client is listed with
`enabled: false` and reason "can't be enabled here (no endpoint mapping)".

**Caching.** The reference and the role policies are cached in memory (and on local disk) for an hour. An expired
copy is served while a refresh runs in the background; if a refresh fails the old copy stays, with `stale: true`, its
`age_s` and the `error`. Only a cold start with no copy waits for the fetch (503 if it fails). The three
catalog routes send `Cache-Control: private, max-age=3600`, `X-Catalog-Fetched-At` and `X-Catalog-Age` (seconds).

All three catalog routes take `?role=<id>` (default: the default role) and evaluate `allowed`/`reason` for it.

- `GET /api/policy/aws/catalog` → the service list, without actions:

  ```json
  {"region": "us-east-1",
   "source": {"url": "https://servicereference.us-east-1.amazonaws.com/", "fetched_at": "...", "age_s": 812,
              "stale": false, "error": null, "services": 457, "actions": 22367, "fetch_s": 4.1},
   "role": "view-only",
   "roles": [{"id": "view-only", "description": "...", "default": true, "warning": null},
             {"id": "builder", "description": "...", "default": false,
              "warning": "Can create billable resources in this account."}],
   "ceiling": {"role": "view-only", "policies": ["ViewOnlyAccess", "inline: sandbox-guards"], "fetched_at": "...",
               "age_s": 812, "stale": false},
   "levels": ["List", "Read", "Write", "Permissions", "Tagging"],
   "services": [{"id": "s3", "name": "S3", "actions": 180, "allowed": 74,
                 "levels": {"List": 21, "Read": 53, "Write": 92, "Permissions": 9, "Tagging": 5},
                 "enabled": true, "reason": null, "signing_names": ["s3"]}],
   "baseline": {<statement>}, "limits": {"policy_chars": 2048}}
  ```

  Services are sorted by `id`. `name` is the reference's `ServiceName` ("S3", "DynamoDB", "OpenSearch Service").
- `GET /api/policy/aws/services/{id}` → one service's actions, loaded on demand:

  ```json
  {"id": "s3", "name": "S3", "enabled": true, "reason": null, "signing_names": ["s3"],
   "hosts": ["*.s3.amazonaws.com", "s3.us-east-1.amazonaws.com", "*.s3.us-east-1.amazonaws.com"],
   "actions": [{"action": "s3:GetObject", "name": "GetObject", "level": "Read", "allowed": true, "limited": false,
                "reason": null}],
   "wildcards": [{"action": "s3:Get*", "label": "Every Get action", "covers": 63, "allowed": false,
                  "reason": "4 of the 63 Get actions are outside the read-only ceiling; pick them one by one"}]}
  ```

  Wildcards are offered for `List*`, `Describe*` and `Get*` when the service has such actions; one is `allowed`
  only when every action it covers is. 404 for an unknown id. (`*.host` also matches `host` in iron-proxy.)
- `GET /api/policy/aws/search?q=getobject&level=Read&limit=100` → case-insensitive substring search over action
  names (`GetObject` or `s3:GetObject`), service ids and service names; `level` filters actions (optional);
  `q` may be empty when `level` is set. Exact matches first, then prefix matches, then the rest.

  ```json
  {"q": "getobject", "level": "Read", "total": 31, "truncated": false,
   "services": [{"id": "s3", "name": "S3", "enabled": true, "matches": 9}],
   "actions": [{"action": "s3:GetObject", "service": "s3", "name": "GetObject", "level": "Read",
                "allowed": true, "reason": null}]}
  ```

  `services` lists services whose id or name matches, or that have matching actions (with `matches`). `limit`
  caps `actions` (1 to 500, default 100).
- `GET /api/policy/aws/scope` → `{"version": 4, "role": "view-only", "session_policy": false, "actions": [...],
  "services": [...], "updated_at", "updated_by", "preview": <preview>}`. `actions` is kept while the session policy is
  off, so switching it back on restores the last selection.
- `POST /api/policy/aws/preview` `{"role": "read-only", "session_policy": true, "actions": [...]}` → 200
  `{"role", "session_policy", "actions", "services", "allowed_services", "hosts", "policy": {...} | null,
  "policy_json": "<minified>" | null, "chars": 312, "limit": 2048, "fits": true}`. With the session policy on,
  `policy` is the full session policy, the proxy's baseline statement included, and actions are checked against
  the role (422 for unknown, not allowed, blocked or unmapped ones; the detail names them). With it off, `policy` is
  null, `chars` 0, and `allowed_services`/`hosts` are the explicit "everything but Secrets Manager" lists.
- `PUT /api/policy/aws/scope` `{"role", "session_policy", "actions"}` → 200 `{"scope_version", "config_version",
  "preview", "change", "apply", "unchanged": false}`: writes `aws-scope.json` and the config. 422 as for preview, for
  an unknown role, or when the policy is over 2,048 characters. The same request again → `unchanged: true`. With the
  session policy on and an empty list, `allowed_services` becomes `["none"]` (iron-proxy 0.55.0 treats an empty list
  as every service) and no AWS host stays on the allowlist.

### `GET /api/policy/status`

```json
{"enabled": true, "applied": true, "pending": false,
 "target": {"config_version": 13, "scope_version": 4, "requested_at": "..."},
 "latency_s": 2.1,
 "config": {"version": 13, "applied_at": "..."},
 "scope": {"version": 4, "role": "view-only", "session_policy": false, "session_name": "ws-view-only-v4", "source_identity": "demo@my2cents.ai",
           "expiration": "...", "assumed_at": "...", "session_token_utilization": 31,
           "session_token_size": 1290, "packed_policy_size": 30, "error": null},
 "reload": {"at": "...", "ok": true, "http_status": 200, "error": null},
 "sidecar": {"task": "3f2a...", "updated_at": "...", "heartbeat_age_s": 1.8, "stale": false},
 "error": null}
```

`scope` adds `role` and `session_policy` (what the sidecar assumed). `session_token_utilization` (percent of the maximum session token size) and `session_token_size` (bytes) are what
STS's AssumeRole returned for the current session; `packed_policy_size` is their deprecated predecessor, kept for
answers that don't carry them (all three `null` until the sidecar has assumed the role). They are separate from
the 2,048-character limit on the session policy's plaintext, which `preview` checks before anything is sent.
`applied` means the sidecar reports both target versions and a successful reload after them; `latency_s` is from
`requested_at` to that reload. `stale` is true when the sidecar hasn't written for 90 s. A config iron-proxy
rejected shows `reload.ok: false` with its validation error, and the proxy keeps the previous config.

### `GET /api/policy/changes?limit=50`

Newest first, up to 200: an array of change entries.

### Feed and new sandboxes

- `proxy` events add `aws`: `{"service": "s3", "region": "us-east-1", "signed": true, "rejected": null}`
  from the `aws_auth` annotations (`rejected` is e.g. `"service_not_allowed"`), or `null` when `aws_auth`
  didn't act.
- `POST /api/sandboxes` puts each custom credential's placeholder in its env var. If the payload would pass
  4,096 bytes it returns 409 `{"detail": "run hook payload would be N bytes (limit 4,096): ..."}`.
