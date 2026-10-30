# Daytona Sandbox Lifecycle

Nine Python scripts that walk a [Daytona](https://www.daytona.io) sandbox through its whole lifecycle: create, run commands and files, lifecycle timers, stop/start and archive, pause and fork on a Linux VM, list and reconnect, snapshots, a custom snapshot, and cleanup. Each script prints timings, so you can reproduce the numbers in the post.

Video walkthrough: https://youtu.be/t67zSjSIUE4

Companion code for [Daytona Sandboxes: Hands-On with the Full Sandbox Lifecycle](https://my2cents.ai/deep-dive/daytona-sandboxes).

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (or plain `pip`)
- A Daytona account and API key from the [dashboard](https://app.daytona.io/dashboard/keys). New organizations get $200 of free compute, far more than this walkthrough uses.
- `05_vm_pause_fork.py` needs the `linux-vm` sandbox class, which not every organization has: check `GET /organizations/{id}/available-sandbox-classes`, and ask Daytona support if it's missing. VMs can live in a different region from your containers (`us-west-3` for mine), so the script creates its client in whichever region the `daytona-vm-small` snapshot is in. It exits with the API's error if your org has no VMs. Everything else runs on Tier 1 containers.

## Setup

```bash
cp .env.example .env          # paste your DAYTONA_API_KEY
uv venv && uv pip install -e .
source .venv/bin/activate
```

The SDK is pinned to `daytona==0.226.1`. Daytona ships SDK releases about twice a week, so bump deliberately.

## Scripts

| Script | What it shows |
|---|---|
| `01_create.py [runs]` | Create with defaults, print class, resources and lifecycle intervals, time N sequential creates |
| `02_commands_files.py` | `process.exec`, session commands, `fs.upload_file/download_file/list_files`, preview URLs (token header and signed), exec timeout |
| `03_timeouts.py` | Auto-stop fires with a process running, `refresh_activity()` keeps a sandbox alive, `ttl_minutes` destroys a stopped sandbox, `auto_delete_interval=0` deletes on stop (takes about 4 minutes) |
| `04_stop_start.py` | Stop/start three times: files survive, processes don't, preview token rotates; a stopped sandbox doesn't wake on exec or HTTP; archive and restore; `pause()` on a container |
| `05_vm_pause_fork.py` | Pause/resume a Linux VM with an in-memory counter, then fork it (needs the `linux-vm` class) |
| `06_list_connect.py` | Labels, `list()` filters, how long `list()` lags `get()`, `get(name)` from a second process, `set_labels()` |
| `07_snapshot_fork.py` | `fork()` on a container, `create_snapshot()` on a running sandbox, three sandboxes from the snapshot |
| `08_custom_snapshot.py` | Build a snapshot with the declarative `Image` builder, compare time-to-import against `pip install` at runtime |
| `09_cleanup.py` | Delete every sandbox labelled by these scripts (forks first) and the demo snapshots |

Run them in order:

```bash
python 01_create.py
python 02_commands_files.py
python 03_timeouts.py
python 04_stop_start.py
python 05_vm_pause_fork.py
python 06_list_connect.py
python 07_snapshot_fork.py
python 08_custom_snapshot.py
python 09_cleanup.py
```

## Safety nets

- Every sandbox gets the label `demo=my2cents-lifecycle`, so `09_cleanup.py` only touches what these scripts created.
- Every sandbox is created with `ttl_minutes=60`, so anything a crashed script leaves behind is destroyed within the hour, whatever its state.
- Stopped sandboxes are kept, and their disk billed, until something deletes them. Run `09_cleanup.py` when you're done.

## License

Apache-2.0
