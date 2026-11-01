#!/usr/bin/env bash
# Test 4: live policy updates. How long a new rule takes to reach a running sandbox with the
# default delivery (supervisors poll the gateway every 10 s) and with push delivery, and what a
# new policy revision does to a connection that is already open. Push is a gateway setting
# (Helm gatewayConfig), so this script restarts the gateway twice and puts the default back.
source "$(dirname "$0")/lib.sh"
NAME="live"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; [[ "${PUSHED:-}" == 1 ]] && set_delivery poll && echo "  gateway.toml now: $(kubectl -n "$NS" get configmap openshell-config -o jsonpath='{.data.gateway\.toml}' | grep config_delivery_mode)"; }
trap cleanup EXIT
time_to_allow() {  # three runs: remove pypi.org, add it back, poll until curl gets through
  for i in 1 2 3; do
    os policy update "$NAME" --remove-endpoint pypi.org:443 --wait >/dev/null 2>&1
    sleep 2
    local t; t=$(now)
    os policy update "$NAME" --add-endpoint pypi.org:443 --binary /usr/bin/curl >/dev/null 2>&1
    until in_sb "$NAME" curl -s -m 3 -o /dev/null https://pypi.org/simple/ 2>/dev/null; do sleep 0.2; done
    printf '  run %s: allowed %s s after the update call\n' "$i" "$(since "$t")"
  done
}
stream_across_update() {
  in_sb "$NAME" sh -c 'curl -sS -N -m 40 "http://echo.echo.svc.cluster.local/slow?seconds=30" > /tmp/slow.txt 2>/tmp/slow.err; echo "exit $?" >> /tmp/slow.err' &
  local pid=$!
  sleep 8
  os policy update "$NAME" --add-endpoint "example.org:443" --binary /usr/bin/curl --wait >/dev/null 2>&1
  wait "$pid"
  in_sb "$NAME" sh -c 'echo "  lines received: $(grep -c tick /tmp/slow.txt) of 30"; sed "s/^/  /" /tmp/slow.err'
}

say "openshell sandbox create --name ${NAME} --policy policies/echo-l4.yaml   (delivery: poll, the default)"
make_sandbox "$NAME" --policy "${HERE}/policies/echo-l4.yaml" || exit 1
say "openshell policy update ${NAME} --add-endpoint pypi.org:443 --binary /usr/bin/curl   (no --wait)"
time_to_allow
say "a 30-second download from the echo server, and an unrelated policy update 8 s in"
stream_across_update
os sandbox delete "$NAME" >/dev/null 2>&1

say "helm upgrade openshell --reuse-values --set gatewayConfig.openshell\\.gateway.config_delivery_mode=push"
PUSHED=1
t=$(now); set_delivery push || exit 1; echo "  gateway back after $(since "$t") s"
make_sandbox "$NAME" --policy "${HERE}/policies/echo-l4.yaml" || exit 1
say "openshell policy update ${NAME} --add-endpoint pypi.org:443 --binary /usr/bin/curl   (delivery: push)"
time_to_allow
say "helm upgrade openshell --reuse-values --set gatewayConfig.openshell\\.gateway.config_delivery_mode=poll"
