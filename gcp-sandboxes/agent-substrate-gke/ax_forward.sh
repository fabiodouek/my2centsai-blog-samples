#!/usr/bin/env bash
# Two port-forwards that restart themselves, so the measured runs do not depend on each CLI's own tunnel handling:
#   ax-server  -> localhost:18080   (use with AX_SERVER=localhost:18080)
#   ate-api    -> localhost:18443   (use with kubectl-ate --endpoint localhost:18443; common.kate does this when ATE_ENDPOINT is set)
source "$(dirname "$0")/env.sh"
fwd() { while true; do kubectl --context "$CTX" -n "$1" port-forward "svc/$2" "$3" >/dev/null 2>&1 || true; sleep 1; done; }
fwd ax-system ax-server 18080:8080 &
fwd ate-system api 18443:443 &
wait
