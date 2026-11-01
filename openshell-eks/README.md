# OpenShell on Amazon EKS: a self-hosted install

Companion code for part 2 of the my2cents.ai series "OpenShell on Amazon EKS": OpenShell
**0.1.6** on Amazon EKS **1.37**, built with Terraform and a handful of scripts, with every image
copied into ECR, VPC CNI network policy enforcement on, the gateway reachable through a
port-forward (and, for one test, through an IP-restricted NLB), and Kata ready as a per-sandbox
VM runtime. Parts 3 to 6 of the series run on this cluster.

Video walkthrough: https://youtu.be/ShSChGiXwmY

## What it builds

- A VPC with one NAT gateway, and EKS 1.37 (`osh-eks`) with two `m7i.2xlarge` AL2023 nodes with
  nested virtualization (so `/dev/kvm` exists for Kata), IMDSv2 with a hop limit of 1
- Add-ons: VPC CNI with `enableNetworkPolicy`, CoreDNS, kube-proxy, EKS Pod Identity Agent, EBS
  CSI with a default gp3 StorageClass; the AWS Load Balancer Controller (Helm, Pod Identity)
- ECR repositories under `openshell-eks/` (created on first push): the OpenShell gateway,
  supervisor and sandbox images, the Agent Sandbox controller, the Ubuntu base and a workload
  image with the AWS CLI, boto3, git and gh
- Agent Sandbox v1.0.3 (the controller OpenShell's Kubernetes driver depends on) and the OpenShell
  Helm chart 0.1.6 in namespace `openshell` (Pod Security `restricted`), telemetry off
- Kata Containers (kata-deploy) with RuntimeClass `kata-qemu`, labelled for OpenShell

## Requirements

- An AWS account and credentials that can create all of the above (the `default` profile)
- Terraform >= 1.9, the AWS CLI v2, kubectl, Helm (3.x or 4.x), Docker with buildx, `jq`,
  `envsubst` (gettext), [`uv`](https://docs.astral.sh/uv/)
- The OpenShell CLI 0.1.6 (see `../openshell-architecture/00_gateway.sh`)

No secrets are needed: the gateway's PKI is generated in the cluster, and the CLI's client
certificate is copied from it into `~/.config/openshell/gateways/osh-eks/mtls/`.

## Scripts

| Script | What it does |
|---|---|
| `01_cluster.sh` | Terraform apply, kubeconfig, the load balancer controller, then checks: Kubernetes 1.37, two Ready nodes, `/dev/kvm`, **Landlock active in the node kernel (gate 1)**, IMDS settings, Pod Identity agent, VPC CNI network policy agent, default StorageClass. Writes `env.sh` |
| `02_kata.sh` | kata-deploy, RuntimeClasses, the `openshell.ai/sandbox-attachable=true` label, one pod in a Kata VM (**gate 3, part a**) |
| `03_images.sh` | Copies the images in `images.txt` into ECR with their multi-arch indexes, builds and pushes the workload image, verifies every digest (`--verify-only` checks without copying) |
| `04_fence.sh` | What the VPC CNI enforces before OpenShell relies on it: deny-all egress, the startup window in standard mode, a fenced pod reaching its own node's IP, strict mode (then back to standard) |
| `05_install.sh` | Agent Sandbox v1.0.3, the chart, namespace `openshell`, the NLB Service, `helm upgrade --install` with `values/openshell.yaml`, the client bundle, a port-forward on `127.0.0.1:17680` and the CLI gateway `osh-eks` (`--show-preflight` first shows the chart's error without Agent Sandbox) |
| `06_remote.sh` | The CLI through the NLB, allowed for your current IP only (`--delete` removes the NLB) |
| `07_first_sandbox.py` | First sandboxes: cold and warm create times, the Kubernetes objects one sandbox becomes, a use test; exits 2 with diagnostics if a sandbox can't reach its supervisor (**gate 2**) |
| `08_kata_sandbox.sh` | A sandbox in a Kata VM through `--driver-config-json` (**gate 3, part b**: fails on 0.1.6, see below), and the refusal without the RuntimeClass label |
| `09_scale.sh` | `down`: zero nodes between sessions; `up`: two nodes, gateway, port-forward |
| `98_delete_sandboxes.py` | Deletes every sandbox and reports leftover workspace PVCs |
| `99_teardown.sh` | Removes everything, ECR repositories and leftover EBS volumes included, and checks |

```bash
./01_cluster.sh
./02_kata.sh
./03_images.sh
./04_fence.sh
./05_install.sh --show-preflight
./06_remote.sh
uv run python 07_first_sandbox.py
./08_kata_sandbox.sh
./06_remote.sh --delete
./09_scale.sh down       # between sessions
./99_teardown.sh         # when you're done with the whole series
```

Every script is safe to run again: a second run converges instead of failing or duplicating.

## Configuration files

| File | Why |
|---|---|
| `terraform/` | The cluster. Versions, the AMI release and the add-ons are pinned; VPC CNI network policy is on |
| `values/openshell.yaml` | The chart values: images from ECR at tag `0.1.6`, our workload image as the default, telemetry off, per-sandbox driver config allowed, gateway requests |
| `manifests/gateway-nlb.yaml` | Our own internet-facing NLB Service for the gateway: TCP passthrough on 8080 only, one source address |
| `manifests/fence-test/` | The pods and the deny-all policy `04_fence.sh` uses |
| `workload/Dockerfile` | The workload image: the pinned Ubuntu base plus the tools the tests use, running as UID 1000 |
| `images.txt` | Source images and their ECR paths |

## The three gates

1. **Landlock** must be active in the node kernel (in `/sys/kernel/security/lsm`, ABI 3 or newer),
   or OpenShell's sandbox runtime refuses to start. `01_cluster.sh` checks it on every node.
   *Result: passed (EKS 1.37 AL2023 AMI, kernel 6.18.56, Landlock ABI 7).*
2. **The control path**: a sandbox's workload Pod reaches its supervisor through the supervisor's
   Service, under a NetworkPolicy that names the supervisor Pod. If the CNI doesn't admit that,
   sandboxes stay in `Provisioning`. `07_first_sandbox.py` exits 2 and prints the evidence.
   *Result: passed (VPC CNI with network policy enforcement).*
3. **Kata on nested virtualization**: `02_kata.sh` and `08_kata_sandbox.sh`.
   *Result: a plain Kata pod runs (guest kernel 6.18.35); an OpenShell 0.1.6 sandbox under Kata
   doesn't: the Kata VM's agent dies while the workload Pod's `workspace-init` container seeds the
   workspace volume (exit 255), with or without CPU and memory limits. The other parts of the
   series run sandboxes on runc.*

## Gotchas this directory works around

| What happens | Fix here |
|---|---|
| `openshell gateway add --local` fills the registration's mTLS directory with the certificates of a local OpenShell gateway on the same machine; the cluster gateway then fails with `invalid peer certificate: BadSignature` | `05_install.sh` registers first, then writes the cluster's client bundle |
| The chart's Service can't take load balancer annotations or source ranges, and also publishes the unauthenticated metrics port 9090 | Our own NLB Service in `manifests/gateway-nlb.yaml`, port 8080 only |
| The PKI hook never re-issues the server certificate | `05_install.sh` creates the NLB first and puts its hostname in `pkiInitJob.serverDnsNames` |
| `DeleteSandbox` blocks until the Pods and the EBS volume are gone (about 30 s), past the Python SDK's default 30 s RPC timeout | `common.py` creates the client with a 120 s timeout |
| The workload runs as UID 10001 whatever the image's `USER` says | Nothing to fix; the driver sets it |
| Telemetry is on by default | `server.telemetryEnabled: false` |
| Port 8080 may be taken on a laptop | The port-forward uses 127.0.0.1:17680 |

## Cost

About $0.97 an hour with both nodes up (two `m7i.2xlarge` at $0.4032/h, the EKS control plane at
$0.10/h, one NAT gateway at $0.045/h, plus small change), about $23 a day. The NLB adds about
$0.03/h while it exists. `./09_scale.sh down` leaves about $0.15/h (control plane and NAT gateway).

## Telemetry

OpenShell sends anonymous usage telemetry unless `OPENSHELL_TELEMETRY_ENABLED=false` is set on the
gateway; the chart's switch is `server.telemetryEnabled`, which is `true` by default.
`values/openshell.yaml` sets it to `false`, and `05_install.sh` checks the gateway's environment.
