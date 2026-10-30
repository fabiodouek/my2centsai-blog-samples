# Google AX on Amazon EKS

Companion code for the my2cents.ai deep dive on self-hosting [Google AX](https://github.com/google/ax)
(Agent Executor) on Amazon EKS.

AX runs on Agent Substrate, which Google documents for two targets: GKE and a local kind cluster. This folder runs both on EKS 1.37 with
Terraform for the AWS side, Agent Substrate and AX built from source and pushed to ECR, snapshots on
S3, and a small patch that lets Substrate's installer use an EKS overlay.

Video walkthrough: https://youtu.be/d2vtQcadRok

```text
laptop (ax CLI, ate-setup)
   │ kubectl / port-forward
   ▼
EKS 1.37 ── ate-system: ate-api-server, ate-controller, atenet router/egress, atelet (DaemonSet), postgres
         ── ax-system:  ax-server, ax-postgres, WorkerPool ax-workers (gVisor)
   │
   ├── S3   ax-eks-snapshots-<account>   (actor snapshots, via EKS Pod Identity)
   └── ECR  ax-eks/substrate/*, ax-eks/ax/*   (created on push)
```

## Requirements

- An AWS account and credentials that can create EKS, VPC, IAM, S3 and ECR resources
- Terraform 1.9+, AWS CLI v2, `kubectl`, `jq`, Docker with `buildx` (builds `linux/amd64` images;
  Docker Desktop on Apple Silicon works)
- Go 1.26+ (the scripts set `GOTOOLCHAIN=auto`, so Go fetches the 1.27.x toolchain both projects pin)

## Why EKS 1.37

Substrate depends on `PodCertificateRequest`, `ClusterTrustBundle` and the `podCertificate`
projected volume. They are GA and on by default from Kubernetes 1.37. On 1.36 they are beta, and
EKS gives you no way to turn beta APIs on, so 1.36 and older can't run Substrate.

## Scripts

| Script | What it does |
|---|---|
| `01_cluster.sh` | `terraform apply` (VPC, EKS 1.37 with two `m7i.xlarge` AL2023 nodes, S3 bucket, Pod Identity roles, ECR create-on-push template), kubeconfig, then checks the certificate APIs, that a `podCertificate` projection survives admission, and the node kernel |
| `02_sources.sh` | Checks out AX and Substrate at the commits AX's install guide pins, applies the `ate-setup` patch and copies in the EKS overlay |
| `03_substrate.sh` | `ate-setup deploy ate-system` with the EKS overlay: builds every Substrate image with ko, pushes to ECR, installs the control plane |
| `04_workerpool.sh` | Creates the gVisor `WorkerPool` AX schedules tasks on (the control plane install doesn't) |
| `05_ax.sh` | Builds the `ax` CLI and the `ax-server` / `ax-task-runner` images, deploys PostgreSQL and `ax-server` |
| `06_check_task.sh` | The install guide's smoke test: create, resume, `ax ssh`, delete, with timings |
| `07_suspend.sh` | Writes to `/tmp`, `/workspace` and starts a background process, suspends and resumes: only `/workspace` comes back |
| `99_teardown.sh` | Deletes tasks, the ECR repositories, everything Terraform created and the PostgreSQL EBS volumes (Terraform doesn't own those) |

Run them in order. Builds and install records go to `.src/` and `.records/` (both git-ignored).

## What's EKS-specific

- **`substrate-eks/overlay/`**: a kustomize overlay alongside Substrate's `base` (GKE) and `kind`
  overlays. It sets the objectstore-plugin sidecars to S3 with Pod Identity credentials, mounts the
  AL2023 ECR credential provider (`/etc/eks/image-credential-provider`) for atelet's image pulls,
  and drops GKE-only objects (the router `PodMonitoring`, the managed OTel collector endpoint).
- **`substrate-eks/ate-setup-overlay.patch`**: `ate-setup` picks the kind or GKE bundle. The patch
  adds an `ATE_SYSTEM_OVERLAY` override and skips the GCS transfer-service-account check when it is
  set. 11 lines.
- **`scripts/docker-credential-ax-eks`**: a Docker credential helper for the ECR registry, so
  pushes work without `docker login` (which fails non-interactively against the macOS keychain).

## Cost

While it runs: EKS control plane $0.10/h, two `m7i.xlarge` about $0.40/h, one NAT gateway about
$0.05/h plus data, and about $0.08/h of gp3 storage (Substrate's bundled PostgreSQL requests a
500 GiB volume). Roughly $0.63/h, or $15/day. Run `99_teardown.sh` when you're done.

## Security notes

This is an evaluation setup:
- The EKS API endpoint is public. Set `endpoint_public_access_cidrs` for anything longer-lived.
- AX's control plane has no authentication of its own ([google/ax#376](https://github.com/google/ax/issues/376));
  it is reachable only through `kubectl port-forward`.
- `ate-client` is a Substrate bootstrap owner, as in Substrate's own dev installs. Substrate's
  docs warn against that outside development.
- On S3, Substrate's snapshot sidecars use the pod's role for every transfer (on GCS each transfer
  gets a token scoped to one actor), so atelet's role can read every snapshot in the bucket.
