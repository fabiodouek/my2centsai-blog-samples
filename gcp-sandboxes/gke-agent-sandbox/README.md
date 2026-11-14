# GKE Agent Sandbox

Companion code for [GKE Agent Sandbox: Hands-On with Warm Pools, Pod Snapshots and the Silent Fallback](https://www.my2cents.ai/deep-dive/gcp-gke-agent-sandbox/), part 5 of 6 in the Google Cloud Sandboxes series.

One script per test, run in order against a GKE Standard cluster. They create real resources and cost money while the cluster runs (about $0.41 an hour at list price, plus $0.10 to $0.20 an hour for each temporary node pool). `teardown.sh` deletes everything and checks.

## Setup

```bash
cp env.example.sh env.sh   # edit PROJECT, BUCKET, ROLE_ID; then: . ./env.sh
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
./00_cluster.sh            # cluster (7 min), gVisor pool, snapshot bucket, role and bindings
```

Tested with Google Cloud CLI 593.0.0 (beta 2026.11.06) with the `gke-gcloud-auth-plugin`, `kubectl` 1.37.2, Python 3.12, `k8s-agent-sandbox` 1.0.11 and Agent Sandbox controller v1.0.11 on GKE 1.37.1-gke.2097000 (Rapid), on November 14, 2026. The GKE docs still pin v1.0.6: the SDK and the controller versions must match.

## Files

| File | What it does |
|---|---|
| `00_cluster.sh` | Cluster, `gvisor` node pool (C3), snapshot bucket (HNS, soft delete off), custom role, Workload Identity bindings |
| `01_install.sh` | Test 1: the managed `--enable-agent-sandbox` flag (on, off, what it leaves), `kubectl apply` of the controller, the secure policy and two rejected Sandboxes, the Sandbox Router |
| `02_isolation.sh`, `probe.sh`, `metadata_probe.py` | Test 2: the same probe from a runc pod, a templated Sandbox and a directly created Sandbox |
| `03_warmpool_latency.py` | Test 3: pools of 2 and 10, cold creates, bursts of 10 and 20, client times and the controller's histogram |
| `03b_first_command.py` | Test 3 side check: the first command on a sandbox takes about 2.4 s, later ones about 125 ms |
| `04_pod_snapshots.py`, `app/state_app.py` | Test 4: five snapshot, suspend and resume cycles; what survives (`--no-pvc` for the variant without the volume) |
| `05_mismatch.py` | Test 5: restore a C3 snapshot onto an N2 node (needs the `mismatch` pool, command in the file) |
| `06_microvm.py` | Test 6: gVisor next to microVM: kernel, metadata, start latency, overhead, snapshot refusal (needs the `microvm` pool, command in the file) |
| `07_visibility.sh`, `07_visibility.py` | Test 7: `kubectl top`, kubelet, Cloud Monitoring and Logging for a runc pod and a gVisor Sandbox |
| `manifests/` | Templates, pools, Sandboxes, snapshot storage config and policy, the burn workload |
| `redact.sh` | Filter that masks the project ID, number and bucket in recorded output |
| `teardown.sh` | Deletes sandboxes, snapshots, PVCs, pools, cluster, bucket, role; then lists what is left |
| `results/` | Raw results of the runs in the post (project ID, number and bucket redacted) |

Notes:

- `01_install.sh` takes about 15 minutes because turning the managed add-on on and off are control-plane updates.
- The snapshot guide's template does not pass `secure-sandbox-policy`; `manifests/sandbox-snapshot.yaml` does.
- `06_microvm.py` removes the policy binding while it runs and puts it back.
