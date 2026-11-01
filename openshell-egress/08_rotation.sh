#!/usr/bin/env bash
# Test 8: rotation and revocation with the echo-demo provider (dummy values, shown back by the echo
# server). A process started before a rotation, one started after, then detach and delete.
source "$(dirname "$0")/lib.sh"
NAME="rot"
PROVIDER="e-rot"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; os provider delete "$PROVIDER" >/dev/null 2>&1; }
trap cleanup EXIT
cleanup
import_profile echo-demo
whoami_line() { in_sb "$NAME" sh -c "curl -sS -m 8 -H \"Authorization: Bearer ${1:-\$ECHO_TOKEN}\" http://${ECHO_HOST}/whoami | jq -r 'if .error then \"\(.error)\" else .headers.Authorization end'"; }

say "openshell provider create --name ${PROVIDER} --type echo-demo --credential ECHO_TOKEN   (ECHO_TOKEN=demo-token-1)"
ECHO_TOKEN=demo-token-1 os provider create --name "$PROVIDER" --type echo-demo --credential ECHO_TOKEN >/dev/null
make_sandbox "$NAME" --provider "$PROVIDER" --policy "${HERE}/policies/echo-read-write.yaml" || exit 1
OLD="$(in_sb "$NAME" printenv ECHO_TOKEN)"
echo "  placeholder: ${OLD}"
echo "  echo got:    $(whoami_line)"
# A long-running process that started with the old placeholder and calls the echo server later.
in_sb "$NAME" sh -c 'nohup sh -c "sleep 25; curl -sS -m 8 -H \"Authorization: Bearer \$ECHO_TOKEN\" http://echo.echo.svc.cluster.local/late > /tmp/late.json 2>&1" >/dev/null 2>&1 &'

say "openshell provider update ${PROVIDER} --credential ECHO_TOKEN --wait   (ECHO_TOKEN=demo-token-2)"
t=$(now); ECHO_TOKEN=demo-token-2 os provider update "$PROVIDER" --credential ECHO_TOKEN --wait >/dev/null 2>&1; echo "  applied after $(since "$t") s"
echo "  new process, placeholder: $(in_sb "$NAME" printenv ECHO_TOKEN)"
echo "  new process, echo got:    $(whoami_line)"
echo "  old placeholder, echo got: $(whoami_line "$OLD")"
sleep 20
echo "  the process started before the rotation, 25 s later: $(in_sb "$NAME" jq -r .headers.Authorization /tmp/late.json)"

say "openshell provider delete ${PROVIDER}   (still attached)"
os provider delete "$PROVIDER" 2>&1 | nocolor | grep -E '×|│|Deleted' | head -n 2 | sed 's/^/  /'
say "openshell sandbox provider detach ${NAME} ${PROVIDER} --wait"
t=$(now); os sandbox provider detach "$NAME" "$PROVIDER" --wait >/dev/null 2>&1; echo "  applied after $(since "$t") s"
echo "  new process, ECHO_TOKEN:   $(in_sb "$NAME" sh -c 'echo ${ECHO_TOKEN:-unset}')"
echo "  old placeholder, echo got: $(whoami_line "$OLD")"
say "openshell provider delete ${PROVIDER}"
os provider delete "$PROVIDER" 2>&1 | nocolor | sed 's/^/  /'
echo "  old placeholder, echo got: $(whoami_line "$OLD")"
