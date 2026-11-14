# Cloud Run sandboxes

Companion code for [Cloud Run Sandboxes: Hands-On with the Boundary, Secrets, 1,000 Sandboxes and Noisy Neighbors](https://www.my2cents.ai/deep-dive/gcp-cloud-run-sandboxes/), part 4 of 6 in the Google Cloud Sandboxes series.

Cloud Run sandboxes (Preview) let a Cloud Run container start child sandboxes with the `sandbox` CLI that Cloud Run mounts at `/usr/local/gcp/bin/sandbox`. These scripts deploy one small Cloud Run service and run the tests from the post inside it.

## What is in here

| File | What it does |
|---|---|
| `host/Dockerfile`, `host/server.py` | The host image: Python plus the tools a sandbox may use, a root-only `0600` file, and a **private command endpoint** that runs a shell script and streams its output. |
| `deploy.sh` | Builds the image with Cloud Build and deploys the service (2 vCPU / 4 GiB, second generation, min = max = 1 instance, CPU always allocated, IAM-only). Sandboxes are not on yet. |
| `lib.sh`, `run.sh` | `./run.sh <script> [timeout]` sends a script to the instance through the endpoint and streams the output back. |
| `t1_enable.sh` | Test 1: the GA command track rejects `--sandbox-launcher`; `gcloud beta` enables it; the YAML before and after. |
| `t2_boundary.sh` | Test 2: kernel, `dmesg`, hypervisor flag, seccomp and mounts on the host and inside a sandbox. |
| `t3_visibility.sh` | Test 3: environment variables, a Secret Manager secret (as env var and as a mounted file), a `0600` root file, the user, and the metadata server. |
| `t4_network.sh` | Test 4: default deny, `--allow-egress`, DNS versus connect errors. |
| `t4b_vpc.sh` | Test 4 (optional): does `--allow-egress` follow Direct VPC egress? Needs the second service `sbx-internal` (`gcloud run deploy sbx-internal --image us-docker.pkg.dev/cloudrun/container/hello --ingress internal --no-allow-unauthenticated`) and the service updated with `--network default --subnet default --vpc-egress all-traffic`. |
| `t5_state.sh` | Test 5: writes, `--write`, tar export and import, `fork`, `--rootfs`. |
| `t6_logging.sh`, `t6_search.sh` | Test 6: a canary passed with `--env`, then searched in Cloud Logging. |
| `t7_latency.sh` | Test 7: 1,000 sequential sandboxes, concurrency 10, 50 and 100, and 100 `exec` calls into a warm sandbox. |
| `t8_noisy.sh`, `t8_hogs.sh`, `t8_unhog.sh` | Test 8a: host request latency at idle and with 4 and 8 busy sandboxes. |
| `t8_memory.sh` | Test 8b: a sandbox that allocates memory until it dies. |
| `t8_parent.sh` | Test 8c: what happens to a running sandbox when a new revision replaces the instance. |
| `adk/`, `t9_adk_deploy.sh`, `t9_adk_ask.py`, `t9_orphans.sh` | Test 9: a minimal ADK agent whose code runs through `CloudRunSandboxCodeExecutor`, a script that asks it for a runaway loop, and a check for what is still running afterwards. |
| `ssh_run.py` | Runs commands in a Cloud Run SSH session (`gcloud beta run services ssh` has no `--command` and needs a terminal). |
| `teardown.sh` | Deletes everything the tests created. |

## Prerequisites

- `gcloud` with the `beta` component, logged in to a project with billing on. `export CLOUDSDK_CORE_PROJECT=<your project>`.
- APIs: `run`, `artifactregistry`, `cloudbuild`, `secretmanager`, `logging`.
- Roles for you: `roles/run.developer`, `roles/iam.serviceAccountUser`, plus enough to use Cloud Build, Artifact Registry and Secret Manager.
- `curl`. For test 9: `pip install google-adk` and a Gemini API key stored as the Secret Manager secret `m2c-adk-key`.

## Run it

```bash
./deploy.sh                      # build + deploy (no sandboxes yet)
./t1_enable.sh                   # turn sandboxes on with gcloud beta
./run.sh t2_boundary.sh
./run.sh t3_visibility.sh
./run.sh t4_network.sh
./t4b_vpc.sh                     # optional, see its row above
./run.sh t5_state.sh
./run.sh t6_logging.sh && sleep 45 && ./t6_search.sh
N=1000 CONC_N=300 ./run.sh t7_latency.sh 1700     # about 10 minutes
./t8_noisy.sh
./run.sh t8_memory.sh 300
./t8_parent.sh
./t9_adk_deploy.sh               # test 9 (needs google-adk and the secret m2c-adk-key)
python3 t9_adk_ask.py demo1 "Run exactly this code and tell me the output: while True: pass" 700
python3 ssh_run.py adk-sbx '' --script t9_orphans.sh
./teardown.sh
```

The scripts that start with `./run.sh` run **on the instance**; the others run on your laptop.

## Warnings

- `host/server.py` is a **remote-code-execution endpoint** by design. `deploy.sh` makes the service IAM-only (`--no-allow-unauthenticated`). Never deploy it with public access, and delete it when you are done.
- Cloud Run sandboxes are in Preview. The service bills for instance time while it exists (2 vCPU / 4 GiB with CPU always allocated is about $0.16 an hour at list price in `us-central1`; the monthly free tier covers about 33 hours).
- The tests use dummy canary values (`m2c-secret-canary-4417`, `m2c-root-only-canary`, `m2c-canary-7731`). Do not put real secrets in `--env`: the command line is written to Cloud Logging.
- Inside a sandbox there is no `PATH` and no environment, so the scripts call tools by full path.
