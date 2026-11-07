# OpenSandbox sandbox lifecycle on Amazon EKS

Companion code for part 3 of the my2cents.ai series "OpenSandbox on Amazon EKS": "OpenSandbox Sandbox Lifecycle: Hands-On on Amazon EKS". It runs on the cluster from part 2 and shows what happens to a sandbox over its life: its states, its timeout and renewal, pause and resume to Amazon ECR, warm pools, runc against Kata, and what happens when a pod is deleted or a node is cordoned.

Video walkthrough: https://youtu.be/9WgY42WBggA

## What you need first

This directory reuses the cluster, the images and the server of part 2. Next to it you need [`../opensandbox-eks`](../opensandbox-eks), with these scripts run there:

- `01_cluster.sh`: the cluster, and `env.sh`, which every script here reads.
- `02_kata.sh`: Kata Containers and the `kata-qemu` and `kata-clh` RuntimeClasses.
- `03_images.sh`: the images in ECR, including `opensandbox-eks/python:3.12-slim`, the workload image used here.
- `04_install.sh`: the controller and the lifecycle server, the API key in `.env`, and the release's source checkout in `.src/OpenSandbox` (its charts and its resume benchmark are used here).
- `05_gateway.sh`: the ingress gateway. The server configuration changes here keep its settings.
- `06_console.sh` is optional.

Then bring the nodes up if you scaled them down (`../opensandbox-eks/08_scale.sh up`).

Tools: [`uv`](https://docs.astral.sh/uv/), Python 3.11 or later as `python3` (for `server_config.py`, which uses only the standard library), `kubectl`, Helm (tested with 4.3.1, as in part 2), the AWS CLI v2, `jq`, `curl` and `envsubst` (from gettext). Run `uv sync` once in this directory. `pyproject.toml` pins `opensandbox==1.2.0` and `opensandbox-cli==1.2.0`.

The Python scripts read the API key from `../opensandbox-eks/.env`. `03_resume_benchmark.sh` reads it from the Secret `opensandbox-server-auth`. Every script reads the cluster's names from `../opensandbox-eks/env.sh`. No script prints the API key or an ECR token. `common.py` loads part 2's `common.py` from its file (it is not copied), so the two never drift apart.

### The port-forward

The lifecycle server is reachable only through `kubectl port-forward` on `127.0.0.1:18080`. You do not have to start it: every Python script here keeps one up while it runs. A thread checks `/health` every second and starts `kubectl -n opensandbox-system port-forward svc/opensandbox-server 18080:80` whenever nothing answers. That matters because the forward ends whenever the server pod is replaced (`switch_runtime.sh` does that), and sometimes on its own. A forward you started yourself is used while it works and never stopped.

### The ECR token: run `00_snapshot_config.sh` before every session

Pause pushes a snapshot image to ECR. The controller's commit Job reads registry credentials only from a `kubernetes.io/dockerconfigjson` Secret, and an ECR token lasts 12 hours. `00_snapshot_config.sh` writes a fresh one, so run it at the start of each session that pauses sandboxes. Two scripts check the token before they start:

- `02_pause_resume.py` checks it against ECR itself (`GET /v2/` must answer 200) and refuses a token that expires within 10 minutes; `02b_token_fallback.py` checks the 200.
- `03_resume_benchmark.sh` refuses one that expires within an hour (its run takes about 8 minutes).

On EKS a broken pull Secret does not stop the snapshot image from pulling, so a resume should not depend on the token either (inferred: no resume was run with a broken Secret). `02b_token_fallback.py` shows the pull side by pulling a sandbox's real snapshot image three ways: with a copy of the pull Secret holding a token ECR rejects (`GET /v2/` answered 400 to it in our run, 200 to the real one), with no pull secret at all, and on the other node. Each time the kubelet pulled the image anyway. It falls back to the node group role's ECR credentials; the script prints that role's managed policies, `AmazonEC2ContainerRegistryReadOnly` among them. The shared Secret is never touched. With a registry in another account, the node role can't pull unless that repository's policy grants it; give the role pull access there, or keep the pull secret current.

What we did not test: a pause with an expired token. Testing it would mean breaking the shared Secret. The release docs (`docs/guides/pause-resume.md`, Troubleshooting) say the commit Job then logs `unauthorized` and the snapshot fails.

## Scripts and run order

Run them in this order from this directory. Each one prints what it does, and exits 1 when a step goes wrong. Times are from our runs on two `m7i.2xlarge` nodes.

| Script | What it does | Time (measured) |
|---|---|---|
| `00_snapshot_config.sh` | Creates the ECR repository `opensandbox-eks/snapshots` with the settings of part 2's repository creation template. Writes the Secret `opensandbox-snapshot-ecr` with a fresh ECR token and upgrades the controller with `values/controller-snapshot.yaml`. The first run replaces the controller pod. A re-run only replaces the token: the Helm manifests are identical and the pod stays. | 34 s first run, 19 s re-run |
| `01_states.py` | Creates a sandbox with the minimum 60-second timeout. Prints every state it goes through, runs a command, calls `renew()`, then waits until the controller deletes the sandbox at its expiry. | about 1 min 40 s |
| `02_pause_resume.py` | Pause and resume with ECR, and what survives. Every item is a check: the vault before the pause, no pod while Paused, a new pod on the snapshot images, the marker back, `/dev/shm`, the process and the vault gone. | about 40 s |
| `02b_token_fallback.py` | What an invalid token in the resume pull secret does on EKS: nothing (see above). | under 1 min |
| `03_resume_benchmark.sh` | The project's own resume benchmark (`tests/python/benchmarks/kubernetes_resume.py` from the 1.2.0 tag): 3 warmups and 30 samples. | about 8 min (33 trials of about 15 s) |
| `04_pools.py` | A Pool (`manifests/pool.yaml`): warm against cold creates, `recycleStrategy: Delete`, and the 400 for a pooled create with a network policy. | about 2.5 min |
| `05_runtimes.py` | What a sandbox sees under the server's current runtime, and three timed creates. Run it under runc, switch to Kata, run it again. | about 25 s |
| `switch_runtime.sh runc\|kata` | Switches the server's `[secure_runtime]`: a Helm upgrade through `server_config.py`. | about 30 s |
| `06_failure.py` | Deletes a sandbox's pod, then cordons a node and creates sandboxes, and uncordons it at the end. | about 2 min |
| `99_cleanup.py` | Removes what these scripts leave, and nothing else: sandboxes with `example=opensandbox-lifecycle`, and only this directory's snapshot images. | seconds |

### 00: the snapshot registry

All snapshots go to the one repository `opensandbox-eks/snapshots`. The default image naming (`{{.Registry}}/{{.SandboxName}}-{{.ContainerName}}:{{.SnapshotTag}}`) makes one repository per sandbox. `values/controller-snapshot.yaml` sets `imageURITemplate: "{{.Registry}}:{{.SandboxName}}-{{.ContainerName}}-{{.SnapshotTag}}"`, which puts the sandbox and the container in the tag instead, for example `opensandbox-eks/snapshots:<sandbox id>-sandbox-snap-gen2`. The number is the BatchSandbox's generation, so the first pause of a server-created sandbox is `gen2`, not `gen1`.

An explicit `aws ecr create-repository` does not apply a repository creation template; only a push to a missing repository does. So the script reads part 2's template (tag mutability, encryption, tags, the 7-day rule for untagged images) and applies the same settings. The pushed snapshot images are tagged, so that rule never removes them. `99_cleanup.py` deletes them. Deleting a `SandboxSnapshot` does not delete its images either: the controller's deletion handler (`kubernetes/internal/controller/sandboxsnapshot_lifecycle.go`, `handleDeletion`, at the 1.2.0 tag) removes only the commit and unpause Jobs. If you keep using snapshots, add a lifecycle rule to the repository that selects tagged images, by tag pattern or by age, or delete them yourself. Pick an age longer than any sandbox stays paused: a paused sandbox's snapshot image is its only copy.

If you run `../opensandbox-eks/04_install.sh` again, it reinstalls the controller from `values/controller.yaml` alone, which turns snapshots off. Run `00_snapshot_config.sh` again after it.

### 01: states, renew and expiry

The server answers the create call only once the pod is Running. To show the `Pending` state, a second thread lists the run's sandboxes four times a second. In our run the states were `Pending (CREATING)`, then `Running`, then `Stopping (DELETING)`, then gone. `renew(90 s)` sets the expiry to now plus 90 seconds, and the BatchSandbox's `spec.expireTime` moves with it. `renew()` can also shorten a sandbox's life. The controller deleted the sandbox about one second after its `expires_at`.

### 02: pause and resume

The sandbox gets an outbound network policy and the credential proxy, because the Credential Vault lives in the egress sidecar that the policy adds. Before the pause, the script writes `/root/marker.txt`, writes `/dev/shm/marker.txt` (memory), starts `sleep 1000` and creates a vault entry. The entry is a placeholder `apiKey` for `api.example.com`, not a real credential.

Pause commits the root filesystem of each container to an image, one per container. With the sidecar that makes two: about 43 MB for the sandbox and about 136 MB for the egress sidecar. After the push, the pod is deleted. Resume starts a new pod from those images (by digest) and gives it the pull secret.

What came back in our runs:

- `/root/marker.txt`: yes.
- `/dev/shm/marker.txt`: no.
- The `sleep` process: no. Its pid file, in the root filesystem, did come back.
- The vault: no, the sidecar answers 404 for it.

The vault was process memory in the old sidecar. The script checks that the new sidecar answers (its egress policy reads back) and that it says 404 for the vault. A sidecar that does not answer counts as a failure, not as "the vault is gone". Whoever holds the credentials outside the sandbox has to create the vault again after every resume, and the script does that and checks it. In our three runs, pause took 6.7 to 12.6 s, and resume to the first command 5.6 to 8.7 s. Both are asynchronous on the server (`202 Accepted`), so the server's 60-second `sandbox_create_timeout_seconds` does not apply to them.

`kubectl get sandboxsnapshots` lists the fast-sandbox CRD (`sandboxsnapshots.sandbox.fast.io`), not OpenSandbox's. Ask for `sandboxsnapshots.sandbox.opensandbox.io` (short name `sbxsnap`). The scripts here use the full names for every OpenSandbox resource.

### 03: the project's resume benchmark

`03_resume_benchmark.sh` runs the runner exactly as published, from its own directory (it records the checkout's git revision), with this directory's `.venv` (the same SDK version). It writes `results/environment.json`, the as-built facts the runner's guide (`docs/guides/kubernetes-resume-benchmark.md`) asks for, with no credentials. It also writes `results/resume-uncontrolled-<time>.jsonl`, one record per trial plus the summary. `command_ms`, the time until a command reads the marker back, is the resume time a user waits for. The run is "uncontrolled": no cache was cleared or warmed. `SAMPLES` and `WARMUPS` change the counts.

The runner turns the SDK's retries off, so one dropped connection fails a trial. A failure that hits the clean-up stops the run. The script therefore keeps a port-forward of its own on `127.0.0.1:18081` (`BENCH_PORT`) for the whole run and does not share 18080. Our first full run, on 18080, stopped at trial 4 when another client's port-forward ended.

Our run had the cluster to itself. 30 of 30 measured trials succeeded (and the 3 warmups), with `completed_requested_trials: true`. The server ran on runc, on two `m7i.2xlarge` nodes, through the server proxy:

| from the resume call to | p50 | p95 |
|---|---|---|
| `api_ack_ms`: the API accepted | 125 ms | 357 ms |
| `running_ms`: the first GET saying Running | 1,534 ms | 1,967 ms |
| `ready_ms`: execd healthy on a fresh connect | 2,067 ms | 2,502 ms |
| `command_ms`: the marker read back | **2,396 ms** | **2,823 ms** |

`command_ms` ranged from 1,982 to 3,235 ms. An earlier run, with other work on the cluster, gave p50 2,327 ms and p95 2,653 ms. The runner's sandboxes have no network policy, so there is no egress sidecar and one snapshot image per pause. `02_pause_resume.py`'s sandbox carries a sidecar and took 5.6 to 8.7 s to its first command after resume. That came from three runs measured with that script's own polling, so it is not comparable sample for sample.

### 04: pools

`manifests/pool.yaml` is the release's pool example adapted to this cluster:

- every image comes from our ECR;
- the workload is python:3.12-slim, with the same 1 CPU / 2 GiB a cold create gets;
- the tolerate-everything toleration is gone.

Each pool pod runs task-executor in the foreground. After allocation, the controller starts `bootstrap.sh` (and so execd) as a task, because the pod existed before the request did.

In our run:

- The controller filled the buffer (2 Ready pods) 5 s after `kubectl apply`.
- Three rounds of one cold and one warm create, on a cluster with nothing else running, gave cold 2.7 / 2.1 / 2.8 s and warm 1.3 / 1.3 / 1.3 s. The median warm create was 1.5 s faster. An earlier run, with other work on the cluster, gave the same medians: 2.7 and 1.3 s.
- The warm sandbox's hostname is the pool pod's name (`osb-pool-xxxxx`), not `<sandbox id>-0`.

With `recycleStrategy: Delete`, the pod a sandbox used is deleted when the sandbox is, along with whatever the sandbox left in it. The buffer is refilled with pods that never ran a sandbox. In both our runs the controller deleted the used pod about 25 s after the sandbox, and the pod then took another 30 s to go: about the default 30-second termination grace period. The replacement pod was Running within 27 to 28 s of the sandbox's deletion. `Restart` and `Noop` reuse the pod instead. With `Noop`, files and processes leak into the next sandbox.

A pooled create cannot carry `networkPolicy` or `credentialProxy`: the server answers 400, because the pod already exists and the egress sidecar cannot be added to it. Put an egress policy for pooled sandboxes in the Pool template. The server's `[secure_runtime]` does not reach pool pods either: set `runtimeClassName` in the Pool template to run them under Kata.

### 05 and switch_runtime.sh: runc and Kata

The runtime belongs to the server, not to the request. One server process has one `[secure_runtime]`, and a create request cannot choose. So the test is: run `05_runtimes.py`, switch, run it again, switch back.

```bash
uv run python 05_runtimes.py      # runc
./switch_runtime.sh kata          # kata-qemu; the server pod is replaced (about 30 s)
uv run python 05_runtimes.py      # a VM per sandbox
./switch_runtime.sh runc          # parts 4 and 5 expect runc
```

What one sandbox (limits: 1 CPU, 2 GiB) saw in our runs:

| | runc | kata-qemu |
|---|---|---|
| kernel | `6.18.56-125.164.amzn2023.x86_64`, the node's | `6.18.35`, its own |
| CPUs it can use | 8, all of the node's (the 1-CPU limit is a cgroup quota) | 2 |
| memory it sees | 30.8 GiB, the node's | 3.9 GiB |
| machine (DMI) | `Amazon EC2 / m7i.2xlarge` | none |
| its kernel has been up | 22 min (the node) | 4 s (the VM just booted) |
| pod overhead reserved | none | 250m CPU, 160Mi |
| create time, median of 3 | 2.2 s | 4.2 s |

Kata's `configuration-qemu.toml` on these nodes sets `default_vcpus = 1`, `default_memory = 2048` and `static_sandbox_resource_mgmt = false` (read from a node with `kubectl debug`). So the VM starts with 1 vCPU and 2 GiB, and Kata adds the container's limits to it: 1 + 1 vCPU and 2 + 2 GiB, which fits what the sandbox saw (the hot-plug itself wasn't observed). The RuntimeClass reserves only 160Mi of overhead for all that.

gVisor is not installed here. With an egress policy on (parts 4 and 5), the server refuses gVisor with HTTP 400: `ensure_egress_runtime_compatible` in `server/opensandbox_server/services/validators.py` rejects `networkPolicy` when `[secure_runtime] type = "gvisor"`. The message says gVisor does not support the nftables rules the egress sidecar needs. The validator lets Kata through, and the release's secure-runtime guide recommends Kata for sandboxes with egress control.

### server_config.py: every server change goes through it

`switch_runtime.sh` is a thin wrapper around `server_config.py`. Parts 5 and 6 use the same script for the egress image and the egress mode. It rebuilds the `opensandbox-server` Helm release from part 2's `values/server.yaml`, adds `values/server-gateway.yaml` when it exists, and changes only lines of the `configToml`:

```text
./server_config.py --show [--json]                      what the server runs with now
./server_config.py --runtime runc|kata-qemu|kata-clh    [secure_runtime] from values/runtime-<name>.toml
./server_config.py --egress-image base|<image>          [egress] image (an ECR image is checked first)
./server_config.py --egress-mode dns|dns+nft            [egress] mode
./server_config.py --baseline                           runc, the base egress image, dns+nft (part 2's)
./server_config.py --drop-gateway ...                   remove the ingress gateway on purpose
add --dry-run to see the config.toml change, checked with helm template, without upgrading
```

A setting you do not pass keeps its value from the release's last *deployed* Helm revision. A failed upgrade is never taken as the current state. So switching the runtime keeps part 5's egress image, and the other way round. A setting missing from that revision stops the script instead of becoming empty. The script also sets the two pod annotations part 2's `04_install.sh` sets.

Rules that keep concurrent or partial runs from losing a setting:

- **One change at a time.** A lock file, `logs/server_config.lock` next to the script, is held from reading the current state until the new server is verified. Every caller in parts 3 to 6 runs this one file, so they all share the lock. The revision is read again just before the upgrade, and if any other client changed the release meanwhile, nothing is upgraded.
- **The gateway cannot be dropped by accident.** When the running release has the gateway and `values/server-gateway.yaml` is missing or does not enable it, the script refuses. `--drop-gateway` removes it on purpose.
- **Checked before and after.** The composed values are rendered with `helm template` before the upgrade, and they must hold the wanted `[egress]`, `[secure_runtime]` and `[ingress]`. After the upgrade, the script reads the `config.toml` that the server pod was started with (a `subPath` mount, fixed for the pod's life) and checks it the same way. It also checks `/health` and `/version` through `kubectl exec`.
- **Ctrl-C does not kill Helm.** That would leave the release `pending-upgrade`; the script waits for Helm to finish instead.

Every run creates a new Helm revision, even when nothing changes; that repairs drift. Rebuilding the same settings leaves the manifests identical and the pod alone (about 17 s). When the config changes, the server pod is replaced: the Deployment uses `Recreate`, so the API is down for a few seconds.

The offline tests use a fake Helm release, the real chart, and no cluster:

```bash
uv run python -m unittest discover -s tests -v    # 23 tests: server_config.py and 99_cleanup.py's scoping
```

### 06: when things go wrong

- **A sandbox's pod is deleted.** The controller created a new pod with the same name before the script's first 2-second poll. The server kept reporting `Running`, and the SDK's next command went to the new pod. The file written before is gone: the new pod starts from the image, not from the old writable layer. Nothing was snapshotted. Only a paused sandbox has a snapshot.
- **A node is cordoned.** Four new sandboxes all landed on the other node; the script exits 1 if one does not. It skips this test when a node is already cordoned or not Ready, because it would undo that cordon at the end. Our BatchSandbox template (part 2) has no tolerations. The release's default template carries `tolerations: [{operator: Exists}]`. A plain pod with that toleration, pinned to the cordoned node, ran there: the default template would have put sandboxes on it too.
- `kubectl drain` would also evict the sandbox pods on the node: the first case, for each of them. The script cordons and does not drain.

The script uncordons the node at the end, also when it fails or is interrupted. It also marks the node with an annotation, so `99_cleanup.py` can uncordon it if the script was killed outright.

## Cleanup

```bash
uv run python 99_cleanup.py
```

It deletes, all scoped to this directory:

- the sandboxes with the metadata `example=opensandbox-lifecycle`, which every script here sets;
- the Pool `osb-pool`;
- the pods and Secrets of `06_failure.py` and `02b_token_fallback.py`;
- a cordon `06_failure.py` left;
- the snapshot images of this directory's own sandboxes. Their ids are recorded in `results/created-sandboxes.txt` and in the benchmark's results files. An image goes only once its sandbox no longer exists (not listed in any state, and no BatchSandbox).

Other parts' images, untagged images, and images of a sandbox that still exists (a paused one, for example) are left alone. The benchmark's own sandboxes do not carry our metadata: `03_resume_benchmark.sh` deletes any its run left, by the ids in its results file. Cleanup keeps the repository, the Secret and the controller's snapshot settings (parts 4 and 6 use them). Part 2's `99_teardown.sh` removes everything at the end of the series.

Between sessions, scale the nodes to zero with `../opensandbox-eks/08_scale.sh down`. Part 2's README puts the running cluster at about $1.01 an hour, the NLB included.
