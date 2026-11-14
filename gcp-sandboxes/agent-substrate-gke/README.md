# Agent Substrate on GKE

Companion code for [Agent Substrate on GKE: Hands-On with Suspend and Resume, Density and Snapshots in Cloud Storage](https://www.my2cents.ai/deep-dive/gcp-agent-substrate-gke/), part 6 of 6 in the Google Cloud Sandboxes series.

It repeats the AX-on-EKS measurements ([`google-ax-eks`](../../google-ax-eks), [`google-ax-lifecycle`](../../google-ax-lifecycle), [`google-ax-stress`](../../google-ax-stress)) with the same harness, ported, on two `c3-standard-4` nodes. `eks-baseline/` holds those EKS results and `charts.py` draws both runs on the same axes.

Versions used: Substrate v1.0.0, installer `substrate-gke` v1.0.0-gke.0, AX main at `eac8313` (its `go.mod` pins Substrate v1.0.0), GKE 1.37.1-gke.2097000 on the Rapid channel.

## Before you start

- A local machine with `gcloud` (and the `beta` component), `kubectl`, `gke-gcloud-auth-plugin`, Go, Docker with Buildx, `make`, `jq` and Python 3.12. Cloud Shell does not work: the installer needs more than its 5 GB of disk.
- A billed project, and the roles the installer needs (Kubernetes Engine Admin, Storage Admin, Artifact Registry Admin, Project IAM Admin, Service Account Admin, Service Usage Admin, Monitoring Editor). Set a budget alert.
- The cluster costs about **$0.61 an hour** while it runs. Run `teardown.sh` when you finish; Substrate's 500 GiB PostgreSQL volume outlives the cluster.
- `cp env.example.sh env.sh`, edit it, and `. ./env.sh`. Nothing in it is a secret.
- `gcloud auth application-default login` is what the Go tools expect. I had no browser session for it, so my own `env.sh` pointed them at a small local stand-in for the metadata server; you won't need that.

## Setup scripts

| Script | What it does | Time |
|---|---|---|
| `00_cluster.sh` | Cloud NAT, Private Google Access, and a zonal Standard cluster of 2 x c3-standard-4 (the CLI twin of the console steps) | 7 min |
| `01_bootstrap.sh` | What the installer's provisioning step runs: `setup-gcp bootstrap` (buckets, service accounts, IAM, repository, dashboards) | 10 min |
| `02_install.sh` | `ate-setup deploy ate-system` with the pre-built v1.0.0 images, and builds `kubectl-ate` | 1.5 min |
| `03_workerpool.sh` | A WorkerPool of 2 gVisor workers, pinned by digest | 10 s |
| `04_ax.sh` | Builds the `ax` CLI and the AX images, deploys PostgreSQL and `ax-server` | 1 to 3 min |
| `05_demo.sh` | Substrate's own Counter demo (memory fidelity). **Delete its WorkerPool before the density tests**: AX schedules onto any pool | 1 min |
| `ax_forward.sh` | Supervised port-forwards to `ax-server` and the Substrate API; `run.sh` starts it. The CLIs' own tunnels added 1 to 3 s spikes | |
| `teardown.sh` | Everything, in the right order, then lists what is left | 5 min |

The installer is an interactive terminal program; these scripts run the same commands from the same repository and tag, so they can be recorded and repeated. `01_bootstrap.sh` and `02_install.sh` read the settings the installer would pass.

## Tests

Run one with `./run.sh <script>`: it picks the atespace, starts the port-forwards and keeps the output in `results/<script>.txt`.

| Test | Script | What it checks |
|---|---|---|
| 1 Install, readiness, Counter demo | `t1a_readiness.sh`, `05_demo.sh` | certificate APIs, pod-certificate admission, kernel; a native memory-fidelity actor |
| 2 What a suspend keeps | `t2a_ax_keeps.py`, `t2b_native_keeps.py` | `/tmp`, a 50 MB workspace, a background process, boot log, an open TCP connection; AX against a native actor |
| 3 Lifecycle timings | `t3a_create.py`, `t3b_first_resume.py`, `t3c_suspend_resume.py` | create, first resume, size probes, suspend/resume x 5 |
| 4 Wake on request | `t4_wake_on_request.py` | the router, `ate-target-actor`, negatives, router ports, a native actor |
| 5 Snapshots to Cloud Storage | `t5a_snapshot_gcs.py`, `t5b_large_workspace.py` | time to durable and detached, object sizes, resume from GCS; a 2 GB workspace |
| 6 Density | `t6a_density_waves.py`, `t6b_density_ramp.py` | waves of 12 and 40, then a ramp at 50m / 64Mi until refusal |
| 7 Latency and control-plane load | `t7a_latency.py`, `t7b_control_plane.py` | N = 1 to 40, three rounds; 120 tasks, 20 in flight |
| 8 Wake by CPU limit, bursts | `t8_wake_burst.py` | 50m / 250m / 1 CPU; bursts of 1, 10, 30 |
| 9 Noisy neighbors | `t9_noisy.py` | six victims against CPU, disk and memory hogs |
| 10 Snapshot storm, failures, resources | `t10a_snapshot_storm.py`, `t10b_failures.py`, `t10c_monitoring.py`, `t10d_nat_sanity.py` | 20 x 100 MB; command exit, worker pod deletion, WorkerPool patch (last); Cloud NAT and Cloud Storage bytes, node CPU/memory |

`t0_rtt.py` records the client-path baseline (the median of 10 x `ax get tasks`), `t0_window.py` marks the start of the measured window for `t10c_monitoring.py`, and `charts.py` draws `charts/` from `results/` and `eks-baseline/`. `cleanup.py` and `cleanup_lifecycle.py` delete the tasks. `manifests/` holds the native probe actor, and `router-probe.yaml` the curl pod the router accepts as a client.

## Notes

- Each test ran once on an otherwise empty cluster, after deleting every task (`ax get tasks` lists at most 50, so the harness loops).
- Client-side numbers include the round trip from the laptop (about 0.5 s for `ax get tasks`, 1.24 s for `kubectl ate`); in-cluster numbers (the router wake tests) do not.
- Boot disks are `hyperdisk-balanced` because a new project allows 500 GB of `pd-balanced`, which Substrate's PostgreSQL volume alone uses.
- The workspace results under `results/` are from the run in the post, November 14, 2026.
