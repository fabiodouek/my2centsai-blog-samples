#!/usr/bin/env bash
# Test 1: a first sandbox from the CLI. Create, inspect, run commands, move a file in and
# out, read the logs, delete.
set -euo pipefail
cd "$(dirname "$0")"

NAME="first"
IMAGE="openshell-workload:0.1.6"
say() { printf '\n$ %s\n' "$*"; }

openshell status >/dev/null 2>&1 || { echo "OpenShell gateway not reachable: run ./00_gateway.sh first" >&2; exit 1; }

say "openshell sandbox create --name ${NAME} --from ${IMAGE} --detach"
start=$(python3 -c 'import time; print(time.time())')
openshell sandbox create --name "$NAME" --from "$IMAGE" --detach --no-tty
end=$(python3 -c 'import time; print(time.time())')
python3 -c "print(f'create to Ready: {${end} - ${start}:.1f} s')"

say "openshell sandbox list"
openshell sandbox list

say "openshell sandbox get ${NAME}"
openshell sandbox get "$NAME"

say "openshell sandbox exec ${NAME} -- uname -a"
openshell sandbox exec "$NAME" --no-login-shell --no-tty -- uname -a
say "openshell sandbox exec ${NAME} -- id"
openshell sandbox exec "$NAME" --no-login-shell --no-tty -- id

say "openshell sandbox upload ${NAME} ./README.md /sandbox/"
openshell sandbox upload "$NAME" ./README.md /sandbox/ --no-git-ignore
tmp=$(mktemp -d)
say "openshell sandbox download ${NAME} /sandbox/README.md ${tmp}/"
openshell sandbox download "$NAME" /sandbox/README.md "${tmp}/"
if cmp -s README.md "${tmp}/README.md"; then echo "upload round trip: ok"; else echo "upload round trip: MISMATCH" >&2; exit 1; fi
rm -rf "$tmp"

say "openshell logs ${NAME} -n 15"
openshell logs "$NAME" -n 15 2>/dev/null || openshell logs "$NAME" | tail -15

say "openshell sandbox delete ${NAME}"
openshell sandbox delete "$NAME"
openshell sandbox list
