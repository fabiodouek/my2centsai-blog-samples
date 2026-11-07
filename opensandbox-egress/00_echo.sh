#!/usr/bin/env bash
# Deploy the in-cluster echo server (manifests/echo-server.yaml) and check it from a throwaway pod.
#
#   ./00_echo.sh            apply, wait for the rollout, curl both Services from a probe pod
#   ./00_echo.sh delete     remove the echo server (Deployment, Services, ConfigMap)
#
# Needs ../opensandbox-eks/01_cluster.sh .. 04_install.sh to have run (the cluster, the ECR python image
# and the opensandbox namespace). Safe to run again.
set -euo pipefail
cd "$(dirname "$0")"

[[ -f ../opensandbox-eks/env.sh ]] || { echo "run ../opensandbox-eks/01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ../opensandbox-eks/env.sh
: "${ECR_REGISTRY:?env.sh does not set ECR_REGISTRY}" "${ECR_PREFIX:?env.sh does not set ECR_PREFIX}" "${KUBE_CONTEXT:?env.sh does not set KUBE_CONTEXT}"
export ECR_REGISTRY ECR_PREFIX
command -v envsubst >/dev/null || { echo "envsubst is not installed (gettext)" >&2; exit 1; }

k() { kubectl --context "$KUBE_CONTEXT" "$@"; }
# shellcheck disable=SC2016  # envsubst takes a literal list of names, so the single quotes are meant
VARS='${ECR_REGISTRY} ${ECR_PREFIX}'
render() { envsubst "$VARS" <manifests/echo-server.yaml; }

if [[ "${1:-}" == "delete" ]]; then
  render | k delete --ignore-not-found -f -
  exit 0
fi
if (( $# > 0 )); then echo "usage: ./00_echo.sh [delete]" >&2; exit 2; fi

render | k apply -f -
k -n opensandbox rollout status deploy/echo --timeout=180s

# A probe pod in the same namespace, outside any egress policy, to see the echo server's own answer.
probe="echo-probe-$$"
trap 'k -n opensandbox delete pod "$probe" --ignore-not-found --wait=false >/dev/null 2>&1 || true' EXIT
for svc in echo echo-alt; do
  printf '%s: ' "$svc"
  k -n opensandbox run "$probe" --rm -i --restart=Never --quiet \
    --image="${ECR_REGISTRY}/${ECR_PREFIX}/python:3.12-slim" --labels=example=opensandbox-egress -- \
    python3 -c "import urllib.request as u; print(u.urlopen('http://${svc}.opensandbox.svc.cluster.local/probe').read().decode())"
done
