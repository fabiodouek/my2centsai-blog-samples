#!/usr/bin/env bash
# Test 1: deny by default. A sandbox whose policy names one endpoint (the echo server, read-only):
# what DNS answers, what a denied connection looks like (EACCES), what a denied request looks like
# (HTTP 403 with a JSON body), and what `openshell logs --level warn` leaves out.
source "$(dirname "$0")/lib.sh"
NAME="deny"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT

say "openshell sandbox create --name ${NAME} --policy policies/echo-read-only.yaml"
make_sandbox "$NAME" --policy "${HERE}/policies/echo-read-only.yaml" || exit 1

say "DNS inside the sandbox"
in_sb "$NAME" sh -c 'for h in echo.echo.svc.cluster.local pypi.org; do printf "  %-30s %s\n" "$h" "$(getent hosts "$h" | cut -d" " -f1)"; done'

say "a host the policy doesn't name: the connection is refused (EACCES)"
in_sb "$NAME" sh -c 'curl -sS -m 5 https://pypi.org/ 2>&1 | sed "s/^/  curl:    /"
  python3 -c "import socket; socket.create_connection((\"pypi.org\", 443), timeout=5)" 2>&1 | tail -n 1 | sed "s/^/  python3: /"'

say "an allowed host, a method the policy doesn't allow: HTTP 403 with a JSON body"
in_sb "$NAME" sh -c 'curl -sS -m 5 -X POST -d x -D /tmp/h -o /tmp/b http://echo.echo.svc.cluster.local/items
  sed -n "1p" /tmp/h | tr -d "\r" | sed "s/^/  /"; grep -i "^x-openshell" /tmp/h | tr -d "\r" | sed "s/^/  /"
  jq -c "{error, layer, rule, detail}" /tmp/b | sed "s/^/  /"'
in_sb "$NAME" sh -c 'printf "  GET  /items -> %s\n" "$(curl -sS -m 5 -o /dev/null -w "%{http_code}" http://echo.echo.svc.cluster.local/items)"'

say "openshell logs ${NAME}"
ocsf "$NAME" | grep -E 'NET:OPEN|NET:REFUSE|HTTP:' | cut -c1-150 | sed 's/^/  /' | tail -n 6
say "openshell logs ${NAME} --level warn"
warn="$(os logs "$NAME" -n 200 --level warn 2>/dev/null | nocolor)"
echo "  OCSF events: $(grep -c '\[OCSF' <<<"$warn")"
echo "  WARN lines:  $(grep -c '\[WARN' <<<"$warn") (the network proxy's own log, L4 denials only)"
grep '\[WARN' <<<"$warn" | sed -E 's/^\[[0-9.]+\] \[[a-z]+\] +\[WARN *\] //' | cut -c1-150 | sed 's/^/  /' | tail -n 2
