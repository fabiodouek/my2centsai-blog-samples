#!/usr/bin/env bash
# Deletes every sandbox on the local gateway ($OPENSHELL_GATEWAY, default "openshell"). With --stop,
# also stops it.
set -euo pipefail
# The local gateway from 00_gateway.sh, even when another one (part 2's osh-eks) is active.
export OPENSHELL_GATEWAY="${OPENSHELL_GATEWAY:-openshell}"

openshell status >/dev/null 2>&1 || { echo "OpenShell gateway not reachable: nothing to clean" >&2; exit 0; }

names() { openshell sandbox list -o json 2>/dev/null | python3 -c '
import json, sys
data = json.load(sys.stdin)
items = data.get("sandboxes", data) if isinstance(data, dict) else data
print("\n".join(s["name"] for s in items))'; }

deleted=0
rounds=0
seen=" "   # macOS ships bash 3.2: no associative arrays
while :; do
  batch=$(names)
  [ -z "$batch" ] && break
  rounds=$((rounds + 1))
  if [ "$rounds" -gt 60 ]; then echo "still listed after 60 rounds: $(echo $batch)" >&2; exit 1; fi
  for n in $batch; do
    # Deletion is asynchronous ("cleanup is pending"); ask once per sandbox, then wait.
    case "$seen" in *" $n "*) continue ;; esac
    openshell sandbox delete "$n" >/dev/null && deleted=$((deleted + 1))
    seen="${seen}${n} "
  done
  sleep 1
done
echo "deleted ${deleted} sandbox(es)"
openshell sandbox list

if [ "${1:-}" = "--stop" ]; then
  brew services stop openshell
fi
