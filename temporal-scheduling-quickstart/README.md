# temporal-scheduling-quickstart

Companion code for the my2cents.ai post **["Temporal: Hands-On with Scheduling on a Self-Hosted macOS Stack"](https://my2cents.ai/deep-dive/temporal-scheduling)**.

A minimal Python project that schedules a recurring health-check workflow against `httpbin.org`, records failures into a local SQLite `incidents` table, and exercises the parts of the Temporal Schedules API the post walks through: interval + calendar specs, overlap policy, jitter, pause/trigger/backfill/list/describe.

## Prerequisites

- macOS with Homebrew
- [`uv`](https://docs.astral.sh/uv/) (`brew install uv`)
- Python 3.12 (`uv python install 3.12`)
- The Temporal CLI (`brew install temporal`)
- Docker Desktop (only for the optional `compose-up` track)

## Quickstart (CLI dev server)

```bash
# 1. Install deps
make install

# 2. In terminal 1, start the Temporal dev server (gRPC :7233, UI :8233)
make dev

# 3. In terminal 2, start the worker
make worker

# 4. In terminal 3, create the every-2-min schedule
make schedule

# Open http://localhost:8233 to watch runs fire.
```

## Layout

```
src/
  config.py             env-driven Temporal connection config
  targets.py            list of httpbin.org targets to probe
  storage.py            SQLite schema + insert helper
  activities.py         probe_endpoint, record_incident
  workflows.py          HealthCheckWorkflow (loops targets, records incidents)
  worker.py             registers workflow + activities on the task queue
  schedules/
    create_interval.py  every 2 min, jitter=10s, overlap=SKIP
    create_cron.py      weekday 09:00 America/New_York calendar spec
    pause_trigger.py    pause / unpause / trigger ad-hoc
    backfill.py         backfill the previous 30 minutes (overlap=ALLOW_ALL)
    list_describe.py    list all schedules / describe one
    delete_all.py       delete both schedules
docker-compose.yml      Postgres-backed stack for the second track
Makefile                see "Make targets" below (or run `make help`)
```

## Make targets

| Target | What it does |
|---|---|
| `make help` | Print a summary of all targets |
| `make install` | `uv sync` |
| `make dev` | `temporal server start-dev --db-filename ./temporal.db --ui-port 8233` |
| `make worker` | Run the Python worker |
| `make schedule` | Create the every-2-min interval schedule |
| `make cron` | Create the weekday-09:00 NY calendar schedule |
| `make pause` / `unpause` / `trigger` | Operate the interval schedule |
| `make backfill` | Backfill the last 30 minutes |
| `make list` / `describe` | Inspect schedules |
| `make delete` | Delete both schedules |
| `make compose-up` / `compose-down` | Postgres-backed stack on `:7233` (UI on `:8080`) |
| `make clean` | Remove `incidents.db` and `temporal.db` |

## Switching to the docker-compose stack

```bash
# Tear down the dev server (Ctrl-C in terminal 1)
make compose-up
# The worker from the Quickstart still talks to localhost:7233 — no env
# change needed. If you stopped it, start it again with `make worker`.
make schedule
# Web UI is on http://localhost:8080 (not 8233)
```

## Cleanup

```bash
make delete        # drop the schedules
make clean         # remove SQLite DBs
make compose-down  # if you brought up compose
```
