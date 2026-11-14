# Managed Agents in the Gemini API: sandbox, egress and credential tests

Companion code for [Managed Agents in the Gemini API: Hands-On with the Sandbox, Its Egress Proxy and Hooks](https://www.my2cents.ai/deep-dive/gcp-managed-agents/), part 2 of the Google Cloud Sandboxes series.

Managed Agents has no exec API: every command runs through Google's Antigravity agent. These scripts send a small probe script into the agent's sandbox as an inline file, ask the agent to run it, and print only what the sandbox printed. That keeps each test repeatable and makes the network and credential settings explicit, which the AI Studio Playground doesn't expose.

## What you need

- A Gemini API key for a project with Gemini API billing (Prepay credits) or on the free tier. Create one in [AI Studio > API Keys](https://aistudio.google.com/api-keys).
- `curl` and `jq`.

```bash
export GEMINI_API_KEY=...   # never commit it
```

## The tests

| Script | What it checks |
|---|---|
| `t1-fingerprint.sh` | What the sandbox is: kernel (gVisor), OS image, user, CPUs, memory, egress proxy |
| `t2-egress.sh` | Egress control: default network, an allowlist of one host, network disabled |
| `t3-credential.sh` | Credential injection: placeholder inside the sandbox, refusals for the wrong domain or location, and a trusted domain that echoes the secret back |
| `t4-hooks.sh` | Hooks: the docs' example matcher (`code_execution`), the real tool name (`execute_code`), and a gate that crashes (fails open) |
| `t5-idle.sh` | What survives an idle restore (waits 35 minutes) |
| `t6-teardown.sh` | Deletes every environment for this API key |

On 2026-11-12 `t4-hooks.sh` let the command through in case 1 (the docs' `code_execution` matcher); on 2026-11-14 the same script blocked it, so expect the current behavior to differ from the first run described in the post.

Run them from this directory, for example `bash t2-egress.sh`. `lib.sh` holds the shared `ask` and `say` helpers; the `probe*.sh` files are what runs inside the sandbox.

## Cost and cleanup

During the preview, sandbox compute isn't billed; you pay for model tokens. A one-command test used about 10,000 tokens (about one cent with Gemini 3.8 Flash). Environments are kept for 7 days after their last use, so run `t6-teardown.sh` when you're done. `t3-credential.sh` deletes the credential it creates.
