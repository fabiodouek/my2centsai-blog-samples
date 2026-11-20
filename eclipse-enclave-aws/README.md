# Eclipse Enclave on AWS

Companion code for the my2cents.ai post on Eclipse Enclave 1.0.1 on AWS. Enclave runs an AI coding agent (Claude Code here) in a container behind a gateway that filters its network access. These scripts put it on one small Graviton EC2 instance with no inbound access and test what the fence does and does not stop.

Everything runs in **us-east-1** on one **`t4g.small`** (Graviton2, 2 vCPU, 2 GiB RAM), Ubuntu 24.04 arm64, in the default VPC, with a security group that has **no inbound rules** and no key pair. You reach the instance only through AWS Systems Manager (SSM). Claude Code talks to Claude Haiku 4.5 on Amazon Bedrock with the instance role's credentials.

## Prerequisites

- An AWS account and credentials that can create EC2 instances, security groups and IAM roles, and call Amazon Bedrock. The default VPC must exist.
- Access to **Claude Haiku 4.5** on Bedrock in us-east-1 (the scripts use the `us.anthropic.claude-haiku-4-5-20251001-v1:0` inference profile; set `MODEL` to use another).
- AWS CLI v2 and `jq`, `bash`, on your machine. No Session Manager plugin is needed: the scripts use `aws ssm send-command`.

## Steps

The instance is launched from the **EC2 console** in the post (Launch instance: Ubuntu Server 24.04 LTS, 64-bit Arm, `t4g.small`, proceed without a key pair, the `enclave-eval-no-ingress` group, 30 GiB gp3, the `enclave-eval-profile` instance profile). `02_launch_cli.sh` sends the same request with the AWS CLI.

| Script | What it does |
|---|---|
| `01_prereqs.sh` | The no-inbound security group, and an instance role (SSM + Bedrock invoke) with its instance profile |
| `02_launch_cli.sh` | CLI equivalent of the console launch form. Skip it if you launched from the console, and run `03_wait_ssm.sh` instead |
| `03_wait_ssm.sh` | Finds the instance by its `Name` tag and waits until SSM can reach it |
| `run_remote.sh remote/01_install.sh` | Swap file, Docker and buildx, a non-root `agent` user, the Enclave `enclave-linux-arm64` binary with its checksum checked |
| `run_remote.sh remote/02_first_run.sh agent` | First run: builds the images (about 4.5 minutes here), memory sampled; then a second run |
| `run_remote.sh remote/03_bedrock.sh agent` | Claude Code on Bedrock: denied by default, then `--allow-domain`, then a headless task |
| `run_remote.sh remote/04_egress_probe.sh agent` | Nine network probes from inside a session, then the gateway log |
| `05_imds.sh` | Instance metadata (IMDS) from a plain container, from Enclave, from Enclave with `--allow-all-network`, and from a plain container at hop limit 1 |
| `run_remote.sh remote/06_secrets.sh agent` | A declared secret becomes a placeholder; an AWS key passed with `--pass-env` does not |
| `07_parallel.sh` | Two agents in two git worktrees on 2 GiB of RAM, then a kernel-log check for out-of-memory kills |
| `08_security_group.sh` | No inbound rules, nothing answers on the public address, SSM still works |
| `99_teardown.sh` | Terminates the instance, deletes the group, profile and role, and checks nothing is left |

`run_remote.sh` sends a script from `remote/` through SSM Run Command and prints its output. Enclave wants a terminal and SSM has none, so the scripts wrap Enclave in `script -qec` to give it one.

## Notes

- Enclave refuses to run in a home directory, so the agent works in `~/project`.
- The 2 GiB host needed the 2 GiB swap file only lightly: the first image build peaked at about 820 MB of RAM and under 100 MB of swap.
- The dummy `ANTHROPIC_API_KEY` in `06_secrets.sh` is a fake value, and the script deletes it at the end.
- The Bedrock runs cost cents. The instance costs about $0.017 an hour plus a public IPv4 address at $0.005 an hour and 30 GB of gp3. **Run `99_teardown.sh` when you are done.**
