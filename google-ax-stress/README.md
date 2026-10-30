# Stress-testing Google AX on two EKS nodes

Companion code for the my2cents.ai deep dive that stress-tests Google AX and Agent Substrate on the
two-node Amazon EKS cluster from [`../google-ax-eks`](../google-ax-eks): density ceiling,
create/resume/suspend latency under concurrency, wake-on-request bursts through Substrate's router,
control-plane throughput, noisy neighbors and a snapshot storm. Every chart in the post is drawn by
`09_charts.py` from the results these scripts write.

Video walkthrough: https://youtu.be/hLPkKjIvDXY

No more nodes are added: everything runs on the two `m7i.xlarge` workers the install post created.

## Requirements

- The `ax-eks` cluster, the `ax` CLI and `kubectl-ate` from `../google-ax-eks`
- Python 3.11+, `kubectl`, the AWS CLI, Terraform
- `matplotlib` for the charts: `uv venv && uv pip install -e .`

## Scripts

| Script | What it does | Writes |
|---|---|---|
| `01_metrics.py` | Turns on Container Insights (`terraform apply -var container_insights=true`): metrics only, no logs | `results/window.json` |
| `02_density.py` | Adds idle tasks (cpu 50m, 64Mi) in batches of 10 until Substrate refuses them | `results/density.{json,csv}` |
| `03_latency.py` | Create, resume, suspend with 1, 5, 10, 20, 40 calls in flight; p50/p95/p99 | `results/latency.{json,csv}` |
| `04_wake_burst.py` | 1, 10, 30, 60 suspended tasks woken at once by requests through the router | `results/wake.{json,csv}` |
| `05_control_plane.py` | 120 tasks created, resumed, suspended, deleted 20 at a time; control-plane CPU | `results/control_plane.{json,csv}` |
| `06_noisy.py` | Six victim tasks benchmark CPU and disk while CPU, disk and memory hogs run next to them | `results/noisy.{json,csv}` |
| `07_snapshot_storm.py` | 20 tasks with 100 MB each suspend at once; time until every snapshot is durable in S3 | `results/snapshot_storm.{json,csv}` |
| `08_cloudwatch.py` | Pulls the test window's Container Insights and NAT gateway metrics | `results/cloudwatch.json` |
| `09_charts.py` | Draws every chart, light and dark | `charts/*.png` |
| `10_cleanup.py` | Deletes the `stress` tasks, turns Container Insights off, deletes its log groups | |

`common.py` has a `Sampler` that polls each node's kubelet stats summary while a test runs (CPU and
memory per node and per control-plane/worker pod). Substrate runs `runsc` inside its worker pods,
so a worker pod's numbers are the sum of the sandboxes it hosts.

Run them one after the other, with nothing else using the cluster: they measure shared capacity.

## Parking the cluster

To stop paying for the nodes without tearing everything down:

```bash
aws eks update-nodegroup-config --cluster-name ax-eks \
  --nodegroup-name "$(aws eks list-nodegroups --cluster-name ax-eks --query 'nodegroups[0]' --output text)" \
  --scaling-config minSize=0,maxSize=1,desiredSize=0
```

The EKS control plane, the NAT gateway and the EBS volumes (including Substrate's 500 GiB PostgreSQL
disk) still bill, about $5/day. The Terraform module ignores later changes to the node group's desired
size, which is why this is a CLI call. `../google-ax-eks/scripts/99_teardown.sh` removes everything.
