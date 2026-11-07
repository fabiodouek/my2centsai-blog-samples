#!/usr/bin/env bash
# The same server, driven with the osb CLI: create, list, run a command, move a file, read metrics, kill.
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN="${OSB_DOMAIN:-127.0.0.1:8080}"
API_KEY="${OSB_API_KEY:-local-dev-key}"   # the api_key in sandbox.toml
IMAGE="python:3.12-slim"

if ! curl -fsS -m 3 "http://${DOMAIN}/health" >/dev/null 2>&1; then
  echo "OpenSandbox server not reachable at ${DOMAIN}: run ./00_server.sh first" >&2
  exit 1
fi
# /health needs no key, so ask for something that does.
status="$(curl -s -m 10 -o /dev/null -w '%{http_code}' -H "OPEN-SANDBOX-API-KEY: ${API_KEY}" "http://${DOMAIN}/v1/sandboxes?pageSize=1")" || status="none"
case "$status" in
  2??) ;;
  401) echo "OpenSandbox server at ${DOMAIN} rejected the API key: set OSB_API_KEY to the api_key in sandbox.toml" >&2; exit 1 ;;
  none|000) echo "OpenSandbox server at ${DOMAIN} did not answer the API key check within 10 s: check server.log" >&2; exit 1 ;;
  *) echo "OpenSandbox server at ${DOMAIN} answered HTTP ${status} to the API key check: check server.log" >&2; exit 1 ;;
esac

# osb_quiet runs the CLI against the local config file; osb also prints the command first.
osb_quiet() { uvx --from opensandbox-cli==1.2.0 osb --config ./osb.toml "$@"; }
osb() {
  local shown="" arg
  for arg in "$@"; do [[ "$arg" == *" "* ]] && arg="\"$arg\""; shown+=" $arg"; done
  echo "\$ osb${shown}"
  osb_quiet "$@"
}

# Kill the sandbox on the way out, even if a step fails, so a crashed run leaves nothing behind.
id=""
cleanup() { [[ -z "$id" ]] || osb_quiet sandbox kill "$id" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# The CLI keeps the server address and key in ./osb.toml (gitignored).
[[ -f osb.toml ]] || osb_quiet config init >/dev/null
osb config set connection.domain "$DOMAIN"
osb config set connection.api_key "$API_KEY"

echo
echo "\$ osb sandbox create --image ${IMAGE} --timeout 10m -o json"
created="$(osb_quiet sandbox create --image "$IMAGE" --timeout 10m -o json)"
echo "$created"
id="$(printf '%s' "$created" | uv run python -c 'import json, sys; print(json.load(sys.stdin)["id"])' 2>/dev/null)" || id=""
if [[ -z "$id" ]]; then
  echo "could not read the sandbox id from: ${created} (run ./99_cleanup.sh to remove any sandbox it created)" >&2
  exit 1
fi

echo
osb sandbox list
echo
osb command run "$id" -- ls /
echo
osb file write "$id" /tmp/hello.txt --content "hello from the cli"
osb file cat "$id" /tmp/hello.txt
echo; echo
osb sandbox metrics "$id"
echo
osb sandbox kill "$id"
id=""

echo
osb sandbox list
