# LangSmith sandbox auth proxy: Claude Code with Anthropic, GitHub and AWS

Companion code for the my2cents.ai deep dive on LangSmith sandbox credentials.

Video walkthrough: https://youtu.be/9JBE5jVkDnQ

Claude Code runs inside a LangSmith sandbox that never holds a real credential. The Anthropic key
and a GitHub token are LangSmith workspace secrets. A transparent proxy on the sandbox host adds them
as HTTP headers for the hosts (and paths) each rule matches. For AWS, the proxy re-signs SigV4
requests with an IAM role that trusts LangSmith's OIDC issuer, so not even temporary AWS keys enter
the VM.

```text
sandbox (placeholders only)  ──HTTPS──▶  LangSmith auth proxy  ──▶  api.anthropic.com   x-api-key: {ANTHROPIC_API_KEY}
  ANTHROPIC_API_KEY=sk-ant-placeholder   (transparent, TLS         api.github.com      Authorization: {GITHUB_BEARER}
  GH_TOKEN=proxy-injected                 intercept, header         github.com          Authorization: {GITHUB_BASIC}
  AWS_ACCESS_KEY_ID=dummy                 rules, SigV4 re-sign)     *.amazonaws.com     SigV4 as the IAM role
```

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or pip)
- A LangSmith account with Sandboxes and an API key
- An Anthropic API key
- A throwaway GitHub repo and a fine-grained personal access token scoped to that repo only, with
  **Contents: read and write** and **Pull requests: read and write**
- For `08_aws_role.py`: an AWS account where you can create an IAM OIDC provider, a role and a
  bucket. On organizations with restricted egress (non-Enterprise), AWS hosts are blocked until
  LangChain support grants an exemption.

## Setup

```bash
uv venv && uv pip install -e .
cp .env.example .env       # fill in the keys and GITHUB_REPO
python snapshot/build.py   # Node 22 + Claude Code 2.1.312 + gh 2.103.3, about 5 minutes
python 01_setup_secrets.py
```

`01_setup_secrets.py` is the only script that reads the real Anthropic key and GitHub token, and it
stores them as workspace secrets:

| Secret | Value | Rule |
|---|---|---|
| `ANTHROPIC_API_KEY` | the API key | `x-api-key` on `api.anthropic.com` |
| `GITHUB_BEARER` | `Bearer <token>` | `Authorization` on `api.github.com` (gh, REST) |
| `GITHUB_BASIC` | `Basic base64("x-access-token:<token>")` | `Authorization` on `github.com` (git over HTTPS) |

A header value can also mix text and a reference, such as `"Bearer {MY_TOKEN}"`.

`snapshot/build.py` gives the build its own client with a 10-minute HTTP timeout. The SDK default is
10 seconds, and `create_snapshot_from_dockerfile(timeout=...)` doesn't extend it for the final
capture, which fails the build. Dockerfile builds are tagged with a build hash rather than `latest`,
so `common.py` looks the snapshot up by name and passes its ID.

### AWS role (for `08_aws_role.py`)

```bash
ACCT=123456789012
WS=<your LangSmith workspace ID>
aws iam create-open-id-connect-provider --url https://api.smith.langchain.com --client-id-list sts.amazonaws.com
cat > trust.json <<EOF
{"Version":"2012-10-17","Statement":[{"Effect":"Allow",
 "Principal":{"Federated":"arn:aws:iam::${ACCT}:oidc-provider/api.smith.langchain.com"},
 "Action":"sts:AssumeRoleWithWebIdentity",
 "Condition":{"StringEquals":{"api.smith.langchain.com:aud":"sts.amazonaws.com",
                              "api.smith.langchain.com:sub":"ws:${WS}:sandbox"}}}]}
EOF
aws iam create-role --role-name my2cents-langsmith-sandbox --assume-role-policy-document file://trust.json
```

Then attach a permissions policy with only what the sandbox needs (Bedrock invoke, and S3 on one
prefix). The `sub` trusts every sandbox in the workspace. Use the issuer for your region
(`/api/v1/info` → `sandbox_workload_identity_issuer`).

## Scripts

| Script | What it shows |
|---|---|
| `02_injection_mechanics.py` | Against GitHub: `Bearer {SECRET}` templating, a sandbox-sent header overwritten, `match_paths`, opaque vs workspace secrets, plain HTTP, what the API stores (including the managed egress allow list). |
| `03_claude_code.py` | Claude Code with a placeholder key: invalid without the rule, works with it, streaming included. |
| `04_github.py` | `gh api` and `git clone`/`git push` to a private repo with no token in the sandbox. |
| `05_agent_task.py` | Claude Code clones the repo, commits, pushes a branch and opens a PR. |
| `06_leak_attempts.py` | Env, files and `/proc` (hash-compared against the real values), curl verbose, echo hosts, metadata endpoint, body reflection via GitHub's markdown API, asking Claude Code. Ends with what injection does not stop. |
| `07_rotation.py` | Change, re-apply, disable and delete the GitHub secret under a running sandbox. |
| `08_aws_role.py` | AWS via the OIDC role: identity, S3 inside and outside the allowed prefix, Claude Code on Bedrock. Stops early if AWS is blocked by egress. |
| `09_cleanup.py` | Deletes demo sandboxes, the snapshot and the workspace secrets; closes demo PRs and deletes `langsmith-demo-*` branches. |

Run them in order: `python 02_injection_mechanics.py`, and so on.

Every sandbox is named `my2cents-lscreds-*` and created with a 10-minute idle stop and deletion 10
minutes after that, so anything a crashed script leaves behind goes away on its own.

## Caveats

- Injection stops the agent from *reading* the credential, not from *using* it. Scope the GitHub token
  to one repo and keep the Anthropic key on a spend limit.
- Workspace secrets are resolved when a proxy config is applied. Changing or deleting a secret does
  nothing to running sandboxes until you call `update_sandbox(proxy_config=...)`, and re-applying
  after a delete is rejected. To cut a sandbox off, disable the rule or delete the sandbox, and revoke
  the token at GitHub.
- A disabled rule fails open: the request still goes out, without the header.
- `env_vars` are stored and returned by the API in plaintext. Never put a real value there.
- Sandboxes run as root. Claude Code needs `IS_SANDBOX=1` to accept `--dangerously-skip-permissions`.
- On non-Enterprise organizations, egress is limited to a managed allow list, and an `access_control`
  you pass at create time is silently dropped.

## Cleanup

```bash
python 09_cleanup.py
# AWS, if you created it:
aws iam delete-role-policy --role-name my2cents-langsmith-sandbox --policy-name <policy>
aws iam delete-role --role-name my2cents-langsmith-sandbox
aws iam delete-open-id-connect-provider --open-id-connect-provider-arn arn:aws:iam::<acct>:oidc-provider/api.smith.langchain.com
aws s3 rb s3://<bucket> --force
```

Then revoke the GitHub token if you no longer need it.

## License

Apache-2.0
