# LangSmith Sandbox Lifecycle

Ten Python scripts that walk a [LangSmith Sandbox](https://docs.langchain.com/langsmith/sandboxes) through its whole lifecycle: create, run commands and files, lifetime settings and resizing, stop/start with and without memory, idle stop and auto-wake, list and reconnect, snapshots and forks, a custom snapshot, the quota and the bill, and cleanup. Each script prints timings, so you can reproduce the numbers in the post.

Video walkthrough: https://youtu.be/-9pbNbtK_Xw

Companion code for [LangSmith Sandboxes: Hands-On with the Full Sandbox Lifecycle](https://my2cents.ai/deep-dive/langsmith-sandboxes).

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (or plain `pip`)
- A LangSmith account and API key from **Settings > API Keys**. The free Developer plan includes 8 LSU of sandbox usage and up to 10 running sandboxes, far more than this walkthrough uses.
- Sandboxes on non-Enterprise workspaces get a fixed egress allow-list (package registries, GitHub, Docker Hub, the big model APIs). `02_commands_files.py` prints it and shows what's blocked.

## Setup

```bash
cp .env.example .env          # paste your LANGSMITH_API_KEY
uv venv && uv pip install -e .
source .venv/bin/activate
```

The SDK is pinned to `langsmith[sandbox]==0.14.14`. The sandbox API changes often (templates were replaced by snapshots in April 2026), so bump deliberately.

The Python SDK doesn't expose labels, memory-preserving stop, memory snapshots, list filters or usage, so `common.py` has a small `rest()` helper for `https://api.smith.langchain.com/api/v2/sandboxes`. Everything else goes through `SandboxClient`.

## Scripts

| Script | What it shows |
|---|---|
| `01_create.py [runs]` | Quota from `GET /usage`, defaults (size, image, user, TTLs, egress allow-list), what the guest reports, N sequential creates with time to first command |
| `02_commands_files.py` | `run()` exit codes, streaming, the 60 s default timeout, stdin with `close_input`, `write/read/ls/glob/grep/stat`, download URL, service URL, egress probes, `pip` vs PEP 668 |
| `03_lifetimes.py` | TTL validation, `update_sandbox()` on a live sandbox, the 30-day `delete_after_stop_seconds` cap, resizing with `PATCH` (applies on next start), `delete_after_stop_seconds=60` deleting a stopped sandbox |
| `04_stop_start.py` | Stop/start three times: files survive, processes don't; a command on a stopped sandbox wakes it; then the same cycle with `preserve_memory_on_stop`, where the process keeps counting |
| `05_idle_wake.py` | Auto-stop at `idle_ttl_seconds=60`, waking via `read()`, `run()`, a download URL and a service URL; a detached process doesn't count as activity, an attached command and service traffic do |
| `06_list_connect.py` | Labels, `list_sandboxes()` vs the REST filters and cursor paging, a second process that reattaches to a running command and wakes a stopped sandbox |
| `07_snapshot_fork.py` | `capture_snapshot()` while a command streams, three "forks" from the snapshot (sequential and concurrent), deleting a snapshot in use, a memory snapshot that resumes a running process |
| `08_custom_snapshot.py` | `create_snapshot_from_dockerfile()` and `create_snapshot(docker_image=...)`, time-to-import vs `pip install` at runtime |
| `09_quota_bill.py` | Whether stopped sandboxes count against the cap, and the bill from `GET /usage/costs` |
| `10_cleanup.py` | Delete every sandbox and snapshot these scripts created |

Run them in order:

```bash
python 01_create.py
python 02_commands_files.py
python 03_lifetimes.py
python 04_stop_start.py
python 05_idle_wake.py        # ~20 minutes, mostly waiting for idle stops
python 06_list_connect.py
python 07_snapshot_fork.py
python 08_custom_snapshot.py
python 09_quota_bill.py
python 10_cleanup.py
```

## Safety nets

- Every sandbox is named `my2cents-lc-<test>-<n>`, and the ones created over REST also carry the label `demo=my2cents-lifecycle`, so `10_cleanup.py` only touches what these scripts created. Snapshots are named `my2cents-lc-*`.
- Every sandbox is created with `idle_ttl_seconds=300` and `delete_after_stop_seconds=600`, so anything a crashed script leaves behind stops after 5 idle minutes and is deleted 10 minutes later. Without these the server defaults are 600 s and 14 days.
- Snapshots have no TTL and are billed as storage until deleted, and re-using a snapshot name doesn't replace the old one (it only moves the tag). Run `10_cleanup.py` when you're done.
- `create_snapshot_from_dockerfile()` uploads its whole `context` directory (default: the current directory, minus `.git`, ignoring `.dockerignore`). `08_custom_snapshot.py` points it at `datasci/` so your `.env` and `.venv` aren't sent.

## License

Apache-2.0
