# AWS Lambda MicroVMs Lifecycle

Eleven Python scripts that walk [AWS Lambda MicroVMs](https://docs.aws.amazon.com/lambda/latest/dg/lambda-microvms-guide.html) through the whole sandbox lifecycle:

- building an image
- running MicroVMs and inspecting their defaults
- the endpoint, tokens, files, streaming, WebSockets and the shell
- suspend and resume
- idle auto-suspend and auto-resume
- the maximum duration
- listing and reconnecting
- many MicroVMs from one snapshot, and a custom image
- the egress and identity baseline
- quotas and the bill
- cleanup

Each script prints timings, so you can reproduce the numbers in the post.

Companion code for [AWS Lambda MicroVMs: Hands-On with the Full Sandbox Lifecycle](https://my2cents.ai/deep-dive/lambda-microvms), part 1 of the AWS Lambda MicroVMs series.

Video walkthrough: https://youtu.be/nXKKxiv0SN0

## Prerequisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or plain `pip`)
- Terraform 1.6+
- AWS credentials allowed to manage Lambda MicroVMs, IAM roles, S3 and CloudWatch Logs, and to read Service Quotas and Cost Explorer (for `10_quota_bill.py`), in a Region with MicroVMs (the scripts default to `us-east-1`).
- AWS CLI 2.35.10+ or boto3 1.43.35+ (the first versions with `lambda-microvms`). This repo pins boto3 1.43.123.
- Check your quotas first. Service Quotas lists a default of 2,048 GB of MicroVM memory and 5 `RunMicrovm` calls per second (the Lambda guide says 1,024 GB in us-east-1), but quotas are per account and new accounts can start much lower. AWS says it raises them automatically with usage. The account used for the post had **8 GB and 1 call per second**, and raising memory by hand took a support case. Every script here fits in 8 GB. `10_quota_bill.py` prints your applied values next to the defaults.

## Setup

```bash
cp .env.example .env
uv venv && uv pip install -e .
source .venv/bin/activate
terraform -chdir=infra init && terraform -chdir=infra apply
```

`infra/` creates three things:

- An S3 bucket for the build zips.
- A build role, which reads the zip and writes build logs.
- Two execution roles:
  - `exec-logs`, with runtime logs only, used by most tests.
  - `exec-probe`, with logs plus `s3:ListAllMyBuckets`, used by `09_egress_identity.py`.

There's no VPC: part 1 uses the managed `INTERNET_EGRESS` connector.

Lambda MicroVMs have no exec or file API, only an HTTPS endpoint that forwards to ports inside the VM, so `images/sandbox/agent.py` is a small agent:

| Port | What |
|---|---|
| 8080 | `POST /exec`, `POST /exec/stream` (SSE), `PUT`/`GET /files`, `GET /state`, `GET /events`, `POST /tcp/open`, `GET /tcp/check` |
| 8081 | WebSocket echo |
| 9001 | The six lifecycle hooks (`/aws/lambda-microvms/runtime/v1/{ready,validate,run,resume,suspend,terminate}`). If the image sets a `VALIDATE_CMD` environment variable, `/validate` runs it, so Lambda prefetches whatever it touches |

Anyone holding an endpoint token for port 8080 can run any command as root in that MicroVM. That's what a sandbox is for, but treat tokens like credentials.

## Scripts

| Script | What it shows |
|---|---|
| `01_image.py` | Managed base images, building `images/sandbox` (states, time, per-chipset snapshot builds and their sizes), build hook logs, a second version, pinning and deactivating a version, whether x86_64 is accepted |
| `02_run_defaults.py [runs]` | What `run-microvm` fills in when you pass only the image, N sequential runs with time to `RUNNING` and to the first response, what the guest reports, the 4x burst over the baseline |
| `03_endpoint.py [--slow]` | Tokens and port scopes, whether a token holder can call the hooks, exit codes, a 100 s request, SSE streaming, upload/download bandwidth, HTTP/2, WebSockets, the shell over `SHELL_INGRESS`, `NO_INGRESS`. `--slow` adds a token-expiry check |
| `04_suspend_resume.py` | Three suspend/resume cycles: latency, what survives (files, a background process, in-memory state, an open TCP connection), the guest clock, hooks, wrong-state calls, a request to a suspended MicroVM with auto-resume off |
| `05_idle.py` | Idle suspend at 60 s, auto-resume latency, whether CPU work, outbound-only traffic or a held-open stream count as activity, `suspendedDurationSeconds` (~15 min) |
| `06_duration.py` | `maximumDurationInSeconds` with and without a suspend in the middle, explicit terminate (running and suspended), which `/terminate` hooks show up in CloudWatch (~10 min) |
| `07_list_reconnect.py` | `list-microvms` fields and filters, `run-microvm` having no tags parameter, the run hook payload limit, `clientToken` idempotency, a second process attaching by ID |
| `08_many_from_one.py` | Three MicroVMs from one image: what they share from the snapshot (process memory, Python's `random`, build-time UUIDs) and what's unique; then a data-science image vs `pip install` at runtime, and the same image with a `/validate` warm-up |
| `09_egress_identity.py` | Egress IPs, raw-IP traffic, DNS over UDP and TCP, and the execution role through IMDSv2 (root, non-root, a blackhole route) with and without a role |
| `10_quota_bill.py` | Applied vs default quotas, filling the memory quota, whether suspended MicroVMs count, `RunMicrovm` throttling, Cost Explorer |
| `11_cleanup.py` | Terminate every MicroVM from this repo's images and delete the images |

Run them in order. `01` builds the image the others use:

```bash
python 01_image.py
python 02_run_defaults.py
python 03_endpoint.py
python 04_suspend_resume.py
python 05_idle.py            # ~15 minutes, mostly waiting for idle suspends
python 06_duration.py        # ~10 minutes
python 07_list_reconnect.py
python 08_many_from_one.py   # builds images/datasci the first time
python 09_egress_identity.py
python 10_quota_bill.py
python 11_cleanup.py
terraform -chdir=infra destroy
```

## Safety nets and gotchas

- MicroVMs can't be tagged, so the scripts identify theirs by image name (`my2cents-lc-*`) and put the test name in the run hook payload. `11_cleanup.py` only touches MicroVMs started from `my2cents-lc-*` images.
- `common.run()` sets `maximumDurationInSeconds=3600` and an idle policy that suspends after 10 idle minutes and terminates after 10 suspended minutes. Lambda only applies that second timer after an idle suspend: a MicroVM suspended with `suspend_microvm` stays suspended until its maximum duration, so the hour is the real backstop. Tests that need other values set them explicitly. `02_run_defaults.py` starts one MicroVM with Lambda's defaults (an 8-hour cap, no idle policy) and terminates it in a `finally` block.
- The image identifier must be the full ARN for `get-microvm-image` and `run-microvm`; a bare name returns `Invalid ARN format` (the getting-started guide shows a bare name).
- Output is redacted: the account ID becomes `<acct>` and the bucket name `<bucket>`. Set `REDACT=0` in `.env` to see them.
- Every MicroVM restored from the same chipset's snapshot shares what existed at build time, including Python's `random` state. Generate IDs and seeds in `/run`.
- The lifecycle hooks are reachable through the endpoint by any token that covers their port, and Lambda's own hook calls arrive from `127.0.0.1` like everything else. Scope tokens to the app port.
- Images are billed as snapshot storage until deleted (with a one-week minimum). Run `11_cleanup.py` and `terraform destroy` when you're done.

## License

Apache-2.0
