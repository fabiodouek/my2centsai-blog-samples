# Claude Code in a Google AX task, with no credentials inside

Companion code for the my2cents.ai deep dive on credential injection with Google AX and Agent
Substrate on Amazon EKS.

Claude Code runs in an AX task that never holds a real credential. The Anthropic API key and a
GitHub token live in Kubernetes Secrets. Substrate's egress gateway terminates the task's TLS for
the hosts its egress policy lists and swaps placeholder headers for the real values on the way out.
Every other host doesn't even resolve.

Video walkthrough: https://youtu.be/wf7HAWN3jeM

```text
AX task (placeholders only)  ──HTTPS──▶  Substrate egress gateway  ──▶  api.anthropic.com  x-api-key: <real key>
                                          (TLS intercept, EgressPolicy    api.github.com     Authorization: Bearer <real>
                                           replaceHeaders, k8s Secrets)   github.com         Authorization: Basic <real>
```

AX doesn't wire any of this up yet ([google/ax#475](https://github.com/google/ax/issues/475)), so
the scripts use Substrate directly: `kubectl-ate` for per-task egress policies, the bundled
Kubernetes Secrets credential provider, and a boot wrapper that makes the task trust the gateway's CA.

## Requirements

- The `ax-eks` cluster from [`../google-ax-eks`](../google-ax-eks), installed with
  `--credential-provider='{"name":"k8s.io"}'` (its `03_substrate.sh` does), and its Terraform
  including the node-to-node security group rule
- `kubectl-ate` built from the pinned Substrate checkout:
  `cd ../google-ax-eks/.src/substrate && go build -o ../bin/kubectl-ate ./cmd/kubectl-ate`
- Python 3.11+ (standard library), Docker with `buildx`, `kubectl`, the AWS CLI, `zstd`, `openssl`
- An Anthropic API key, and a throwaway GitHub repo with a fine-grained token scoped to it
  (**Contents** and **Pull requests**: read and write)

```bash
cp .env.example .env   # ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_REPO
```

## Scripts

| Script | What it shows |
|---|---|
| `01_setup.py` | Builds the task image (`image/`), stores the real credentials as Secrets, grants the `creds` atespace access to them, saves the gateway CA. The only script that reads the real values |
| `02_mechanics.py` | With a throwaway canary secret and an echo service: no policy means no DNS, how the header is swapped, when it isn't, what an unlisted host or plain HTTP gets, and a task that doesn't trust the gateway |
| `03_claude_code.py` | Claude Code with a placeholder key: invalid without injection, working with it, streaming included |
| `04_github.py` | `gh` and `git clone`/`push` against a private repo with no token in the task |
| `05_agent_task.py` | Claude Code clones the repo, commits, pushes a branch and opens a pull request |
| `06_leak_attempts.py` | Environment, files and `/proc` as root, `curl -v`, exfiltration, asking Claude Code, the task spec and the snapshot in S3 |
| `07_rotation.py` | Revoke, restore and delete the GitHub token under a running task |
| `08_cleanup.py` | Deletes the tasks, closes the demo pull requests, deletes `ax-*` branches, removes the Secrets and the grant |

Run them in order: `python3 01_setup.py`, and so on.

## Security notes

- The task holds placeholders, but it can still *use* the credentials: anything it sends to an
  allowed host goes out with the real value attached. Allow only the hosts the agent needs.
- A host that echoes request headers back (like the echo service in `02_mechanics.py`) returns the
  real value to the sandbox. Never list one with a real credential.
- `kubectl-ate` acts as the shared `ate-client` identity, a Substrate bootstrap owner. Fine for an
  evaluation cluster; give operators their own identities for anything else.
