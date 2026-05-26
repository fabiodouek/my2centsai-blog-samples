# upstream-patched/

The two files in this directory are **finished, patched versions** of files from the `cloudflare/claude-managed-agents` template (commit baseline matches what shipped on 2026-05-19). They contain both fixes from `../patches/` already applied, so you can either:

- **Apply the patches** in `../patches/` to your own clone (clean diff, lets you carry the fixes forward across upstream updates), **or**
- **Copy these files as-is** over the matching paths in your clone (faster if you just want the demo to run).

Mapping to your `cloudflare/claude-managed-agents` clone:

| File here | Copies to |
|---|---|
| `Dockerfile` | `<your-clone>/Dockerfile` |
| `src/microvm/sandbox.ts` | `<your-clone>/src/microvm/sandbox.ts` |

What's changed vs upstream:

- **`Dockerfile`** — base image bumped from `cloudflare/sandbox:0.10.1` to `cloudflare/sandbox:0.10.2-python` so Python 3.10 is pre-installed and the bash tool can run `python3` without an `apt-get install` chase.
- **`src/microvm/sandbox.ts`** — adds a CA-trust step between `setEnvVars` and `startProcess(ant ...)` (inside the `Sandbox` Durable Object). The Cloudflare egress-proxy CA is mounted at `/etc/cloudflare/certs/cloudflare-containers-ca.crt` but the base image does not auto-add it to the system trust store, so outbound HTTPS from inside the container fails. The inserted block copies the CA into `/usr/local/share/ca-certificates/` and runs `update-ca-certificates` once per session boot.

See the blog post's [Step 3](https://my2cents.ai/deep-dive/claude-managed-agents-self-hosted-sandboxes/#step-3-deploy-the-template) for the full narrative.
