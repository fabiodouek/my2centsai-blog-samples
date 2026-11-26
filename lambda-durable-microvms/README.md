# Lambda Durable Functions orchestrating Lambda MicroVMs

One Python durable function that launches [Lambda MicroVMs](https://docs.aws.amazon.com/lambda/latest/dg/lambda-microvms-guide.html), hands each one a job, waits for the result without running, and terminates it. It uses the MicroVM extension of the AWS Durable Execution SDK, a **developer preview** [announced on 2026-11-25](https://aws.amazon.com/about-aws/whats-new/2026/11/lambda-durable-microvm/). The scripts cover:

- one job in one MicroVM (task mode)
- four jobs in parallel with `context.map`, and what happens past the MicroVM memory quota
- a 20-minute job from a function with a 2-minute timeout
- starting executions from SQS
- two jobs in one MicroVM with a durable wait between them, with and without suspend (session mode)
- a failing job, a crashed worker, and retrying
- replay after the function crashes
- a missing `lambda:TerminateMicrovm`
- what it all cost

Companion code for [Lambda Durable Functions + MicroVMs: Hands-On with the Developer Preview](https://my2cents.ai/deep-dive/lambda-durable-microvms).

Video walkthrough: https://youtu.be/w9ZFenhMe7k

> **Developer preview.** The extension packages are alpha releases (`aws-durable-execution-sdk-python-extensions` 0.1.0a1, `aws-durable-execution-sdk-python-compute-worker` 0.0.1a1). AWS says not to use them for production, and their APIs can change in any release. Everything here is pinned to the versions I tested on 2026-11-26.

## How it works

```
caller ──invoke (alias)──▶ durable function ──RunMicrovm──▶ MicroVM (worker image)
                              │  checkpoints:                 │ worker runs the job
                              │  callback, launch, terminate   │ heartbeats + result
                              ▼                                ▼
                      invocation ends          SendDurableExecutionCallback{Heartbeat,Success,Failure}
                              ▲                                │
                              └──── Lambda invokes again, replays, TerminateMicrovm
```

- `function/handler.py` is the durable function. Each demo is a few lines around `microvm(...)` or `microvm_session(...)`.
- `image/app.py` runs inside the MicroVM image. `start_microvm_worker` serves the lifecycle hooks and the job routes on port 8080 and completes the durable callback with the job's result.

## Prerequisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or `pip`), Terraform 1.6+
- AWS credentials that can manage Lambda, Lambda MicroVMs, IAM roles, S3, SQS and CloudWatch Logs, in a Region with both Lambda durable functions and Lambda MicroVMs (the scripts default to `us-east-1`)
- MicroVM quotas: the scripts need four 2 GB MicroVMs at once (8 GB of MicroVM memory). New accounts can start with a quota that small; `03_fanout.py` shows what happens one MicroVM past it.

## Setup

```bash
cp .env.example .env
uv venv && uv pip install -e .
source .venv/bin/activate
terraform -chdir=infra init && terraform -chdir=infra apply
python 00_image.py      # build the worker image (about 2 minutes)
python 01_deploy.py     # package and deploy the functions, publish a version, alias "live"
```

`infra/` creates an S3 bucket, the image build role, the MicroVM execution role (it may only complete callbacks of these functions), two function roles (one without `lambda:TerminateMicrovm`, for `09_leak.py`) and an SQS queue with a dead-letter queue.

`01_deploy.py` creates three durable functions from the same code: `my2cents-ldm-pipeline` (2-hour execution timeout), `my2cents-ldm-queue` (30 minutes, the most an SQS event source mapping accepts) and `my2cents-ldm-leaky`. Each function bundles boto3 1.44.12: the extension needs the `lambda-microvms` client (boto3 1.43.35 or later). Executions start through the `live` alias, never `$LATEST`, so a replay always runs the code that launched the MicroVM.

## Scripts

| Script | What it shows |
|---|---|
| `00_image.py` | Builds the worker image with every lifecycle hook on port 8080 |
| `01_deploy.py` | Deploys the three durable functions, publishes a version, points the alias |
| `02_task.py` | One scan job in one MicroVM: the operations, the two invocations, the MicroVM's lifetime |
| `03_fanout.py` | Four scans with `context.map` and the same four one after another; then five against a quota for four, with the default fail-fast map, and again with a quota-aware retry strategy |
| `04_long.py` | A 20-minute job: the function runs for a few seconds in total while the MicroVM works |
| `05_sqs.py` | Connects the queue to the function, sends a scan and a 20-minute job |
| `06_session.py` | Build, a 2-minute durable wait, test, in one MicroVM, with and without `suspend_after` |
| `07_failures.py` | A job that raises, a worker that dies mid-job, and a retry loop |
| `08_replay.py` | The function crashes after the job finished: Lambda runs it again, the replay launches nothing |
| `09_leak.py` | The same scan from a role without `lambda:TerminateMicrovm`: the MicroVM runs to its maximum duration |
| `10_cost.py` | Durable operations, Lambda seconds and MicroVM seconds per run, priced at the us-east-1 rates, plus the snapshot reads of every launch |
| `11_cleanup.py` | Terminates MicroVMs, deletes the mapping, the functions, the image and the log groups |

Each script prints what it measured and writes it to `results/<script>.json`, with the account ID and bucket name redacted.

## Things I learned the hard way

- **Fail-fast maps leave MicroVMs running.** `context.map`'s default completion ends the map at the first failed item. Items still waiting on a MicroVM are never resumed, so nothing terminates their MicroVMs: they run until the operation's timeout plus 5 minutes. The handler also has to look at the `BatchResult`; the execution itself succeeds. Use `CompletionConfig.all_completed()` when every item launches a MicroVM.
- **`ServiceQuotaExceededException` is not retried by default.** One MicroVM past the memory quota fails its launch at once. A terminated MicroVM's memory isn't released the instant it's terminated either, so `max_concurrency` equal to the quota still hit it once. `quota_aware_retry` in `handler.py` retries that error every 5 seconds.
- **A failed job is not retried.** `MicrovmJobFailedError` and `MicrovmTimeoutError` reach your handler. Retrying is a loop in your code, and each attempt is a new MicroVM.
- **SQS needs an execution timeout of 30 minutes or less.** `CreateEventSourceMapping` refuses a durable function with a longer one.
- **For short jobs, the launch is the bill.** Every launch reads the image's snapshot ($0.00155 per GB used). For this image that's $0.0009 to $0.0013, more than ten times the compute of a two-second job; the durable operations are about 3 to 4%.
- **Without `lambda:TerminateMicrovm`, nothing fails.** The job and the execution succeed; one ERROR log line is the only sign. Alarm on `terminate failed for`.

## Cleanup

```bash
python 11_cleanup.py
terraform -chdir=infra destroy
```

MicroVM image storage bills for a one-week minimum even if you delete the image sooner.

## License

Apache-2.0, see [LICENSE](LICENSE).
