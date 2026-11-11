# AWS Nitro Sandboxes

Companion code for the my2cents.ai series on AWS Nitro Sandboxes, which AWS launched on 2026-11-09: hardware-isolated virtual machines carved out of a parent EC2 instance's CPUs and memory and run by the Nitro Hypervisor, not nested inside the instance. In confidential mode, the default, the parent can't read the sandbox's memory, and the sandbox can prove what it runs with a signed attestation document. You can use them three ways: directly on an EC2 instance with Firecracker, as Amazon ECS Managed Instances tasks, and as Amazon EKS pods.

Video walkthroughs: [explainer](https://youtu.be/to5AzFHzHUo) · [EC2](https://youtu.be/_ausIROu0Gw) · [ECS](https://youtu.be/BfcSVs7J2vs) · [EKS](https://youtu.be/2tRvC-QUdYI)

| Part | Post | Code |
|---|---|---|
| 1 | [AWS Nitro Sandboxes](https://my2cents.ai/deep-dive/nitro-sandboxes): the architecture, and how the three ways in compare | this README, [`nsm/`](nsm) |
| 2 | [AWS Nitro Sandboxes on EC2: Hands-On with Firecracker and Attestation](https://my2cents.ai/deep-dive/nitro-sandboxes-ec2) | [`ec2/`](ec2) |
| 3 | [AWS Nitro Sandboxes on Amazon ECS](https://my2cents.ai/deep-dive/nitro-sandboxes-ecs) | [`ecs/`](ecs) |
| 4 | [AWS Nitro Sandboxes on Amazon EKS](https://my2cents.ai/deep-dive/nitro-sandboxes-eks) | [`eks/`](eks) |

```text
aws-nitro-sandboxes/
├── nsm/nsm.py     stdlib-only Nitro Secure Module client, used by all three parts
├── ec2/           one m8g.xlarge, Firecracker 1.18.0, direct and confidential mode, KMS attestation
├── ecs/           Terraform for the dependencies, task definitions for isolated, shared and confidential tasks
└── eks/           Terraform for EKS Auto Mode 1.37, a Graviton NodePool, and pod manifests for isolated and confidential pods
```

The console is the intended way to turn the feature on: **Nitro Enclave: Enable** in the EC2 launch wizard, **Turn on Nitro Enclaves** on an ECS capacity provider, and **Isolation mode** in the ECS task definition form. Each README walks through the console and shows the AWS CLI equivalent under it (`--enclave-options Enabled=true`, `nitroEnclavesEnabled: true`, `trustedExecutionConfiguration`). Terraform only creates dependencies: a VPC, IAM roles and a log group for ECS, and a VPC and the cluster for EKS.

## Prerequisites

- An AWS account and credentials that can create IAM roles, KMS keys, VPCs and EC2 instances, plus ECS clusters or EKS clusters for parts 3 and 4.
- **AWS CLI 2.39.7 or later.** It is the first version with the ECS `trustedExecutionConfiguration` and `nitroEnclavesEnabled` fields. On launch day Homebrew still had 2.39.6; [AWS's own installer package](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) worked.
- The [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) for the AWS CLI, for the Session Manager shell on the EC2 parent and for ECS Exec.
- Terraform 1.6 or later (parts 3 and 4).
- kubectl 1.36 or later (part 4).
- `jq`.
- `bash` for the commands on your machine. macOS's default zsh splits unquoted variables differently.

Everything runs in **us-east-1** on **`m8g.xlarge`** (Graviton4, 4 vCPU, 16 GiB). The instance type has to support Nitro Sandboxes; Graviton has one thread per core, so every vCPU is a whole core.

## nsm/nsm.py

There is no CLI for the Nitro Secure Module (NSM) inside a sandbox, so this is a small client that uses only the Python standard library: a raw `ioctl` on `/dev/nsm` (`0xC0200A00`) and a minimal CBOR encoder and decoder, including the indefinite-length maps the NSM's documents use.

```bash
python3 nsm.py attest pub.der att.doc   # ask the NSM for a signed attestation document carrying this public key (DER)
python3 nsm.py pcrs att.doc             # print module_id, the digest and the non-empty PCRs
```

It runs inside a confidential sandbox: an AL2023 guest on EC2 (part 2), or an ECS task that receives it base64-encoded in `NSM_PY_B64` (part 3). `pcrs` decodes the document's payload; it does not verify the COSE signature or the certificate chain. AWS KMS does that when you pass the document as `--recipient`, which is the point of the exercise.

## Cost

Nitro Sandboxes have no additional charge; you pay for what they run on. In us-east-1 on demand, an `m8g.xlarge` was $0.17952 an hour. Part 3 adds a NAT gateway ($0.045 an hour plus data) and the ECS Managed Instances management fee; part 4 adds a NAT gateway, the EKS control plane ($0.10 an hour) and the Auto Mode fee. Each KMS key is $1 a month while it exists, prorated. Check current prices before you rely on these.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Cleanup

Each part's README ends with its own cleanup. In short:

- **EC2** ([`ec2/`](ec2#cleanup)): terminate the instance, delete its security group, schedule the KMS key for deletion with a 7-day window (by key ID, not alias), and delete the instance role and profile.
- **ECS** ([`ecs/`](ecs#cleanup)): stop the tasks, delete the capacity provider (run it again if it shows `DELETE_FAILED` while tasks are still stopping), delete the cluster, schedule the KMS key for deletion, then `terraform destroy`.
- **EKS** ([`eks/`](eks#cleanup)): `kubectl delete` the pods and the NodePool, remove the Pod Identity association and role, schedule the KMS key for deletion, then `terraform destroy`.
