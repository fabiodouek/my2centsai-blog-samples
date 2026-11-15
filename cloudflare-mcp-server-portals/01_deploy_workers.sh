#!/usr/bin/env bash
# Deploy the two MCP servers to Workers (workers.dev). Needs CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID
# in the environment, in .env, or in ~/.config/my2centsai/cloudflare-experiments.env.
set -euo pipefail
cd "$(dirname "$0")"
for f in .env "$HOME/.config/my2centsai/cloudflare-experiments.env"; do
  [ -f "$f" ] && { set -a; . "$f"; set +a; }
done
cd workers
for w in notes tickets; do
  echo "== $w-mcp"
  (cd "$w" && npm install --silent && npx wrangler deploy 2>&1 | grep -E "Uploaded|https://|Deployed")
done
