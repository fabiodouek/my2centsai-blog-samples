# Daytona sandbox secrets: Claude Code with Bedrock, Anthropic and GitHub

Companion code for the my2cents.ai deep dive on Daytona sandbox credentials.

Video walkthrough: https://youtu.be/Ra2D7_8XwME

Claude Code runs inside a Daytona sandbox that never holds a real credential. A Bedrock API key, an
Anthropic API key and a GitHub token live in Daytona Secrets. Each sandbox env var holds a
`dtn_secret_...` placeholder, and Daytona's egress proxy swaps the placeholder for the real value in
HTTPS request headers, only for the hosts each secret is bound to.

Credentials are **per user**. Daytona's secret store is organization-wide, so each user's secrets are
named `<user>-<credential>` (`alice-github-token`, `bob-github-token`, ...) and a sandbox only gets the
secrets of the user it runs for. The tests run as `DEMO_USER` (alice, with a personal access token);
`08_github_app_user.py` adds bob, whose GitHub credential is a GitHub App user token from the OAuth
device flow.

```text
sandbox (placeholders only)  ──HTTPS──▶  Daytona egress proxy  ──▶  bedrock-runtime.us-east-1...  Authorization: Bearer <real>
  AWS_BEARER_TOKEN_BEDROCK=dtn_secret_…   (TLS intercept, swaps        api.anthropic.com            x-api-key: <real>
  GH_TOKEN=dtn_secret_…                   placeholders in headers,     api.github.com               Authorization: Bearer <real>
  GH_BASIC=dtn_secret_…                   scrubs them from replies)    github.com                   Authorization: Basic <real>
```

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or pip)
- A Daytona account and API key ([dashboard](https://app.daytona.io/dashboard/keys)). Secrets work on
  Tier 1. Per-sandbox network allow lists need Tier 3, so these scripts don't set one.
- An AWS profile that can call Bedrock (`bedrock:InvokeModel*`, `bedrock:CallWithBearerToken`,
  `bedrock:ListInferenceProfiles`, `bedrock:GetInferenceProfile`) and
  `sts:GetSessionToken`, plus S3 create/delete on one throwaway bucket for `09_s3_sigv4.py`
- An Anthropic API key (for the direct-API comparison in `03_claude_code.py` and the leak test)
- A throwaway GitHub repo and a fine-grained personal access token scoped to that repo only, with
  **Contents: read and write** and **Pull requests: read and write**
- For `08_github_app_user.py`: a GitHub App with Device Flow enabled, installed on that repo (below)

## Setup

```bash
uv venv && uv pip install -e .
cp .env.example .env       # fill in the keys, GITHUB_REPO and AWS_TEST_BUCKET
python snapshot/build.py   # Daytona's default image + Claude Code 2.1.312 + gh 2.103.3, a few minutes
python 01_setup_secrets.py
```

`01_setup_secrets.py` is the only script that reads the real credentials, and it sends them to
Daytona's secret store, not into a sandbox. It creates four secrets for `DEMO_USER` (default `alice`):

| Secret | Value | Bound to | Sandbox env var |
|---|---|---|---|
| `alice-bedrock-api-key` | a short-term Bedrock API key minted from your AWS profile (12 h) | `bedrock-runtime.us-east-1.amazonaws.com`, `bedrock.us-east-1.amazonaws.com` | `AWS_BEARER_TOKEN_BEDROCK` |
| `alice-anthropic-api-key` | the API key | `api.anthropic.com` | `ANTHROPIC_API_KEY` |
| `alice-github-token` | the token | `api.github.com` | `GH_TOKEN` |
| `alice-github-basic` | `base64("x-access-token:<token>")` | `github.com` | `GH_BASIC` |

git only speaks Basic auth and the proxy only swaps text, so the encoded pair is its own secret and
git sends it with `http.https://github.com/.extraHeader "Authorization: Basic $GH_BASIC"`. Daytona
sets `GIT_SSL_CAINFO` (and `SSL_CERT_FILE`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, ...) to its
proxy CA, so no TLS config is needed. Re-run `01_setup_secrets.py` to rotate, or when the Bedrock key
expires.

`create_sandbox(test, secrets=..., user=...)` in `common.py` does the mapping: the `secrets` map names
credentials (`{"GH_TOKEN": "github-token"}`), and the user picks whose secret each one resolves to.
Each sandbox is also labelled `userId=<user>`.

### GitHub App for the second user (Test 7)

GitHub App user tokens are OAuth tokens (`ghu_...`) that act for one user, limited to what both the
user and the app can reach, and expire after 8 hours with a refresh token. Daytona has no OAuth
integration of its own, so the token is just another secret value: your service runs the OAuth flow,
stores the user's token as that user's secret, and refreshes it.

**App owner, once** (github.com, signed in):

1. Create the app with the permissions pre-filled (private, no webhook, Contents and Pull requests
   write, Metadata read):
   `https://github.com/settings/apps/new?name=<app-name>&url=<homepage>&public=false&webhook_active=false&contents=write&pull_requests=write&metadata=read`
2. On the app's **General** settings page, tick **Enable Device Flow** and save. Keep **Expire user
   authorization tokens** on (the default). Copy the **Client ID** into `.env` as
   `GITHUB_APP_CLIENT_ID`. No client secret or private key is needed for the device flow.
3. **Install App** on your account with **Only select repositories** and pick `GITHUB_REPO`.

**Each end user, when they first use it** (`python 08_github_app_user.py` prints the code):

1. Open `https://github.com/login/device` and enter the 8-character code.
2. Review the app's permissions and click **Authorize**.

The script stores the user and refresh tokens in `.github-app-tokens.json` (mode 600, gitignored); in
production that's your backend's job. The refresh token never goes into a sandbox.

## Scripts

| Script | What it shows |
|---|---|
| `02_substitution_mechanics.py` | Throwaway secrets against httpbin and postman-echo: header swap, response scrubbing, body/URL/plain HTTP, host binding, unrestricted secrets, rotation and delete timing. No real credentials. |
| `03_claude_code.py` | Claude Code with a placeholder key on the Anthropic API and on Bedrock, plus what happens with no secret attached. |
| `04_github.py` | `gh api` and `git clone`/`git push` to a private repo with no token in the sandbox. |
| `05_agent_task.py` | Claude Code on Bedrock clones the repo, commits, pushes a branch and opens a PR. |
| `06_leak_attempts.py` | Env, files and `/proc` as root (hash-compared against the real values), curl verbose, echo hosts, body reflection via GitHub's markdown API, asking Claude Code. Ends with what secrets do not stop. |
| `07_rotation.py` | Revoke, restore, detach and delete the GitHub secret under a running sandbox, timed. |
| `08_github_app_user.py` | A second user signs in to a GitHub App with the device flow; their `ghu_` token becomes their own secrets. One sandbox per user, each with only its own credential; one user's placeholder in the other's sandbox; a push and PR with the app token; refreshing the token and updating the secret under the running sandbox. |
| `09_s3_sigv4.py` | Why S3 (SigV4) can't use a placeholder secret key, and presigned URLs as the alternative. |
| `10_cleanup.py` | Deletes demo sandboxes, every user's secrets, the snapshot, the S3 bucket and the saved GitHub App tokens; closes demo PRs and deletes `daytona-demo-*` branches. |

Run them in order: `python 02_substitution_mechanics.py`, and so on.

Every sandbox gets the labels `demo=my2cents-credentials` and `userId=<user>`, `ttl_minutes=60` as a safety net, and
`auto_stop_interval=0`, because auto-stop ignores running processes and an agent task can outlast it.

## Caveats

- Daytona has no per-user secret permissions: any member or API key with `write:sandboxes` can attach
  any secret in the organization. The `<user>-` naming keeps sandboxes apart, but enforcing who may
  create sandboxes with whose secrets is your service's job.
- Secrets stop the agent from *reading* the credential, not from *using* it. Scope the GitHub token
  to one repo and keep the Bedrock and Anthropic keys on tight IAM and spend limits.
- A secret with no `hosts` is sent to any host. Always bind secrets to hosts.
- Value updates and deletes take about 30 seconds to reach a running sandbox; a deleted secret keeps
  working until then. Detaching with `sandbox.update_secrets()` takes effect immediately. Deleting a
  Daytona secret does not revoke it at GitHub or AWS.
- Only headers are swapped. Body auth, SigV4-signed AWS APIs (S3, STS, ...) and non-HTTPS protocols
  can't use a placeholder.
- `claude -p` run through `process.exec` waits on stdin; close it with `< /dev/null`.

## Cleanup

```bash
python 10_cleanup.py
```

Then revoke the GitHub token if you no longer need it, and the GitHub App's authorization at
`https://github.com/settings/apps/authorizations` (or delete the app). The short-term Bedrock key expires on its own.

## License

Apache-2.0
