#!/usr/bin/env bash
# Test 10: logs. OpenShell writes OCSF events (Open Cybersecurity Schema Framework) for network
# and policy decisions. `openshell logs` shows them as one-line summaries; the per-sandbox setting
# ocsf_json_enabled makes the supervisor also write each event as JSON to its log (stderr),
# where a cluster log collector picks it up.
source "$(dirname "$0")/lib.sh"
NAME="logs"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT
say "openshell sandbox create --name ${NAME} --policy policies/echo-read-only.yaml"
make_sandbox "$NAME" --policy "${HERE}/policies/echo-read-only.yaml" || exit 1
say "openshell settings set ${NAME} --key ocsf_json_enabled --value true"
os settings set "$NAME" --key ocsf_json_enabled --value true 2>&1 | nocolor | sed 's/^/  /'
sleep 12   # supervisors pick up settings on their next poll (every 10 s)

say "three requests: allowed, denied at L7, denied at L4"
in_sb "$NAME" sh -c 'for m in GET POST; do printf "  %-5s echo server  %s\n" $m "$(curl -sS -m 5 -X $m -o /dev/null -w "%{http_code}" http://echo.echo.svc.cluster.local/items)"; done
  printf "  GET   pypi.org     %s\n" "$(curl -sS -m 5 -o /dev/null -w "%{http_code}" https://pypi.org/ 2>/dev/null)"'
sleep 2
SUP="$(kubectl -n "$NS" get pods -l "openshell.ai/boundary-pair=$(sandbox_id "$NAME"),openshell.ai/boundary-role=supervisor" -o jsonpath='{.items[0].metadata.name}')"
JSON="$(kubectl -n "$NS" logs "$SUP" 2>/dev/null | grep -o 'OCSF-JSON {.*' | sed 's/^OCSF-JSON //')"

say "kubectl logs <supervisor pod> | grep OCSF-JSON   (event classes since the sandbox started)"
jq -r '"\(.class_uid) \(.class_name)"' <<<"$JSON" | sort | uniq -c | sort -rn | sed 's/^/  /'
say "the L7 denial as JSON (selected fields)"
jq -c 'select(.class_uid == 4002 and .action == "Denied") | {class_name, severity, action, disposition, method: .http_request.http_method, url: (.http_request.url | "\(.scheme)://\(.hostname)\(.path)"), firewall_rule, message}' <<<"$JSON" | tail -n 1 | jq . | sed 's/^/  /'
