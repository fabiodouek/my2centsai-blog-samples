# OpenShell sandbox lifecycle and isolation on Amazon EKS

Companion code for part 3 of the my2cents.ai series "OpenShell on Amazon EKS": what a sandbox's
life looks like on the cluster from part 2 (restart policies, generations, stop and start,
losing a Pod), and what the workload can and can't do (Landlock, seccomp, no credentials, user
namespaces). OpenShell **0.1.6**, sandboxes on runc.

Video walkthrough: [youtu.be/Pz8BrbW9U_Y](https://youtu.be/Pz8BrbW9U_Y)

## Requirements

- The `osh-eks` cluster and gateway from [`../openshell-eks`](../openshell-eks) (part 2): scripts
  01 to 05 done, the port-forward running (`../openshell-eks/09_scale.sh up` restarts it)
- The OpenShell CLI 0.1.6, `kubectl`, `helm`, `jq`, `python3`

## Scripts

| Script | What it shows |
|---|---|
| `01_lifecycle.sh` | The main process under the restart policies `never`, `on-failure` and `always`; a new generation on every restart; `stop` and `start`; `suspend` (unsupported in 0.1.6); a `--no-keep` sandbox deleting itself |
| `02_persistence.sh` | What survives stop and start (the `/sandbox` volume yes; `/tmp` and processes no); starting a sandbox while the only node in its volume's zone is cordoned |
| `03_supervisor_loss.sh` | Deleting the supervisor Pod, then the workload Pod: what OpenShell does, and how to get the sandbox back |
| `04_isolation.sh` | Inside one sandbox: identity, capabilities, seccomp, Landlock paths, forbidden syscalls, no ServiceAccount token, no network under the default policy |
| `05_user_namespaces.sh` | `server.enableUserNamespaces=true`: `hostUsers: false` and the UID map; switched back at the end |
| `99_cleanup.sh` | Deletes the sandboxes these scripts create, uncordons nodes, switches user namespaces off |

```bash
./01_lifecycle.sh
./02_persistence.sh
./03_supervisor_loss.sh
./04_isolation.sh
./05_user_namespaces.sh
./99_cleanup.sh
```

`02_persistence.sh` cordons a node for about two minutes, and `05_user_namespaces.sh`
restarts the gateway twice; both put things back, also when interrupted.

## What I measured

| | Result |
|---|---|
| `--restart-policy never`, command exits 3 | `Error` |
| `on-failure` / `always` | Restarts with a growing delay; each restart replaces both Pods and starts a new generation (6 to 20 s from exit until Ready again) |
| `stop` / `start` | 7.5 to 8.7 s / 11.7 to 12.7 s; both Pods go, the workspace volume stays, the generation changes |
| `suspend` | "compute driver does not support sandbox suspend" |
| Supervisor Pod deleted | `exec` refused at once; the sandbox is stopped about a minute later; `start` brings it back with its workspace |
| Workload Pod deleted | The controller recreates it, but it keeps the `openshell.ai/bootstrap` scheduling gate and never runs; `Error` after about 30 s; `stop` then `start` brings it back with its workspace |
| `start` while the only node in the volume's zone is cordoned | The workload Pod stays `Pending` (volume node affinity) and the sandbox `Starting`; `Ready` 13 s after uncordon. The supervisor Pod landed in the other zone |
| User namespaces | `hostUsers: false`; UID 0-65535 inside maps to a high unprivileged range on the node |
