#!/usr/bin/env bash
# Kill every sandbox, stop the server started by 00_server.sh, remove leftover sandbox containers.
# Safe to run when nothing is running. If a step fails, the later steps still run (the server is stopped and the
# containers are removed anyway) and the script exits non-zero at the end.
set -euo pipefail
cd "$(dirname "$0")"

DOMAIN="${OSB_DOMAIN:-127.0.0.1:8080}"
URL="http://${DOMAIN}"
healthy() { curl -fsS -m 2 "${URL}/health" >/dev/null 2>&1; }
# True when the pid belongs to the server 00_server.sh starts (a recycled pid will not match).
is_server_pid() { ps -p "$1" -o command= 2>/dev/null | grep -q "opensandbox-server"; }

rc=0

# 1. Sandboxes: list every page, kill what was listed, and list again until nothing is left.
#    OSB_PAGE_SIZE (default 100) only exists so the paging can be tested with a few sandboxes.
if healthy; then
  OSB_PAGE_SIZE="${OSB_PAGE_SIZE:-100}" uv run python - <<'PYTHON' || rc=$?
import os
import sys
import time

from opensandbox.models.sandboxes import SandboxFilter
from opensandbox.sync import SandboxManagerSync

from common import connection, require_server

require_server()  # one line and exit 1 if the server is down or rejects the API key

page_size = int(os.environ["OSB_PAGE_SIZE"])
killed: set[str] = set()
last_pass = 0
try:
    with SandboxManagerSync.create(connection_config=connection()) as manager:
        for _ in range(20):
            ids, page = [], 1
            while True:
                result = manager.list_sandbox_infos(SandboxFilter(page_size=page_size, page=page))
                ids += [info.id for info in result.sandbox_infos]
                if not result.pagination.has_next_page:
                    break
                page += 1
            if not ids:
                print(f"killed {len(killed)} sandboxes, 0 left")
                sys.exit(0)
            for sandbox_id in ids:
                try:
                    manager.kill_sandbox(sandbox_id)
                    killed.add(sandbox_id)
                except Exception as error:  # keep going; the next pass lists what is still there
                    print(f"could not kill {sandbox_id}: {error}", file=sys.stderr)
            last_pass = len(ids)
            time.sleep(1)
except Exception as error:
    sys.exit(f"could not list or kill sandboxes: {error}")
sys.exit(f"sandboxes are still listed after 20 passes ({last_pass} on the last one)")
PYTHON
else
  echo "server not reachable at ${DOMAIN}: no sandboxes to list"
fi

# 2. The server, if 00_server.sh started it.
if [[ -f server.pid ]]; then
  pid="$(cat server.pid)"
  if is_server_pid "$pid"; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      # A hung server: stop its worker process first, then the wrapper that started it.
      pkill -KILL -P "$pid" 2>/dev/null || true
      kill -KILL "$pid" 2>/dev/null || true
      sleep 1
      if kill -0 "$pid" 2>/dev/null; then
        echo "server (pid ${pid}) could not be stopped" >&2
        exit 1
      fi
      echo "server (pid ${pid}) did not stop within 10 s: killed"
    else
      echo "server stopped (pid ${pid})"
    fi
  else
    echo "server not running (stale pid file)"
  fi
  rm -f server.pid
elif healthy; then
  echo "a server is answering at ${URL} but was not started by 00_server.sh: left running"
fi

# 3. Containers the server could not clean up: stopped sandboxes, sidecars, and any from a crashed run.
if ! leftover="$(
  docker ps -aq --filter label=opensandbox.io/id
  docker ps -aq --filter label=opensandbox.io/egress-sidecar-for
)"; then
  echo "Docker is not running: could not check for leftover sandbox containers" >&2
  rc=1
elif [[ -n "$leftover" ]]; then
  # shellcheck disable=SC2086
  docker rm -f $leftover >/dev/null
  echo "removed $(echo "$leftover" | wc -l | tr -d ' ') leftover sandbox containers"
else
  echo "no leftover sandbox containers"
fi

if [[ "$rc" -ne 0 ]]; then
  echo "cleanup finished with errors (see above)" >&2
fi
exit "$rc"
