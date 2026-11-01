# OpenShell on Amazon EKS: stress testing two nodes

Companion code for part 6 of the my2cents.ai series "OpenShell on Amazon EKS": how far two
`m7i.2xlarge` nodes go with OpenShell **0.1.6**, and what gives first. Six tests: density until
something refuses, create latency under concurrency, policy propagation, the egress overhead
ladder, control-plane load, and noisy neighbours. Two ways of watching: a kubelet sampler every
2 s and Container Insights. Sandboxes run on runc. Everything runs on the cluster from
[`../openshell-eks`](../openshell-eks) (part 2), with nothing added to it except Container Insights
(`00_container_insights.sh`).

Video walkthrough: [youtu.be/tlbDLxuo5Z0](https://youtu.be/tlbDLxuo5Z0)

## Requirements

- The `osh-eks` cluster and gateway from [`../openshell-eks`](../openshell-eks): scripts 01 to 05 done,
  the port-forward on `127.0.0.1:17680` (or `$LOCAL_PORT`) running (`../openshell-eks/09_scale.sh up`
  restarts it)
- The echo server from [`../openshell-egress`](../openshell-egress) (`00_echo.sh`), and its
  `echo-demo` provider profile imported on the gateway (for the credential rung of test 4)
- The OpenShell CLI 0.1.6, `kubectl`, `helm`, `jq`, Terraform (only for `00_container_insights.sh`),
  [`uv`](https://docs.astral.sh/uv/)
- `uv sync` here (Python 3.11+, the OpenShell SDK 0.1.6, matplotlib, PyYAML). The scripts import
  `../openshell-eks/common.py` (the SDK client with a 120 s timeout) rather than copying it.

Every sandbox, provider and Pod these scripts create is named `p6-...`; deletes go through a check
that refuses any other name. Each test expects an otherwise empty cluster (it says so when it
isn't) and leaves it empty.

## Scripts

| Script | What it does |
|---|---|
| `00_container_insights.sh on\|off` | Container Insights (metrics only) through part 2's Terraform: plans, shows the summary, asks, applies. `on` also writes `container_insights.auto.tfvars` there, so re-running `../openshell-eks/01_cluster.sh` (when your public IP changes) keeps the add-on; `off` removes the file. `../openshell-eks/99_teardown.sh` deletes the log group the agent creates |
| `sampler.py` | Every 2 s, one CSV row per node: node CPU, memory and pressure (kubelet summary API), Pods, sandbox Pods, unscheduled Pods, attached EBS volumes (VolumeAttachments), NetworkPolicies, PolicyEndpoints, the gateway's CPU, memory and SQLite volume, the shared supervisor pool's CPU and memory. `--metrics-out` adds the gateway's own `/metrics`. Most tests start it in the background and stop it at the end (`04` doesn't); it also runs on its own |
| `01_density.py` | Creates sandboxes in batches until a create fails or one stays not Ready past a deadline, then names the cause from the stuck sandboxes' Pods, PVCs, VolumeAttachments and events. `--variant dedicated\|shared\|shared-1gi` |
| `02_create_latency.py` | 1, 5, 10, 20, 40 creates at once; create→Ready per sandbox, stuck count, the gateway's mutation-lock wait per level. `--topology dedicated\|shared` |
| `03_policy_propagation.py` | N sandboxes with a probe each (`probes/flip.py`); one change allows the echo server, each probe records its first allowed request; a second change cuts long-lived responses. `--delivery poll\|push`, `--scope sandbox\|global` |
| `04_egress_ladder.py` | One sandbox, the same benchmark (`probes/bench.py`) under rising policy work: direct (no OpenShell), `tls: skip`, L4, REST audit, REST enforce, a substituted credential; `--https` adds an AWS endpoint (`tls: skip`, TLS terminated, REST enforce). Latency p50/p95/p99, requests/s, upload MiB/s |
| `05_control_plane.py` | Parallel API clients (1 to 64) cycling list, get and exec against a few sandboxes; gateway CPU, memory and metrics per step. Clients run in a Pod in the cluster (`--from cluster`, default) or on the laptop |
| `06_noisy_neighbors.py` | CPU and disk hog sandboxes on the same node as a probe sandbox (`probes/noisy_probe.py`: CPU work, fsync on its EBS volume and on the node's root volume, a request to the echo server); `--hog-cpu 1` limits the hogs |
| `charts.py` | Light and dark PNG pairs from the CSVs, 2880 px wide, into `charts/` (or `--out DIR`) |
| `99_cleanup.sh` | Removes everything these scripts create and undoes the Helm values they apply, for when a script was killed hard (`--no-helm` skips the release) |

Configuration: `policies/` (the sandbox policies, one per ladder rung, plus the global-policy pair),
`values/` (the three Helm overlays), `manifests/` (the baseline Pod for test 4, the load-client Pod
for test 5), `probes/` (the scripts that run inside sandboxes).

## Run order

Each run below starts from zero sandboxes and deletes everything it created.

```bash
uv sync
../openshell-egress/00_echo.sh                                   # the in-cluster echo server (part 4)
openshell -g osh-eks profile import -f ../openshell-egress/profiles/echo-demo.yaml   # for test 4's credential rung
export P6_RESTART_PORT_FORWARD=1    # let the scripts restart a dead port-forward (default: wait for it)
./00_container_insights.sh on
uv run python 01_density.py --variant dedicated
uv run python 01_density.py --variant shared
uv run python 01_density.py --variant shared-1gi
uv run python 02_create_latency.py --topology dedicated
uv run python 02_create_latency.py --topology shared
uv run python 03_policy_propagation.py --count 20 --delivery poll
uv run python 03_policy_propagation.py --count 20 --delivery push
uv run python 04_egress_ladder.py --https
uv run python 04_egress_ladder.py --https --supervisor other
uv run python 05_control_plane.py
uv run python 06_noisy_neighbors.py
uv run python 06_noisy_neighbors.py --hog-cpu 1
uv run python charts.py                       # into charts/; --out DIR for elsewhere
./99_cleanup.sh
./00_container_insights.sh off
```

Results go to `results/` (`--results DIR` or `P6_RESULTS` to change it). Every test script has a
smoke-size invocation at the top of its docstring; `--print-helm` on 01, 02 and 03 prints the Helm
commands a variant would run, without running them.

## What changes the gateway, and how it goes back

| Test | Change | How it reverts |
|---|---|---|
| `01 --variant shared`, `02 --topology shared` | `helm upgrade --reuse-values -f values/shared-supervisor.yaml`: `gatewayConfig."openshell.drivers.kubernetes".supervisor_topology: gateway`, `supervisor.replicas: 2`, one member per node (required anti-affinity) | `helm rollback openshell <revision before>` at the end of the run, also on Ctrl-C, SIGTERM or an error; p6 sandboxes are deleted first (they lose their supervisor when the pool goes) |
| `01 --variant shared-1gi` | the above plus `-f values/workspace-1gi.yaml`: `server.workspaceDefaultStorageSize: 1Gi` | the same rollback |
| `03 --delivery push` | `-f values/push-delivery.yaml`: `gatewayConfig."openshell.gateway".config_delivery_mode: push` | the same rollback |
| `03 --scope global` | `openshell policy set --global` (replaces every sandbox's policy on the gateway) | `openshell policy delete --global` at the end; the script refuses to start if other sandboxes exist |
| `04` | provider `p6-echo` (profile `echo-demo`, a dummy token read from the environment, never the command line) and Pod `p6-direct` in `default` | both deleted at the end |
| `05 --from cluster` | Pod `p6-load` in `openshell`, mounting the CLI's client certificate (Secret `openshell-client-tls`) | deleted at the end |
| `00_container_insights.sh` | the CloudWatch Observability add-on (one agent Pod per node) | `./00_container_insights.sh off` |

The overlays use the chart's dotted top-level keys (`gatewayConfig."openshell.gateway"`,
`gatewayConfig."openshell.drivers.kubernetes"`), which is what the chart accepts: a nested
`gatewayConfig.openshell.gateway` map (what an unescaped `--set gatewayConfig.openshell.gateway...`
produces) is rejected as defining the table twice. On the command line the equivalent is
`--set 'gatewayConfig.openshell\.gateway.config_delivery_mode=push'`.

Every gateway change restarts the gateway Pod, so the script restarts the port-forward afterwards
(same pid file and log as part 3). After the upgrade the script checks that the rendered
`gateway.toml` (ConfigMap `openshell-config`) has the new setting; after the rollback it checks that
the release values and the rendered `gateway.toml` are exactly what they were, and warns if not.
`99_cleanup.sh` is the fallback when a script couldn't run its own revert: it takes the release's
current values, removes the part 6 keys (delivery mode set back to an explicit `poll`), applies them
with `--reset-values`, and checks the rendered `gateway.toml`. (`--reuse-values` with `--set key=null`
can leave a `gatewayConfig` key in the rendered file.)

## If the port-forward drops

The SDK and CLI reach the gateway through `kubectl port-forward`, which dies with the gateway Pod or
a network blip. A call that failed because the connection was refused never reached the gateway, so
the scripts wait for it (up to `P6_GATEWAY_WAIT_S`, default 600 s), print that they are waiting, and
send the call again on a new channel; with `P6_RESTART_PORT_FORWARD=1` they restart the
port-forward themselves. A call that failed mid-flight is reported, never repeated. A sandbox isn't
counted as stuck for the time the gateway couldn't be reached, and a create that never got through
is recorded as a transport failure, not as a refusal.

## Things to know before reading the numbers

- **Supervisor placement.** The workload Pod can be pinned (driver config), the supervisor Pod
  can't. When it lands on the other node, every request crosses nodes twice: in my smoke runs a
  new-connection GET through the supervisor took about 7 ms cross-node and about 3 ms same-node.
  `04` recreates the sandbox until the supervisor lands where `--supervisor` asks.
- **The echo server's keep-alive responses wait out a delayed ACK** (about 40 ms, in the direct
  baseline too): it writes headers and body separately. Read the new-connection numbers for echo,
  keep-alive only for the HTTPS rungs.
- **In poll mode, the delay varies from change to change.** In the smoke runs (3 sandboxes) every
  sandbox loaded a revision within about 25 ms of the others; in the recorded run with 20
  sandboxes the delays spread across the 10-second poll interval. `03` times 10 changes by default
  so the distribution comes from repeats as well as sandboxes.
- **From the laptop, every API call pays the port-forward** (Health took about 94 ms from the laptop
  and about 1 ms from a Pod in the smoke runs), so `05` runs its clients inside the cluster by default.
- **A delete right after a sandbox becomes Ready can orphan it** (seen once in the smoke runs, 0.1.6):
  `DeleteSandbox` returned in 26 ms instead of about 30 s, the gateway dropped the sandbox (no longer
  listed, its name refused with "already exists"), and its Sandbox CR, both Pods, Services, Secrets,
  NetworkPolicy and EBS volume stayed. After every delete the scripts check that the `Sandbox` CR is
  gone too, and delete a leftover one through Kubernetes with a warning (its owner references take the
  rest); `99_cleanup.sh` does the same for any `default--p6-*` CR the gateway doesn't list.
- **`/dev/zero` is not readable under the default policy** (Landlock): the disk hogs in `06` use
  `policies/hog.yaml`, which adds it, and the script checks that they are really writing.
- **The kubelet's CPU figure lags.** `usageNanoCores` is a rate over cAdvisor's last 10 to 15 s, so
  `05` computes the gateway's CPU per step from the cumulative counter and its timestamps; keep
  steps at 30 s or more.
- **A provider attach is checked against the policy a sandbox was created with** (0.1.6): a sandbox
  created with an L4-only rule for a host refuses a later attach of a credentialed provider for that
  host (`FAILED_PRECONDITION: credentialed endpoint ... uses L4-only`), even after the rule is gone.
  `04` creates its sandbox with no network rules and applies each rung with `policy set`.

## Results

From the recorded runs on 2026-11-01 (two `m7i.2xlarge`, EKS 1.37, OpenShell 0.1.6, runc):

| Test | Result |
|---|---|
| Density, dedicated supervisors | 46 Ready; stopped by Pods per node (58) |
| Density, shared supervisor pool | 61 Ready; stopped by EBS attachments per node (31) |
| Density, shared pool, 1 GiB workspaces | 61 Ready; same limit |
| Shared pool ready after the Helm change | another 49 to 110 s after the Deployment rollout (canary) |
| Create latency, 1 / 40 at once | dedicated p50 9.2 s / 32.4 s; shared p50 12.3 s / 17.5 s; 0 stuck or failed |
| Policy propagation, 20 sandboxes × 10 changes | poll p50 4.96 s, max 10.0 s; push p50 0.07 s, max 0.39 s; 200/200 open streams cut |
| Egress, new connection to echo, p50 | direct 1.11 ms; tls-skip 2.66; L4 3.40; REST audit 2.87; REST enforce 3.35; credential 2.52 ms |
| Egress, HTTPS keep-alive p50 | direct 3.06 ms; tls-skip 3.27; terminated 3.35; REST enforce 3.57 ms |
| Gateway API load | about 2,040 calls/s at 32 to 64 clients, about 2.7 cores, 0 errors |
| Noisy neighbours | CPU hogs: probe CPU work 10.6 → 20.6 ms (15.0 ms with 1-CPU limits); disk hogs: root-volume fsync 1.4 → 51.5 ms, own EBS unaffected |

Known gaps: `04 --supervisor other` couldn't run (the scheduler placed the supervisor on the workload's node 10 times out of 10); the direct baseline's throughput and upload runs failed in the harness (`OSError`), so only its latency is reported.
