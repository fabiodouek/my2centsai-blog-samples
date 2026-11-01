#!/usr/bin/env bash
# Test 3: L7 REST rules on the echo server. A read-only endpoint without `enforcement` (so the
# default, audit), then with `enforcement: enforce`, then explicit method and path rules, and an
# encoded semicolon in a path (NVIDIA/OpenShell#3902, open at v0.1.6).
source "$(dirname "$0")/lib.sh"
NAME="l7"
URL="http://${ECHO_HOST}"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT
set_policy() { os policy set "$NAME" --policy "${HERE}/policies/$1" --wait >/dev/null 2>&1 || { echo "policy set $1 failed" >&2; exit 1; }; }
req() {  # method path -> "<code>  <method and path the echo server received>"
  in_sb "$NAME" sh -c "curl -sS -m 8 --path-as-is -X $1 -o /tmp/b -w '%{http_code}' '${URL}$2'; printf '  '; jq -r 'if .error then \"\(.error) (\(.layer))\" else \"echo got: \(.method) \(.path)\" end' /tmp/b 2>/dev/null"
}
row() { printf '  %-6s %-30s %s\n' "$1" "$2" "$(req "$1" "$2")"; }

say "openshell sandbox create --name ${NAME} --policy policies/echo-audit.yaml   (read-only, no enforcement field)"
make_sandbox "$NAME" --policy "${HERE}/policies/echo-audit.yaml" || exit 1
row GET /items
row POST /items
sleep 3   # let the supervisor flush its log
ocsf "$NAME" | grep -E 'HTTP:(GET|POST)' | cut -c1-120 | sed 's/^/  /' | tail -n 2

say "openshell policy set ${NAME} --policy policies/echo-enforce.yaml   (enforcement: enforce)"
set_policy echo-enforce.yaml
row GET /items
row POST /items

say "openshell policy set ${NAME} --policy policies/echo-rules.yaml   (GET /public/**, DELETE /objects/*)"
set_policy echo-rules.yaml
row GET /public/report
row GET /private/report
row GET /public/../private/report
row GET /public/%2e%2e/private/report
row DELETE /objects/backup
row DELETE /objects/backup%3Bold
sleep 3
ocsf "$NAME" | grep -E 'HTTP:DELETE' | cut -c1-120 | sed 's/^/  /' | tail -n 1
