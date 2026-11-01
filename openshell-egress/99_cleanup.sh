#!/usr/bin/env bash
# Remove everything this directory's tests create: sandboxes, providers, the provider profiles
# they imported, the push-delivery override if a run was interrupted, and the echo server.
source "$(dirname "$0")/lib.sh"
for n in deny bins l7 live tls creds out rot logs; do os sandbox delete "$n" >/dev/null 2>&1 && echo "  deleted sandbox ${n}" & done
wait
for p in e-github e-anthropic e-echo e-rot; do os provider delete "$p" >/dev/null 2>&1 && echo "  deleted provider ${p}"; done
for p in github anthropic echo-demo; do os profile delete "$p" >/dev/null 2>&1 && echo "  deleted profile ${p}"; done
if [[ "$(command helm --kube-context "$KUBE_CONTEXT" -n "$NS" get values openshell -o json | jq -r '.gatewayConfig["openshell.gateway"].config_delivery_mode // empty')" == push ]]; then
  set_delivery poll && echo "  config_delivery_mode back to poll"
fi
kubectl delete namespace echo --ignore-not-found >/dev/null && echo "  deleted namespace echo"
echo "sandboxes left: $(os sandbox list -o json 2>/dev/null | jq '.sandboxes | length')"
