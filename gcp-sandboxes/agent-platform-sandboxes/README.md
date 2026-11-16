# Agent Platform sandboxes

Companion code for [Agent Platform Sandboxes: Hands-On with Code Execution, Shell, Computer Use and Custom Containers](https://www.my2cents.ai/deep-dive/gcp-agent-platform-sandboxes/), part 3 of 6 in the Google Cloud Sandboxes series.

Video walkthrough: https://youtu.be/DLlibgYAXEc

One script per test. They create real sandboxes in your project and cost money while the sandboxes exist (list price $0.085 per vCPU-hour and $0.009 per GiB-hour). Every sandbox gets a short `ttl` and is deleted when its script ends, but read `teardown.py` before you start, and run it when you finish.

## Setup

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
export GOOGLE_CLOUD_PROJECT=<your project>      # billing on, APIs: aiplatform, artifactregistry, cloudbuild, iamcredentials
export LOCATION=us-central1
gcloud auth login                               # `sbx.py` borrows your gcloud token instead of ADC
```

Tested with Python 3.12.13, `google-cloud-agentplatform` 2.9.0 (`api_version` v1beta1), `google-genai` 3.3.0 and Google Cloud CLI 593.0.0, on November 14, 2026.

For Computer Use, create a service account with `roles/aiplatform.user`, give yourself `roles/iam.serviceAccountTokenCreator` on it, and `export SA_EMAIL=<its email>`. For the custom containers, create an Artifact Registry Docker repo `sbx-byoc` in us-central1, grant `roles/artifactregistry.reader` to the project's `service-<project number>@gcp-sa-vertex-sandbox.iam.gserviceaccount.com`, and build the three images in `byoc/` (`gcloud builds submit byoc/<dir> --tag us-central1-docker.pkg.dev/$GOOGLE_CLOUD_PROJECT/sbx-byoc/<dir>:1`). `t8_byoc.py` reads the repo name from `BYOC_REPO` (default `sbx-byoc`). If you delete and recreate a repo, use a new name: templates pointing at the recreated one stayed `PROVISIONING` for me.

## Files

| File | What it does |
|---|---|
| `sbx.py` | Helpers: `python -i sbx.py` gives you `client`, `run()` (Code Execution), `sh()` (Shell), `rest()`, `create_from_template()`, `cdp()` |
| `t0_setup.py` | Creates the Agent Runtime instance that parents everything |
| `t1_create.py` | Create times for all types, template create to ACTIVE, template sprawl |
| `t2_fingerprint.py` | Kernel, user, size per type; the size the platform puts in the routing token |
| `t3_egress.py` | Identity, metadata server and egress matrix |
| `t4_ttl.py` | Default TTLs, TTL reset on execute, deletion at expiry |
| `t5_limits.py`, `t5b_upload.py` | Code Execution: JavaScript, pip, libraries, output files; the 100 MB input cap |
| `t6_pause_snapshot.py` | Pause, resume, snapshot, restore: what survives |
| `t7_computer_use.py`, `cu_loop.py` | Raw CDP, the Gemini 3.8 Flash loop (about 100 lines), noVNC, pause and snapshot of a browser |
| `t8_byoc.py`, `byoc/` | Three custom container images and what the platform does with them |
| `t9_audit.py` | Which operations reach Cloud Logging, and whether the code does |
| `teardown.py` | Deletes everything and verifies |
| `results/` | Raw results of the runs in the post (project IDs and numbers redacted) |

Run the tests in order (`t1` creates the templates the others use). Templates hold a warm pool of 10 sandboxes by default, so delete them when you stop.
