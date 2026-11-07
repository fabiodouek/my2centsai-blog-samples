# OpenSandbox on Amazon EKS

Companion code for the my2cents.ai series "OpenSandbox on Amazon EKS". It builds an Amazon EKS cluster with Terraform, installs OpenSandbox 1.2.0 (server, controller, ingress gateway and developer console) from its Helm charts, adds Kata Containers for VM isolation, and runs a first sandbox on it from Python.

Video walkthrough: https://youtu.be/J7oviaww6oo

Parts 3 to 6 of the series use scripts that need this cluster (and `common.py` from this directory). Build it first, and keep it until you have finished part 6 or are done with the series, then run `./99_teardown.sh`.

## What you get

- An EKS 1.37 cluster `osb-eks` in `us-east-1`, with a managed node group of two `m7i.2xlarge` nodes (AL2023, nested virtualization enabled, IMDSv2 required).
- The AWS Load Balancer Controller, the EBS CSI driver, and Kata Containers (`kata-qemu`, `kata-clh`) installed by `kata-deploy`.
- Private ECR repositories `opensandbox-eks/*` holding the OpenSandbox images, the console images and a `python:3.12-slim` workload image.
- The OpenSandbox lifecycle server, controller and ingress gateway, and the developer console.
- A Network Load Balancer in front of the ingress gateway that accepts connections from your public IP (a `/32`) only.

The lifecycle server API and the console are never exposed: you reach them with `kubectl port-forward`.

## Requirements

- An AWS account and a profile with enough rights to create an EKS cluster, a VPC, IAM roles and ECR repositories. The scripts use your default profile (`AWS_PROFILE` works as usual).
- Terraform 1.9 or later, the AWS CLI v2, `kubectl`, Helm (tested with 4.3.1), `jq`, `curl`, `openssl`, `envsubst` (from gettext).
- Docker with buildx (for `03_images.sh` and `06_console.sh`; the console images are built for `linux/amd64`, under emulation on an Apple Silicon Mac).
- [`uv`](https://docs.astral.sh/uv/) for the Python scripts.

Copy `.env.example` to `.env` if you want to choose your own values; otherwise the scripts generate them and write them to `.env` (mode 600, gitignored). `.env` holds:

| Variable | What it is |
|---|---|
| `OSB_API_KEY` | The lifecycle server's API key (header `OPEN-SANDBOX-API-KEY`). `04_install.sh` stores it in the Secret `opensandbox-server-auth`. The Python scripts and the console read it from here. |
| `BFF_SESSION_SECRET`, `BFF_ADMIN_TOKEN` | The console's session-cookie secret and its admin login. `06_console.sh deploy` generates them. |

No script prints these values. `env.sh` is written by `01_cluster.sh` from the Terraform outputs (account-specific names, the ECR registry host, the VPC id) and is gitignored too.

## Scripts and run order

Run them in order from this directory. Each one can be run again safely: it converges and says what changed.

| Script | What it does | Time (measured) |
|---|---|---|
| `01_cluster.sh` | Terraform: VPC, EKS, node group, IAM, ECR settings. Installs the AWS Load Balancer Controller, writes `env.sh`, checks `/dev/kvm` on the nodes. | about 14 min first run |
| `02_kata.sh` | Installs Kata Containers with `kata-deploy` and runs a test pod under `kata-qemu`, `kata-clh` and `runc`. | about 1.5 min |
| `03_images.sh` | Copies the OpenSandbox images and `python:3.12-slim` into ECR and checks every digest against the source. | minutes; depends on registry rate limits (below) |
| `04_install.sh` | Installs the base chart (CRDs), the controller and the server, with a PVC for the server's SQLite file. Creates `OSB_API_KEY` if missing. | about 1.5 min |
| `05_gateway.sh` | Installs the ingress gateway and the NLB limited to your IP, and points the server at it. | about 4 min (the NLB takes 2 to 3 min) |
| `06_console.sh` | Builds the console images, pushes them to ECR and deploys the console. | first build 1m 09s without cache, deploy 25 s, re-run 13 s |
| `07_first_sandbox.py` | Creates a sandbox from the ECR image (cold, warm, five warm), runs a command and a file round trip through the server proxy and through the gateway, runs `osb sandbox list`. | under 1 min |
| `08_scale.sh down\|up` | Scales the node group to 0 or back to 2. `up` waits for Ready nodes and checks that Kata is installed on the new ones, the OpenSandbox pods, the server's volume and the NLB targets. (`nodes-only` does just the first half of `up`; `99_teardown.sh` uses it.) | `down` about 17 min (in the recorded run the first node was gone after about 3 minutes and the second after about 17, and the nodes bill until they are gone); `up` about 3 min |
| `98_delete_sandboxes.py` | Deletes every sandbox the server lists, past the first page. Prints `deleted N`. | seconds |
| `99_teardown.sh` | Deletes everything and checks for leftovers. Run once, at the end. | about 13 min (13m 14s in the recorded run; terraform destroy 11m 08s) |

`common.py` is shared by the Python scripts: `connection("proxy" or "gateway")`, `require_server()` and `delete_all_sandboxes()`. `pyproject.toml` pins `opensandbox==1.2.0` and `opensandbox-cli==1.2.0`.

### Running the Python scripts

Start the port-forward in a second terminal and leave it running:

```bash
kubectl -n opensandbox-system port-forward svc/opensandbox-server 18080:80
```

It uses local port **18080**, not 8080: another service on your machine may already hold 8080 and answer `404`, which looks like a broken server. Then:

```bash
uv run python 07_first_sandbox.py
```

If the port-forward is not running, the scripts print one line (`Server not reachable at 127.0.0.1:18080: start kubectl ...`) and exit 1. A wrong `OSB_API_KEY` also gives one line.

`07_first_sandbox.py` removes the python image from both nodes first (a privileged debug pod runs `ctr images rm` on the node), so that the first create is cold. Set `SKIP_COLD_PREP=1` to skip that. It deletes only the sandboxes it created. It writes `osb.toml` (gitignored) for the `osb` CLI; the API key goes to `osb` through the environment, not the file.

### Reaching the developer console

```bash
kubectl -n opensandbox-system port-forward svc/opensandbox-console-web 5173:80
```

Open `http://localhost:5173`. Sign in as a developer with the value of `OSB_API_KEY` from `.env`; the admin login uses `BFF_ADMIN_TOKEN`. The console's interface is in Chinese only in release 1.2.0. There is no ingress for the console.

## ECR: scanning, rate limits

- **Scan on push is registry-wide.** ECR sets scanning for the whole registry, not per repository. `01_cluster.sh` configures basic scan-on-push with a rule that matches `opensandbox-eks/*`, which replaces the registry's scanning configuration for the Region. Per-repository output (`scanOnPush: false`) therefore looks off but is expected. If the registry already has a configuration of its own, the script stops: set `ALLOW_ECR_SCAN_OVERWRITE=1` to replace it, or `MANAGE_ECR_SCAN_ON_PUSH=false` to leave scanning alone. `99_teardown.sh` resets the registry to the default (basic, no rules) and checks that.
- **Where the images come from.** The OpenSandbox images come from Docker Hub. Docker Hub documents 100 anonymous pulls per 6 hours, counted per IPv4 address or IPv6 /64 subnet (the rate-limit headers the author got said `100;w=3600`, one hour). Manifest GET requests count, HEAD requests don't, and a multi-arch pull counts once per architecture; if `03_images.sh` reports `Rate limited by: docker.io`, wait and run it again, and it copies only what is missing. The script ignores your own Docker logins on purpose. Python comes from Amazon ECR Public, which mirrors the official image with the same index digest as Docker Hub, and ECR Public has its own limits.

## What the charts install

`04_install.sh` and `05_gateway.sh` install the charts from the `release-1.2.0` tag with a few overrides in `values/`. Know what that grants before you use the cluster for anything else:

- **RBAC is cluster-wide.** The server's ClusterRole (bound by a ClusterRoleBinding) grants create, delete, get, list, patch, update and watch on pods, ConfigMaps, Services and events in every namespace; create, delete and get on Secrets; create, delete, get, list and patch on PVCs; and access to the `batchsandboxes`, `pools` and `sandboxsnapshots` custom resources, and read access to RuntimeClasses and namespaces. The controller's ClusterRole can create, delete and update pods and read Secrets cluster-wide, and has full rights on batch jobs. The base chart also creates ClusterRoles for the `fast-sandbox` components (including create and update on Roles and RoleBindings, and write access to pods, nodes, ConfigMaps and Secrets); this install uses the `batchsandbox` workload provider and none of those are used, but they exist, and so does the namespace `opensandbox-dataplane`.
- **The server runs without a `securityContext`.** It is the most privileged workload (it can create pods in any namespace) and the chart's defaults leave its container and pod security context empty, while the controller runs as non-root with all capabilities dropped. Do not read this install as a hardened one.
- **The console's backend** is the upstream image, which runs as root, and its ServiceAccount can read pods, events and Deployments cluster-wide. It is reachable only through the port-forward. The console's tenants Secret is mounted with `defaultMode: 0400`, which only works because the backend runs as root: if you add a non-root `USER` to its image, the file becomes unreadable.
- **Console image digests change on a rebuild without the build cache.** The backend's `requirements.txt` uses `>=` pins, so pip resolves newer packages and `06_console.sh build` on another machine (or after `docker buildx prune`) gives different digests. Do not treat a digest as fixed.
- **The gateway is cleartext HTTP.** The only control on who can reach it is the NLB's source range (your IP as a `/32`, recomputed by `05_gateway.sh` each run; set `MY_IP` to override). Signed routes (secure access) are not configured. If your IP changes, run `./01_cluster.sh` and `./05_gateway.sh` again.
- The server's SQLite file is on an encrypted `gp3` EBS volume in one availability zone, so the server pod can only run on a node in that zone.

## Costs

Rough prices in us-east-1, on-demand, from the AWS price list in October 2026; check the current prices before you rely on them.

| Item | Per hour |
|---|---|
| EKS control plane | $0.10 |
| 2 x `m7i.2xlarge` | $0.81 (0.4032 each) |
| NAT gateway (one) | $0.045, plus $0.045 per GB processed, and $0.005 for its public IPv4 address |
| 2 x 100 GiB `gp3` node root volumes | $0.02 |
| Network Load Balancer | $0.0225, plus $0.006 per LCU-hour of traffic (price list API, 2026-11-01) |
| The NLB's two public IPv4 addresses | $0.01 ($0.005 each) |
| ECR storage ($0.10 per GB-month), data transfer | not itemised here |

That adds up to about $1.01 an hour (about $24.27 a day) with the nodes running, the NLB's $0.0225 and its two IPv4 addresses ($0.01) included. At node group size 0 (`./08_scale.sh down`) the nodes and their root volumes go, and what remains is the control plane, the NAT gateway, the NLB, a 1 GiB volume and the images: roughly $0.18 an hour with the NLB and its IPv4 addresses. A scale-down takes about 17 minutes to finish (in the recorded run the first node was gone after about 3 minutes and the second after about 17), and the nodes bill until they are gone. That is not free, so tear everything down when you are done.

## Scaling down and tearing down

When you stop for the day:

```bash
./08_scale.sh down     # node group to 0; the nodes terminate
./08_scale.sh up       # back to 2 nodes; checks Kata, the OpenSandbox pods, and runs a pod under kata-qemu
```

The scale-down itself takes about 17 minutes, and the nodes bill until they are gone. The pods on the old nodes are gone afterwards. The nodes that come back are new instances, so `kata-deploy` installs Kata on them again and `up` waits for that. `./01_cluster.sh` keeps whatever size you set (it refuses to go on at 0 and tells you to run `up`).

To remove everything:

```bash
./99_teardown.sh      # asks you to type the cluster name; --yes skips the question
```

If the node group is at 0 it first scales it back to 2 and waits for Ready nodes (`./08_scale.sh nodes-only`, a couple of minutes and the cost of two nodes meanwhile), because the controllers that delete the load balancer and the volume need nodes. It then runs in this order, because controllers inside the cluster create AWS resources that Terraform does not know about and only they can delete them: sandboxes; the gateway's NLB Service (waits for the NLB to disappear); the console and the Helm releases (gateway, server, controller, base, kata-deploy, then the Load Balancer Controller); the PVC (waits for its EBS volume to disappear); the namespaces, the seven CRDs that Helm keeps and the `gp3-encrypted` StorageClass; every `opensandbox-eks/*` ECR repository; `terraform destroy`; then a check for leftovers (network interfaces and security groups in the VPC, load balancers, EBS volumes tagged for the cluster, ECR repositories) that fails if anything is found; and a check that the ECR scanning configuration is back at the default. If your public IP changed since `01_cluster.sh`, the EKS API refuses you: run `./01_cluster.sh` first so that it lets your new address in.

The recorded run in the part 2 video predates a fix to the script's error trap, which used to print its stop message from inside the checks that wait for the NLB, the volume and the cluster to be gone, where a "not found" error is the expected answer. The three "teardown stopped" lines in the video are that cosmetic bug; the run continued and ended clean.

## Notes

- `tests/test_teardown_trap.sh` checks `99_teardown.sh`'s error trap with a stub `aws` (no AWS account or cluster needed): the "is it gone yet?" checks print no stop message, and a real failure still prints it once and stops. Run it with `bash tests/test_teardown_trap.sh`.
- The `.src/` directory (OpenSandbox's source at the `release-1.2.0` tag, fetched by `04_install.sh`), `logs/`, `env.sh`, `.env`, `osb.toml` and `values/server-gateway.yaml` are generated and gitignored.
- Version pins: OpenSandbox `release-1.2.0` (images, charts, console source), PyPI `opensandbox==1.2.0` and `opensandbox-cli==1.2.0`, EKS 1.37, Kata Containers 3.32.0, AWS Load Balancer Controller chart 3.6.2.
