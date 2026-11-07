#!/usr/bin/env bash
# Start the OpenSandbox server on this machine (Docker runtime) from ./sandbox.toml.
# Safe to run twice: if the server is already up it says so and exits 0.
set -euo pipefail
cd "$(dirname "$0")"

VERSION="1.2.0"
DOMAIN="${OSB_DOMAIN:-127.0.0.1:8080}"   # must match [server] host and port in sandbox.toml
URL="http://${DOMAIN}"
IMAGES=(
  "opensandbox/execd:release-${VERSION}"
  "opensandbox/egress:release-${VERSION}"
  "python:3.12-slim"
)

healthy() { curl -fsS -m 2 "${URL}/health" >/dev/null 2>&1; }
# True when the pid belongs to the server this script starts (a recycled pid will not match).
is_server_pid() { ps -p "$1" -o command= 2>/dev/null | grep -q "opensandbox-server"; }

# Already running? Then there is nothing to do.
if [[ -f server.pid ]] && is_server_pid "$(cat server.pid)"; then
  if healthy; then
    echo "OpenSandbox server already running at ${URL} (pid $(cat server.pid))"
    exit 0
  fi
  echo "OpenSandbox server process (pid $(cat server.pid)) is running but ${URL}/health does not answer: if it was just started wait a moment, otherwise run ./99_cleanup.sh and start again" >&2
  exit 1
fi
if healthy; then
  echo "OpenSandbox server already running at ${URL} (not started by this script)"
  exit 0
fi
rm -f server.pid   # a stale pid file: its process is gone or is not the server

command -v uvx >/dev/null || { echo "uv is not installed: see https://docs.astral.sh/uv/" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is not running: start Docker Desktop (or the Docker daemon) and run this again" >&2; exit 1; }

# The packaged example config is the starting point for sandbox.toml. Show where it is stale.
example="$(mktemp)"
trap 'rm -f "$example"' EXIT
uvx "opensandbox-server@${VERSION}" init-config "$example" --example docker --force >/dev/null
echo "Packaged example (opensandbox-server ${VERSION} init-config --example docker) vs ./sandbox.toml:"
diff -U0 "$example" sandbox.toml | grep -E '^[-+](execd_image|image|mode) ' | sort -s -t ' ' -k1.2,1 || true
echo "(api_key, publish_host, [proxy] resolve_internal and [store] path differ too: local choices)"

# Pull the images now so the first sandbox does not pay for the download.
for image in "${IMAGES[@]}"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "pulling ${image}"
    docker pull -q "$image" >/dev/null
  fi
  echo "image ready: ${image}"
done

nohup uvx "opensandbox-server@${VERSION}" --config ./sandbox.toml >server.log 2>&1 &
echo $! >server.pid

for _ in $(seq 1 60); do
  if healthy; then
    echo "OpenSandbox server ${VERSION} ready at ${URL} (pid $(cat server.pid), log: server.log)"
    exit 0
  fi
  if ! kill -0 "$(cat server.pid)" 2>/dev/null; then
    echo "OpenSandbox server exited during startup; last lines of server.log:" >&2
    tail -n 15 server.log >&2
    rm -f server.pid
    exit 1
  fi
  sleep 1
done

echo "OpenSandbox server did not become healthy at ${URL} within 60 s; see server.log (./99_cleanup.sh stops it)" >&2
exit 1
