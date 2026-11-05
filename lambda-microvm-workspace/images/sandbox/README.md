# Sandbox image v2 (`my2cents-ws-sandbox`, one per size)

The image every playground sandbox runs, built four times from the same code because a MicroVM's memory comes from its
image (`resources[].minimumMemoryInMiB`; vCPU = memory / 2 GB): `my2cents-ws-sandbox-1g` (Small, 1 GB),
`my2cents-ws-sandbox` (Medium, 2 GB, the default), `-4g` (Large, 4 GB) and `-8g` (X-Large, 8 GB). It starts from part 2's sandbox (Claude Code 2.1.317, gh, git, the
AWS CLI, no credentials, the iron-proxy demo CA trusted by every client) and adds what an interactive,
browser-driven session needs: tmux, a shell setup, Claude Code pre-configured to start with no prompts,
VM vitals and Claude Code token usage from the in-VM agent, and a `/validate` warm-up.

```bash
uv venv && uv pip install boto3==1.43.123 websockets==15.0.1 httpx python-dotenv
.venv/bin/python scripts/build_sandbox_image.py   # create or update the four images, one after another (~3 min each)
.venv/bin/python scripts/build_sandbox_image.py --size medium   # just one size
.venv/bin/python scripts/smoke_sandbox.py         # end-to-end test, 1 MicroVM, writes results/smoke_sandbox.log
```

Both scripts read part 2's Terraform outputs fresh on every run (`PART2_INFRA` overrides the path): build role,
artifacts bucket, `ca_cert_pem`, the placeholders and part 2's sandbox connector (the one every playground sandbox
uses). The image is tagged `ca-sha256` with the CA it trusts; `--if-needed` (what `deploy.sh` runs)
rebuilds an image only when it's missing or that doesn't match part 2's current CA.

## What's in the image

| File | Goes to | Why |
|---|---|---|
| `Dockerfile` | | part 2's image + `tmux`, `ncurses-term`, `vim-tiny`, `nano`, `htop`, `tini`; `LANG=C.UTF-8` (tmux needs a UTF-8 locale to draw Claude Code's box characters). `tini` is PID 1: with the agent as PID 1, every orphaned process (tmux's daemonised server, a child killed by `timeout`) stayed a `<defunct>` zombie |
| `agent.py` | `/app/agent.py` | the in-VM agent on :8080 (app) and :9001 (hooks); see below |
| `warm.sh` | `/app/warm.sh` | the `/validate` warm-up (`VALIDATE_CMD=bash /app/warm.sh`) |
| `bashrc`, `profile` | `/root/.bashrc`, `/root/.profile` | sources `/etc/sandbox.env` (proxy env + placeholders from the run hook), repeats the CA/`IS_SANDBOX` env, upgrades the shell's `TERM=xterm` to `xterm-256color`, `cd /root/work`, prompt, MOTD. `.profile` makes tmux's login shells read the same file |
| `tmux.conf` | `/root/.tmux.conf` | `tmux-256color`, true colour, `escape-time 10` (Esc stays snappy in Claude Code), 50k history, `mouse on`, `window-size latest`, extended keys, a status bar in the web UI's palette (`bg=#211d1a,fg=#969189`, current window `#e1b761`) showing `<SANDBOX_NAME> · C-b d detaches` (`SANDBOX_NAME` from the run-hook payload, re-read from `/etc/sandbox.env` on each status refresh; `sandbox` if unset) |
| `motd` | `/etc/motd` | five lines on what the sandbox is |
| `claude.json` | `/root/.claude.json` | Claude Code's global state, pre-seeded (below) |
| `settings.json` | `/root/.claude/settings.json` | Claude Code user settings (below) |
| `ca.crt` | `/usr/local/share/ca-certificates/` | written by the build script from `ca_cert_pem`; gitignored |

Also: `/root/work` (working directory), git identity `my2cents demo <demo@my2cents.ai>` with `init.defaultBranch main`
and a credential helper that sends `$GH_TOKEN` (a placeholder; iron-proxy swaps it), and the CA trust env vars from
part 2 (`NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `AWS_CA_BUNDLE`).

## Claude Code: no prompts on first start

Found by starting `claude` in tmux with only the placeholder key, reading the screen with `tmux capture-pane`, and
removing one key at a time (locally in Docker, then in a MicroVM behind iron-proxy, with the proxy both reachable
and unreachable). These are the keys that matter in 2.1.317; everything else (`theme`,
`lastOnboardingVersion`, `lastReleaseNotesSeen`, `hasCompletedProjectOnboarding`, `projectOnboardingSeenCount`,
`allowedTools`, `autoUpdates`) made no difference and is left out.

| Key | File | Without it |
|---|---|---|
| `hasCompletedOnboarding: true` | `~/.claude.json` | "Let's get started. Choose the text style…" (theme picker, then the rest of onboarding) |
| `customApiKeyResponses.approved: ["<last 20 chars of the key>"]` | `~/.claude.json` | "Do you want to use this API key? Yes / **No (recommended)**". Claude Code stores the last 20 characters of an approved `ANTHROPIC_API_KEY`; the build script fills in the placeholder's from Terraform |
| `projects["/root/work"].hasTrustDialogAccepted: true` | `~/.claude.json` | "Accessing workspace: /root/work. Quick safety check: Is this a project you created or one you trust?" Trust is inherited: `/root/work/<repo>` is trusted too |
| `permissions.defaultMode: "bypassPermissions"` | `settings.json` | starts in manual mode (asks before every edit and command) |
| `hasSeenAutoDefaultNudge: true` | `~/.claude.json` | "Make auto mode your default permission mode? **Yes** / No, keep bypass permissions". Only shows when Claude Code can't confirm the account's auto-mode status: here, whenever iron-proxy is unreachable. The local tests (no proxy) and a healthy MicroVM didn't show it; the first smoke run, during a part 2 proxy outage, did. The Enter meant for the prompt then picked the default "Yes" and rewrote `defaultMode` to `auto` |
| `skipDangerousModePermissionPrompt: true` | `settings.json` | with bypass mode as the default: "WARNING: Claude Code running in Bypass Permissions mode… No, exit / Yes, I accept". The older `bypassPermissionsModeAccepted: true` in `~/.claude.json` also suppresses it |
| `IS_SANDBOX=1` (env) | image ENV + `.bashrc` | `claude` exits at once: "--dangerously-skip-permissions cannot be used with root/sudo privileges for security reasons" |

Claude Code rewrites `~/.claude.json` as it runs (startup counters, per-project state); that's fine, the keys above stay.

**Why bypass permissions mode.** The MicroVM *is* the sandbox: nothing on it is worth protecting from Claude, its
only credentials are placeholders that iron-proxy swaps for real ones on allowlisted hosts, and every outbound
request is visible (and blockable) in the proxy feed. Permission prompts there only slow the demo down, and a
prompt stuck in a terminal nobody is watching stalls the agent. If you want prompts back, delete `defaultMode` from
`settings.json` (manual mode) or set it to `acceptEdits` with a `permissions.allow` list; Shift+Tab cycles modes
inside the TUI. `DISABLE_AUTOUPDATER=1` is set because the npm registry isn't on the proxy allowlist.

## Agent (port 8080)

Part 2's endpoints (`/health`, `/exec`, `/exec/stream`, `/files`, `/state`, `/events`, `/headers`, `/tcp/*`;
`/exec` now runs in `/root/work` by default) plus:

- `GET /stats` → `{"cpu_pct", "cpu_count", "mem_total_mb", "mem_used_mb", "mem_available_mb", "load1", "load5",
  "load15", "disk_used_mb", "disk_total_mb", "uptime_s", "procs", "ts"}`. CPU % is from two `/proc/stat` samples
  200 ms apart (the call takes ~200 ms); used memory is `MemTotal - MemAvailable`; disk is the root filesystem.
- `GET /claude-usage` → parses `/root/.claude/projects/**/*.jsonl` and returns
  `{"sessions": [...], "totals": {...}, "files", "skipped_lines"}`. Each session:
  `{"session_id", "project", "model", "turns", "api_calls", "input_tokens", "output_tokens", "cache_read_tokens",
  "cache_creation_tokens", "models": {model: {same counters, "api_calls"}}, "first_ts", "last_ts"}`, newest first.
  `turns` counts typed prompts (not tool results, slash commands or subagent messages); `api_calls` counts distinct
  API responses. A response with several content blocks is written as several lines with the same message id, so
  counters take the max per id rather than summing. Subagent transcripts (separate files, same `sessionId`) are
  merged into their session; `model` is the main thread's last model and `models` breaks tokens down per model so
  the backend can price mixed sessions. Partial lines (a transcript being written) are skipped and counted.
  Files are cached by mtime+size, so polling every 10 s is cheap.

Hooks (port 9001, all six enabled): `/run` writes `/etc/sandbox.env` from the run-hook payload
`{"env": {...}, "nameserver": null}`, as in part 2 (the backend adds `SANDBOX_NAME` to `env` for the tmux status line). `/validate` runs `VALIDATE_CMD` (see below). Only ever mint
endpoint tokens for 8080.

- **No proxy settings.** Part 2's sandbox VPC answers every
  name with a CNAME to its iron-proxy's NLB through DNS Firewall, and can reach nothing else, so clients connect to
  iron-proxy without knowing it (transparent interception). `/etc/resolv.conf` is a read-only overlay in a MicroVM; a
  `nameserver` in the payload is ignored and reported.
- **Hooks never fail.** A non-200 or a timeout from `/run` terminates the MicroVM ~2.5 s after `RunMicrovm`
  ("Run lifecycle hook failed"), which is what writing `resolv.conf` used to do. Every hook catches its exceptions
  and answers 200 with an `error` field (also in `GET /events`); the only non-200 is `/validate`'s deliberate 503.

## `/validate` warm-up

The snapshot's disk pages in lazily, and Claude Code is a 330 MB native binary, so the first `claude` in a fresh
MicroVM is slow unless `/validate` touched it during the build. `warm.sh` runs `claude --version`, `tmux -V`, git,
node, the AWS CLI and gh, then starts the Claude Code TUI in a throwaway tmux server with a copy of the config
(throwaway `HOME`) and waits for the input box, so the whole TUI start path is prefetched.

Lambda drops a single `/validate` request well before `validateTimeoutInSeconds`: calls that took 12.3 s all ended in a
broken pipe, and Lambda called again every ~20 s, so the per-call limit is somewhere under 12 s. A warm-up that never
answers in time fails the build (first attempt: CREATE_FAILED after 8 min). So the agent runs `VALIDATE_CMD` once in a background thread
and answers each `/validate` call within 5 s: 503 ("warming", which Lambda retries) until the warm-up finishes,
then 200. In the v3.0 build the warm-up took ~30 s on each of the two snapshot builds (TUI input box after 8 s):
two 503s about 16 s apart, then a 200 one second after it finished.

## Notes for the backend

Measured by `scripts/smoke_sandbox.py` (image version 4.0, explicit mode; full output in `results/smoke_sandbox.log`):

| Step | Time |
|---|---|
| `run_microvm` → RUNNING | 2.09 s, agent `/health` 0.46 s later |
| shell token + WebSocket + `session_init` | 0.59 s |
| `tmux new -A -s main` → session up | 0.40 s |
| `claude` → input box, no prompts (warmed by `/validate`) | 2.54 s |
| prompt → reply on screen | 3.05 s |
| `suspend_microvm` → SUSPENDED | 0.75 s |
| `resume_microvm` → RUNNING | 0.76 s |
| new WebSocket → `tmux new -A -s main` → Claude screen redrawn | 0.55 s |
| WebSocket to a SUSPENDED MicroVM → `session_init` (auto-resume) | 0.84 s |

- **The shell.** `wss://<endpoint>/shell` with `X-aws-proxy-auth: <shell token>`; first frame text JSON
  `{"type":"session_init","session_id":...}`; then send `{"type":"resize","rows":R,"cols":C}` as text and keystrokes
  as binary. The shell is an interactive bash on `/dev/pts/0` that starts with `TERM=xterm` and reads `/root/.bashrc`,
  so it lands in `/root/work` with the proxy env, `IS_SANDBOX=1` and `TERM=xterm-256color`. WebSocket pings are
  answered (~90 ms round trip from the test machine), so normal ping keepalives work.
- **Attach.** After `session_init` and a resize, send `tmux new -A -s main\n`. It creates the session the first time
  and reattaches afterwards; tmux redraws the whole screen on attach, so the browser needs no replay buffer. Send a
  resize before the attach so tmux sizes the window right (`window-size latest`: the most recently active client wins).
- **Typing into Claude Code.** Enter is `\r`. Send the text and the `\r` as separate frames a few hundred ms apart:
  text plus Enter in one frame is treated as a paste and isn't submitted. "Start Claude Code" can send `claude\r`.
- **Reading the screen without a terminal:** `POST /exec {"cmd": "tmux capture-pane -p -t main"}`. Claude's replies
  start with `●` (U+25CF) on Linux.
- **Suspend/resume and the WebSocket.** On suspend the old WebSocket did *not* close; it stayed OPEN while the
  MicroVM was SUSPENDED and closed with 1006 (abnormal, no reason) after resume. So treat 1006 as "reconnect", and
  don't rely on a close to learn about a suspend (watch `get_microvm`). tmux, the pane shell and `claude` kept their
  PIDs, and Claude answered a new prompt right after resume (its dead keep-alive connections didn't get in the way).
- **Suspended MicroVMs.** `create_microvm_shell_auth_token` works while SUSPENDED, and with `autoResumeEnabled`
  opening the shell WebSocket resumes the MicroVM (`session_init` ~0.85 s after connect). No need to call
  `resume_microvm` first. Right at `session_init`, `get_microvm` reported RUNNING in one run and PENDING in another, so
  treat PENDING after a SUSPENDED as "resuming", not "starting".
- **Inside the VM:** 4 vCPUs, `MemTotal` 8017 MB (the guest sees the 8 GB burst ceiling, not the 2 GB baseline),
  7.8 GB root disk with 1.86 GB used. `/stats` `procs` is container processes only (2 at boot: tini + agent).
- **Hook events.** `GET /events` lists `ready` (captured in the snapshot at build time), then `run`, `suspend`,
  `resume`; a hook error shows up as an `error` field there.
- **What the TUI does on the network.** One interactive start plus two prompts (proxy audit, shared proxy so
  approximate): `/v1/messages`, `/api/claude_cli/bootstrap`, `/api/claude_code/settings` (404) and `policy_limits`,
  `/api/eval/...`, `/api/event_logging/v2/batch`, `/mcp-registry/v0/servers` on api.anthropic.com (key swapped where
  a key is sent); the official plugin marketplace from github.com (`anthropics/claude-plugins-official.git`) and the
  changelog from raw.githubusercontent.com; and two hosts the allowlist **rejects**: `downloads.claude.ai` (twice)
  and `http-intake.logs.us5.datadoghq.com`. Expect those denials in the proxy feed every time Claude Code starts; they
  don't affect the session. `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` in `settings.json` `env` would likely remove
  most of them (not tested here).
