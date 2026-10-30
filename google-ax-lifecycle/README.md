# Google AX task lifecycle on Amazon EKS

Companion code for the my2cents.ai deep dive on the Google AX task lifecycle: create, resume,
suspend, wake on request, snapshots in S3, large workspaces, density and failures, with measured
timings.

Video walkthrough: https://youtu.be/b6XbhiP262o

The scripts drive the real `ax` CLI against the cluster from [`../google-ax-eks`](../google-ax-eks),
the install post. That cluster must be running, with its scripts 01 to 05 done.

## Requirements

- The `ax-eks` cluster and the `ax` CLI from `../google-ax-eks` (`.src/ax/bin/ax`)
- Python 3.11+ (standard library only), `kubectl`, the AWS CLI
- Nothing else: no API keys. Every task runs `sleep infinity` or a short shell command.

## Scripts

| Script | What it shows |
|---|---|
| `01_create.py` | Five tasks created: they start `Suspended` and occupy no worker |
| `02_resume.py` | First resume timings, placement, the default sandbox size (1 CPU, 512 MiB) and a task with limits |
| `03_suspend_resume.py` | Five suspend/resume cycles: `/workspace` survives, `/tmp` and processes don't, the command reruns on every boot |
| `04_auto_resume.py` | A request through Substrate's router wakes a suspended task (needs the router's mTLS identity, see `router-probe.yaml`) |
| `05_snapshot_s3.py` | When a snapshot reaches S3 (5 min), when the node's copy is dropped (30 min), and a resume from S3. Takes about 35 minutes |
| `06_large_workspace.py` | A 2 GB workspace: suspend time grows with size, resume doesn't, every byte comes back |
| `07_density.py` | How many tasks fit on two workers, and what decides it. Run `09_cleanup.py` first |
| `08_failures.py` | The command exits; a worker pod dies under a running and a suspended task; the WorkerPool changes |
| `09_cleanup.py` | Deletes every task in the `lifecycle` atespace |

```bash
python3 01_create.py
python3 02_resume.py
# ...
python3 09_cleanup.py
```

All tasks live in the atespace `lifecycle`, so cleanup never touches anything else. `08_failures.py`
patches the `ax-workers` WorkerPool and puts it back as installed at the end; running tasks on the
cluster fail when it does.
