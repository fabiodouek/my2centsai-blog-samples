#!/usr/bin/env bash
# Test 3: deny by default. curl may reach pypi.org; python3 may not, even to the same host;
# nothing may reach httpbin.org until a live policy update allows it for curl.
set -euo pipefail
# The local gateway from 00_gateway.sh, even when another one (part 2's osh-eks) is active.
export OPENSHELL_GATEWAY="${OPENSHELL_GATEWAY:-openshell}"
cd "$(dirname "$0")"

NAME="netdemo"
IMAGE="openshell-workload:0.1.6"
say() { printf '\n$ %s\n' "$*"; }
in_sandbox() { openshell sandbox exec "$NAME" --no-login-shell --no-tty -- "$@"; }

openshell status >/dev/null 2>&1 || { echo "OpenShell gateway not reachable: run ./00_gateway.sh first" >&2; exit 1; }

say "openshell sandbox create --name ${NAME} --from ${IMAGE} --policy policies/curl-only.yaml --detach"
openshell sandbox create --name "$NAME" --from "$IMAGE" --policy policies/curl-only.yaml --detach --no-tty

curl_code() { in_sandbox curl -sS -m 10 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null || true; }
py_get() {
  in_sandbox python3 -c "import urllib.request; print(urllib.request.urlopen('$1', timeout=10).status)" >/dev/null 2>&1 \
    && echo "200" || echo "denied"
}

code=$(curl_code https://pypi.org/simple/);  echo "pypi.org (curl):    ${code:-denied}"
echo "pypi.org (python3): $(py_get https://pypi.org/simple/)"
code=$(curl_code https://httpbin.org/get);      echo "httpbin.org (curl): $( [ "$code" = 200 ] && echo 200 || echo denied )"

say "openshell policy update ${NAME} --add-endpoint httpbin.org:443 --binary /usr/bin/curl --wait"
start=$(python3 -c 'import time; print(time.time())')
openshell policy update "$NAME" --add-endpoint httpbin.org:443 --binary /usr/bin/curl --wait
deadline=$((SECONDS + 60))
until [ "$(curl_code https://httpbin.org/get)" = 200 ]; do
  [ "$SECONDS" -lt "$deadline" ] || { echo "httpbin.org still denied 60 s after the update" >&2; openshell sandbox delete "$NAME"; exit 1; }
  sleep 0.2
done
end=$(python3 -c 'import time; print(time.time())')
echo "httpbin.org (curl): 200 after the update ($(python3 -c "print(f'{${end} - ${start}:.1f}')") s)"

say "openshell logs ${NAME} (deny decisions)"
openshell logs "$NAME" 2>/dev/null | grep -iE 'deny|denied' | tail -6

say "openshell sandbox delete ${NAME}"
openshell sandbox delete "$NAME"
