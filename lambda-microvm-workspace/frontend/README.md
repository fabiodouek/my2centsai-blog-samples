# Frontend: Lambda MicroVM Workspace UI

React + TypeScript + Vite single-page app, built to static files in `dist/` and served from S3 through
CloudFront. It talks to the backend's REST API under `/api/*` and its WebSockets under `/ws/*` (same
origin by default).

What's on the screen:

- **Sidebar**: your sandboxes (state, an 8 h lifetime bar on one shared scale, time left, idle countdown when
  running with no terminal attached), **New** (name, idle-suspend minutes, lifetime), and the account's MicroVM
  memory quota as one cell per 2 GB sandbox.
- **Terminal** (xterm.js): one tmux session per sandbox. The overlay shows connecting, suspended (with
  **Resume**), reconnecting (with a countdown), detached, shell exited, terminated and error states. Toolbar:
  **Start Claude Code** (types `claude` and Enter; unavailable while the tmux pane runs Claude Code or anything
  other than a shell, per the `pane` field of `vitals?live=1`), Suspend/Resume, Detach/Attach, Copy, Terminate (confirm),
  and a toggle for the side panel. Dragging the splitter resizes the terminal and sends a resize to the shell.
- **Side panel**: Proxy feed (filters, requests per minute by host), Credentials (revoke/restore with a confirm
  that says it affects every sandbox), Claude Code token usage, Vitals and timers (CPU and memory charts, idle
  policy, lifecycle timeline), and DNS queries (by default only lookups from the sandbox subnets; **Everything**
  adds the workspace backend's and iron-proxy's own; click a row for the full name and source). Proxy rows
  where iron-proxy's `aws_auth` acted carry an **aws** badge with the service (oxide when it refused the call).
- **Proxy policy** (sidebar entry above the quota meter; `#policy/allowlist|credentials|aws|changes`): the
  global iron-proxy policy, for every sandbox on the proxy, when part 2 runs with `control_plane=true`.
  Allowlist editor (AWS endpoints are locked to AWS access), credentials (add with a write-only value, rotate,
  revoke/restore by reload, delete), AWS access, and the change log. AWS access starts with the role sandbox
  AWS calls are signed as (`view-only`, the default; `read-only`; `data-reader`; `builder`, with a warning that it
  can create billable resources) and a "Narrow with a session policy" switch, off by default: off, sandboxes get
  the role's full permissions and the page shows how many services and hosts iron-proxy lets through (everything
  but Secrets Manager). On, it lists every service and action in AWS's Service Authorization Reference (fetched
  and cached by the backend), evaluated for the selected role: a search box over service and action names with
  an access-level filter (List, Read, Write, Permissions, Tagging) searches on the server; a service's actions
  load when it is opened. Actions outside the role, blocked ones (Secrets Manager, `ssm:GetParameter*`) and
  services with no endpoint mapping are listed but disabled, with the reason; actions the role allows only on
  some resources are tagged "limited". The selection shows as chips, next to a live session-policy preview with
  its size against the 2,048-character STS limit, and Apply. A status line follows each change until the
  proxy's control sidecar reports it applied ("Applied in 1.9 s · view-only · session ws-view-only-v29 · no
  session policy · token 18% · expires 02:40"). Only members of the Cognito `admins` group can change anything; everyone else
  sees it read-only. The terminal stays connected (hidden) while the page is open.

Live CPU/memory (`vitals?live=1`) and Claude Code usage are only requested while a terminal for that sandbox
is attached in this browser, because any request to the MicroVM endpoint keeps it awake. Everything else uses
the control plane or CloudWatch Logs and never wakes a sandbox.

## Run it

```bash
npm install
npm run dev:mock      # mock mode, no backend or AWS needed: http://localhost:5173
npm run dev           # real backend: needs public/config.json (below) and the API reachable
npm run build         # type-check + production build into dist/
npm run preview       # serve dist/ locally
```

### Mock mode (`VITE_MOCK=1`)

`npm run dev:mock` (or `VITE_MOCK=1 npm run dev`) starts the app with:

- **MSW** answering every REST route with an in-memory simulation (sandboxes that start, suspend after their
  idle time, resume, terminate; quota; credentials computed from the fake proxy log; vitals; Claude usage).
- **Fake WebSockets** for `/ws/events` (proxy, DNS and lifecycle events, heartbeats; the feed status follows
  `feeds` messages like the backend's) and `/ws/terminal/{id}` (a small fake shell inside tmux-style chrome
  that echoes keystrokes).

In the fake terminal, type `help`. Useful commands: `curl <url>`, `git pull`, `npm install`, `claude` (a fake
Claude Code session; each prompt adds tokens and proxy traffic), `exit`, and two failure drills:
`mock-drop-upstream` (the backend loses the MicroVM and retries) and `mock-drop-socket` (the browser's socket
drops and the client reconnects). Revoke the `anthropic` credential, then send a prompt in the fake Claude
session to see the 401.

The proxy policy endpoints are simulated too (`src/mock/policy.ts`): writes bump the config/scope version and a
fake sidecar applies them 1.5 to 2.5 s later.

Scenarios: add `?mock=empty` for a first-run account with no sandboxes, `?mock=full` to start with the 8 GB
quota full, `?viewer=1` to see the workspace as a non-admin. Mock code is loaded only when `VITE_MOCK=1`; production builds don't contain it. All mock data is
synthetic and the sidebar shows a "Mock data" badge.

### Against a backend

The dev server proxies `/api` and `/ws` to `BACKEND_URL` (default `http://localhost:8000`, i.e. `uvicorn`).
If the backend enforces `X-Origin-Verify`, set `ORIGIN_SECRET` and the proxy adds the header:

```bash
cp config.example.json public/config.json     # fill in the Cognito values
BACKEND_URL=http://localhost:8000 ORIGIN_SECRET=... npm run dev
# or against the deployed stack (CloudFront adds the origin header itself):
BACKEND_URL=https://<cloudfront domain> npm run dev
```

Sign-in uses the Cognito managed login, so `http://localhost:5173/` must be an allowed callback and logout URL
on the app client (the infra adds it).

## `config.json`

The app reads `/config.json` at startup. `scripts/deploy.sh` writes it into `dist/` after the build; for local
dev put it in `public/config.json` (git-ignored). Shape:

```json
{
  "region": "us-east-1",
  "userPoolId": "us-east-1_XXXXXXXXX",
  "clientId": "xxxxxxxxxxxxxxxxxxxxxxxxxx",
  "cognitoDomain": "my2cents-ws-xxxxxxxx.auth.us-east-1.amazoncognito.com",
  "apiBase": ""
}
```

- `cognitoDomain`: the managed login host, with or without `https://`.
- `apiBase`: empty for same origin (CloudFront routes `/api/*` and `/ws/*` to the ALB); otherwise a full
  origin such as `https://api.example.com`, and WebSockets use the matching `wss://` origin.

## Auth and API notes

- OIDC authorization code flow with PKCE (`oidc-client-ts`), scopes `openid email`, redirect URI = the site
  origin + `/`. Tokens are kept in `localStorage` and refreshed with the refresh token. REST calls send
  `Authorization: Bearer <ID token>`; a 401 refreshes once, then sends you to sign in. Sign out clears the
  session and goes to Cognito's `/logout`.
- WebSockets send `{"type":"auth","token":"<ID token>"}` first. Both sockets send `{"type":"ping"}` every
  25 s (CloudFront drops idle connections after 60 s) and accept the backend's `heartbeat`/`pong`; once the
  server has shown it heartbeats, 60 s of silence counts as a dead connection and triggers a reconnect.
- Events: the backend tails the proxy and DNS logs (CloudWatch Logs Live Tail, billed per minute) only while
  a page asks for it, so the hub sends `{"type":"feeds","on":true}` while a Proxy or DNS panel is mounted
  (side panel open on that tab) and the page is visible, `on:false` when that ends, and the current value
  again after every reconnect. Switching Proxy to DNS sends nothing. A feed panel opened while the backend's
  tail is idle shows **Starting…** (and, if it has no rows yet, a backfilling placeholder instead of the
  empty state) until the status says `live_tail`. Credentials polls `GET /api/credentials` and doesn't need
  the feed.
- Terminal: keystrokes go up as binary frames, `{"type":"resize","rows","cols"}` right after auth and on every
  size change; output comes down as binary. Status frames drive the overlay. After a suspend the backend keeps
  the socket open and reattaches tmux itself once the sandbox runs again, so **Resume** only calls
  `POST /resume` (it opens a new socket only if the old one is gone).
- Response parsing is tolerant: snake_case or camelCase, ISO or epoch timestamps.

## Theming tmux in the sandbox image

tmux's default status line is a saturated green band, which fights the UI's palette. The mock draws it the
way the image should configure it; these lines in the sandbox image's `tmux.conf` match:

```
set -g status-style "bg=#211d1a,fg=#969189"
set -g window-status-current-style "fg=#e1b761"
```

## Layout of the code

```
src/
  lib/        config, auth (oidc-client-ts), api (REST + normalizers), events hub (/ws/events),
              terminalConnection (/ws/terminal), store (incl. the #policy route), policy (apply-status
              polling), polling and live-stat hooks, formatting
  components/ Workspace layout, Sidebar, Workbench (toolbar + xterm + overlay), RightPanel and panels/,
              policy/ (the Proxy policy page), charts/ (uPlot line charts, SVG requests-per-minute columns,
              lifetime bar), ui/ primitives
  mock/       MSW handlers, in-memory simulation, proxy policy, fake WebSocket + fake shell (mock mode only)
  styles/     tokens.css (palette, type, spacing) and app.css
```
