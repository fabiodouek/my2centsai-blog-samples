# claude-managed-agents-self-hosted-sandboxes

Finished code for the PDF-summarizer demo in the post [Claude Managed Agents on Cloudflare: Hands-On with Self-Hosted Sandboxes](https://my2cents.ai/deep-dive/claude-managed-agents-self-hosted-sandboxes/).

The blog post walks through building this same code step by step. This directory exists so you can **skip the typing**: apply the two upstream-template patches, drop in the two new source files, and you have the demo built.

## What's in here

```
claude-managed-agents-self-hosted-sandboxes/
├── README.md                  # this file
├── patches/
│   ├── 01-bump-base-image.patch     # Dockerfile: bump cloudflare/sandbox to 0.10.2-python
│   └── 02-trust-egress-ca.patch     # src/microvm/sandbox.ts: install the egress-proxy CA
├── upstream-patched/          # finished, patched versions of the two upstream files
│   ├── README.md
│   ├── Dockerfile
│   └── src/microvm/sandbox.ts
├── src/
│   └── tools/
│       ├── custom-tools.ts    # the cf_list_pdfs + cf_read_pdf custom tools
│       └── pdf-text.ts        # PDF text extractor that works in the Workers runtime
├── wrangler.jsonc.snippet     # the PDFS R2 binding to merge into wrangler.jsonc
└── samples/                   # three public-domain GAO Highlights PDFs for the demo
```

## Two upstream-template patches (required for any session to complete)

The default `cloudflare/claude-managed-agents` template ships with two bugs that prevent the demo agent from completing a single tool round-trip. Both are documented in the blog's [Step 3](https://my2cents.ai/deep-dive/claude-managed-agents-self-hosted-sandboxes/#step-3-deploy-the-template) and need to be applied **before** the first `npm run deploy`. The patches here are the same fixes, shipped as `git apply`-able diffs:

- **`patches/01-bump-base-image.patch`** — one-line bump in `Dockerfile` from `cloudflare/sandbox:0.10.1` to `cloudflare/sandbox:0.10.2-python`. The default base image has no Python; the `-python` variant ships Python 3.10 so the bash tool can run `python3` without an `apt-get install` chase.
- **`patches/02-trust-egress-ca.patch`** — adds a CA-trust step in `src/microvm/sandbox.ts` between `setEnvVars` and `startProcess(ant ...)`. The base image mounts the Cloudflare egress-proxy CA at `/etc/cloudflare/certs/cloudflare-containers-ca.crt` but does not auto-add it to the system trust store. Without this, all outbound HTTPS from inside the container fails with `self-signed certificate in certificate chain`, `ant beta:worker run` cannot stream events from `api.anthropic.com`, and every session stalls at `requires_action` after the first tool call.

Apply both from the root of your `cloudflare/claude-managed-agents` clone:

```bash
cd ~/path/to/claude-managed-agents
git apply ~/path/to/this-companion/patches/01-bump-base-image.patch
git apply ~/path/to/this-companion/patches/02-trust-egress-ca.patch
```

If you would rather skip the patch step entirely, the [`upstream-patched/`](./upstream-patched/) directory ships the **finished, patched versions** of those two files. Copy them straight over the matching paths in your clone:

```bash
cp upstream-patched/Dockerfile                <your-clone>/Dockerfile
cp upstream-patched/src/microvm/sandbox.ts    <your-clone>/src/microvm/sandbox.ts
```

## Quick start (skip the typing)

These steps assume you have already followed the blog post's Steps 1-6 — meaning you have a deployed `claude-managed-agents-control-plane` Worker with all secrets pushed.

1. **Apply the two patches** above so subsequent sessions actually complete tool round-trips.

2. **Copy the two source files** into your `cloudflare/claude-managed-agents` clone:

   ```bash
   cp src/tools/custom-tools.ts <your-clone>/src/tools/custom-tools.ts
   cp src/tools/pdf-text.ts     <your-clone>/src/tools/pdf-text.ts
   ```

   `custom-tools.ts` is a full replacement for the file shipped in the upstream template. `pdf-text.ts` is new.

3. **Add the `PDFS` R2 binding** to `<your-clone>/wrangler.jsonc`. The snippet in `wrangler.jsonc.snippet` shows what to merge.

4. **Create the bucket**:

   ```bash
   npx wrangler r2 bucket create claude-managed-agents-pdfs
   ```

5. **Upload sample PDFs** to the `pdfs/` prefix. Three public-domain GAO Highlights reports ship in `samples/` — see [`samples/README.md`](./samples/README.md) for sources and licensing:

   ```bash
   npx wrangler r2 object put claude-managed-agents-pdfs/pdfs/gao-cybersecurity.pdf --file ./samples/gao-cybersecurity.pdf
   npx wrangler r2 object put claude-managed-agents-pdfs/pdfs/gao-dod-readiness.pdf --file ./samples/gao-dod-readiness.pdf
   npx wrangler r2 object put claude-managed-agents-pdfs/pdfs/gao-supply-chain.pdf  --file ./samples/gao-supply-chain.pdf
   ```

6. **Redeploy** from `<your-clone>`:

   ```bash
   npm run deploy
   ```

7. **Create an agent** in the dashboard, tick `cf_list_pdfs` and `cf_read_pdf` in the per-agent tool list, and save.

8. **Try it**:

   > Summarize the three PDFs in the `pdfs/` prefix of the R2 bucket. For each one, give me three bullet points covering scope, headline number, and risks.

   The agent calls `cf_list_pdfs` once, then `cf_read_pdf` per object, and returns a structured summary.

## A note on PDF extraction in Workers

PDF parsing in the Workers runtime is awkward: no native binaries, strict CPU limits, and most NPM PDF libraries either pull in Node-only deps or break under the bundler. The `pdf-text.ts` helper here is a lightweight pure-JS extractor that works for text-based PDFs (reports, articles, slide exports). Image-only or scanned PDFs will return `(no extractable text)` because the helper does not OCR — that would require a Workers AI vision-model path, which is intentionally out of scope for this demo.

## Cleanup

When you are done:

```bash
npx wrangler r2 bucket delete claude-managed-agents-pdfs
# Then follow the blog post's Cleanup section to tear down the rest of the deploy.
```

## License

MIT, same as `cloudflare/claude-managed-agents`.
