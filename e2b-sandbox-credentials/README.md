# E2B sandbox credential injection: Claude Code with Anthropic and GitHub

Companion code for the my2cents.ai deep dive on E2B credential injection.

Claude Code runs inside an E2B sandbox that never holds a real credential. The Anthropic API key and
a GitHub token live in E2B Secrets. E2B's egress proxy adds them as HTTP headers on the way out,
only for `api.anthropic.com`, `github.com` and `api.github.com`. Everything else is denied.

Video walkthrough: https://youtu.be/jUcVsgsBYNg

```text
sandbox (placeholders only)  ──HTTPS──▶  E2B egress proxy  ──▶  api.anthropic.com   x-api-key: <real key>
                                         (TLS intercept,        github.com          Authorization: Basic <real>
                                          header rules)         api.github.com      Authorization: Bearer <real>
```

## Requirements

- Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or pip)
- An E2B account and API key ([dashboard](https://e2b.dev/dashboard)). Per-host request transforms
  are a public beta; Secrets worked on my account without an access request.
- An Anthropic API key
- A throwaway GitHub repo and a fine-grained personal access token scoped to that repo only, with
  **Contents: read and write** and **Pull requests: read and write**

## Setup

```bash
uv venv && uv pip install -e .
cp .env.example .env   # fill in E2B_API_KEY, ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_REPO
python template/build.py   # Claude Code + git + gh on the E2B base image, about 40 s
python 01_setup_secrets.py
```

`01_setup_secrets.py` is the only script that reads the real Anthropic key and GitHub token, and it
sends them to E2B's secret store, not into a sandbox. It creates three secrets:

| Secret | Value | Used as |
|---|---|---|
| `anthropic-api-key` | the API key | `x-api-key` on `api.anthropic.com` |
| `github-token` | the token | `Authorization: Bearer …` on `api.github.com` (gh, REST) |
| `github-basic` | `base64("x-access-token:<token>")` | `Authorization: Basic …` on `github.com` (git over HTTPS) |

git only speaks Basic auth, and the proxy cannot base64-encode, so the encoded pair is stored as its
own secret.

## Scripts

| Script | What it shows |
|---|---|
| `02_header_mechanics.py` | Injection rules with a throwaway secret and an echo service: overwrite, body and query untouched, other hosts untouched, plain HTTP, live rotation, delete fails open. No real credentials. |
| `03_claude_code.py` | Claude Code with a placeholder key: 401 without the rule, works with it, streaming included. |
| `04_github.py` | `gh api` and `git clone`/`git push` to a private repo with no token in the sandbox. |
| `05_agent_task.py` | Claude Code clones the repo, commits, pushes a branch and opens a PR. |
| `06_leak_attempts.py` | Env, files and `/proc` as root, curl verbose, echo hosts, body reflection via GitHub's markdown API, exfiltration, asking Claude Code. Ends with what injection does not stop. |
| `07_rotation.py` | Revoke, rotate and delete the GitHub secret under a running sandbox. |
| `08_cleanup.py` | Kills demo sandboxes, deletes the secrets, closes demo PRs and deletes `e2b-demo-*` branches. |

Run them in order: `python 02_header_mechanics.py`, and so on.

## Network policy used by every sandbox

```python
{
    "allow_out": lambda ctx: list(ctx.rules.keys()),   # only the hosts we inject for
    "deny_out": lambda ctx: [ctx.all_traffic],         # everything else blocked
    "rules": {
        "api.anthropic.com": [{"transform": {"headers": {"x-api-key": Secret.fill("anthropic-api-key")}}}],
        "github.com":        [{"transform": {"headers": {"Authorization": f"Basic {Secret.fill('github-basic')}"}}}],
        "api.github.com":    [{"transform": {"headers": {"Authorization": f"Bearer {Secret.fill('github-token')}"}}}],
    },
}
```

The sandbox gets placeholder `ANTHROPIC_API_KEY` and `GH_TOKEN` values so Claude Code and gh start;
the proxy overwrites the headers they produce.

## Caveats

- Injection stops the agent from *reading* the credential, not from *using* it. Scope the GitHub token
  to one repo and keep the Anthropic key on a spend limit.
- Never add a rule for a host that echoes request headers back (httpbin, postman-echo): the sandbox
  would read the injected value in the response.
- Deleting a secret fails open: the request is still sent, just without the header.

## Cleanup

```bash
python 08_cleanup.py
```

Then revoke the GitHub token if you no longer need it.
