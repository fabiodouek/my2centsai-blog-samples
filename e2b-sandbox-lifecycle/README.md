# E2B Sandbox Lifecycle

Nine Python scripts that walk an [E2B](https://e2b.dev) sandbox through its whole lifecycle: create, run commands and files, timeouts, pause/resume, auto-pause and auto-resume, list and connect, fork and snapshot, a custom template, and cleanup. Each script prints timings, so you can reproduce the numbers in the post.

Companion code for [E2B Sandboxes: Hands-On with the Full Sandbox Lifecycle](https://my2cents.ai/deep-dive/e2b-sandboxes).

## Prerequisites

- Python 3.11+ (E2B's Python SDK minimum since 2.56.0)
- [uv](https://docs.astral.sh/uv/) (or plain `pip`)
- An E2B account and API key from the [dashboard](https://e2b.dev/dashboard). The free Hobby plan includes $100 of one-time credits, far more than this walkthrough uses.

## Setup

```bash
cp .env.example .env          # paste your E2B_API_KEY
uv venv && uv pip install -e .
source .venv/bin/activate
```

The SDK is pinned to `e2b==2.59.0`. E2B ships SDK releases every few days and minor versions have changed defaults, so bump deliberately.

## Scripts

| Script | What it shows |
|---|---|
| `01_create.py [runs]` | Create with defaults, print `get_info()` (vCPU, RAM, timeout, lifecycle), time N sequential creates |
| `02_commands_files.py` | Blocking, streaming and background commands; `files.write/read/list`; preview URL; command timeout |
| `03_timeouts.py` | `set_timeout()` sets an exact value, `connect()` only extends, expiry kills by default |
| `04_pause_resume.py [cycles]` | Pause/resume N times; checks files, a running process and its in-memory counter survive; reattaches to its stdout |
| `05_auto_resume.py` | `lifecycle={"on_timeout": "pause", "auto_resume": True}`; wake via `files.read()` and via HTTP |
| `06_list_connect.py` | Metadata tags, `Sandbox.list()` filters and pagination, `Sandbox.connect(id)` from a second process |
| `07_fork_snapshot.py` | `fork(count=3)` copies files, processes and memory; `create_snapshot()` and restore |
| `template/build.py` + `08_template.py` | Build a data-science template, compare time-to-`import pandas` against the base template |
| `09_cleanup.py` | Kill all running and paused sandboxes tagged by these scripts, delete demo snapshots |

Run them in order:

```bash
python 01_create.py
python 02_commands_files.py
python 03_timeouts.py
python 04_pause_resume.py 3
python 05_auto_resume.py
python 06_list_connect.py
python 07_fork_snapshot.py
python template/build.py      # one-off, about a minute
python 08_template.py
python 09_cleanup.py
```

## Cost

A default sandbox (2 vCPU, 512 MiB) costs about $0.11/hour while running, billed per second. Paused sandboxes are not billed but **never expire**, so always finish with `09_cleanup.py` or:

```bash
npx @e2b/cli sandbox kill --all --state=running,paused --metadata=demo=my2cents-lifecycle
```

Every sandbox created here carries the metadata tag `demo=my2cents-lifecycle`, so cleanup never touches your other sandboxes.

## License

Apache-2.0 (see [LICENSE](LICENSE))
