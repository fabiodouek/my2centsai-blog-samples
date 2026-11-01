#!/usr/bin/env bash
# Install OpenShell 0.1.6 on the cluster:
#
#   1. the Agent Sandbox controller v1.0.3 (OpenShell's Kubernetes driver creates a Sandbox
#      resource per sandbox, and this controller turns it into the workload Pod), image from ECR
#   2. the OpenShell chart, pulled once into charts/
#   3. namespace openshell, labelled Pod Security "restricted"
#   4. our own NLB Service for the gateway, created first so its hostname can go into the
#      gateway's certificate (the chart's PKI hook never re-issues a certificate)
#   5. helm upgrade --install with values/openshell.yaml
#   6. the CLI: the client mTLS bundle from the cluster, a port-forward on 127.0.0.1:17680, and
#      a gateway registration named osh-eks
#
#   ./05_install.sh                    install or converge
#   ./05_install.sh --show-preflight   first try the chart without Agent Sandbox, to show its
#                                      preflight error (only does anything before step 1 ran)
#
# Safe to run again: a re-run reuses the NLB hostname, the chart's PKI hook skips (its three
# Secrets exist), and Helm records one more revision with the same manifests.
set -euo pipefail
cd "$(dirname "$0")"

OPENSHELL_VERSION="0.1.6"
AGENT_SANDBOX_VERSION="v1.0.3"
NS="openshell"
RELEASE="openshell"
GATEWAY_NAME="osh-eks"            # CLI registration
LOCAL_PORT="${LOCAL_PORT:-17680}" # 17670 is the local gateway's port
CHART="charts/helm-chart-${OPENSHELL_VERSION}.tgz"
AS_MANIFEST="charts/agent-sandbox-${AGENT_SANDBOX_VERSION}.yaml"
MTLS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateways/${GATEWAY_NAME}/mtls"
LOGS="logs"

for tool in kubectl helm jq curl envsubst openshell; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
[[ -f env.sh ]] || { echo "env.sh not found: run ./01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
helm() { command helm --kube-context "$KUBE_CONTEXT" "$@"; }
mkdir -p charts "$LOGS"

script_start=$(date +%s)
duration() { local s=$1; if (( s >= 60 )); then printf '%dm %02ds' $((s / 60)) $((s % 60)); else printf '%ds' "$s"; fi; }
phase() { phase_start=$(date +%s); printf '\n\033[1m== %s\033[0m\n' "$1"; }
phase_done() { printf '   took %s\n' "$(duration $(( $(date +%s) - phase_start )))"; }
ok() { printf '  ok    %s\n' "$*"; }

render_values() {  # values with the ECR registry filled in
  ECR_REGISTRY="$ECR_REGISTRY" ECR_PREFIX="$ECR_PREFIX" envsubst '${ECR_REGISTRY} ${ECR_PREFIX}' <values/openshell.yaml
}

# --- 0. chart ----------------------------------------------------------------------------
phase "chart ${OPENSHELL_VERSION}"
if [[ ! -f "$CHART" ]]; then
  helm pull oci://ghcr.io/nvidia/openshell/helm-chart --version "$OPENSHELL_VERSION" -d charts >/dev/null
fi
ok "$(tar -xOzf "$CHART" helm-chart/Chart.yaml 2>/dev/null | grep -E '^(version|appVersion):' | tr '\n' ' ')(${CHART})"
phase_done

if [[ "${1:-}" == "--show-preflight" ]] && ! kubectl get crd sandboxes.agents.x-k8s.io >/dev/null 2>&1; then
  phase "the chart without Agent Sandbox (expected to fail)"
  kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  set +e
  helm upgrade --install "$RELEASE" "$CHART" -n "$NS" -f <(render_values) 2>&1 | tail -n 4
  set -e
  phase_done
fi

# --- 1. Agent Sandbox ----------------------------------------------------------------------
phase "Agent Sandbox ${AGENT_SANDBOX_VERSION} (image from ECR)"
if [[ ! -f "$AS_MANIFEST" ]]; then
  curl -fsSL "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${AGENT_SANDBOX_VERSION}/sandbox.yaml" \
    | sed "s#registry.k8s.io/agent-sandbox/agent-sandbox-controller:${AGENT_SANDBOX_VERSION}#${ECR_REGISTRY}/${ECR_PREFIX}/agent-sandbox/agent-sandbox-controller:${AGENT_SANDBOX_VERSION}#" \
    >"$AS_MANIFEST"
fi
grep -q "${ECR_REGISTRY}/${ECR_PREFIX}/agent-sandbox/" "$AS_MANIFEST" || { echo "${AS_MANIFEST}: controller image not rewritten to ECR" >&2; exit 1; }
kubectl apply --server-side -f "$AS_MANIFEST" >"$LOGS/agent-sandbox-apply.log"
kubectl -n agent-sandbox-system rollout status deployment/agent-sandbox-controller --timeout=5m >/dev/null
ok "agent-sandbox-controller ready; CRD $(kubectl get crd sandboxes.agents.x-k8s.io -o jsonpath='{.spec.versions[?(@.served==true)].name}' | tr ' ' ',')"
phase_done

# --- 2. namespace ----------------------------------------------------------------------------
phase "namespace ${NS}"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl label namespace "$NS" --overwrite pod-security.kubernetes.io/enforce=restricted \
  pod-security.kubernetes.io/enforce-version=latest >/dev/null
ok "namespace ${NS}, Pod Security enforce=restricted"
phase_done

# --- 3. NLB ----------------------------------------------------------------------------------
phase "gateway NLB (internet-facing, one source address)"
if hostname="$(kubectl -n "$NS" get service openshell-gateway-nlb -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null)" && [[ -n "$hostname" ]]; then
  ok "existing NLB ${hostname} (06_remote.sh manages its source address)"
else
  MY_IP="${MY_IP:-$(curl -fsS -m 10 https://checkip.amazonaws.com)}"
  MY_IP="$MY_IP" envsubst '${MY_IP}' <manifests/gateway-nlb.yaml | kubectl apply -f - >/dev/null
  nlb_start=$(date +%s)
  for _ in $(seq 1 90); do
    hostname="$(kubectl -n "$NS" get service openshell-gateway-nlb -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
    [[ -n "$hostname" ]] && break
    sleep 5
  done
  [[ -n "$hostname" ]] || { echo "the NLB got no hostname within 7.5 minutes" >&2; exit 1; }
  ok "NLB ${hostname} for ${MY_IP}/32, hostname after $(duration $(( $(date +%s) - nlb_start )))"
fi
phase_done

# --- 4. OpenShell ----------------------------------------------------------------------------
phase "helm upgrade --install ${RELEASE} (chart ${OPENSHELL_VERSION})"
prev_rev="$(helm -n "$NS" list --filter "^${RELEASE}\$" -o json | jq -r '.[0].revision // empty')"
if ! helm upgrade --install "$RELEASE" "$CHART" -n "$NS" -f <(render_values) \
    --set-json "pkiInitJob.serverDnsNames=[\"${hostname}\"]" \
    --wait --timeout 10m >"$LOGS/helm-openshell.log" 2>&1; then
  tail -n 25 "$LOGS/helm-openshell.log" >&2
  exit 1
fi
rev="$(helm -n "$NS" list --filter "^${RELEASE}\$" -o json | jq -r '.[0].revision')"
if [[ -z "$prev_rev" ]]; then
  ok "installed (revision ${rev})"
elif diff <(helm -n "$NS" get manifest "$RELEASE" --revision "$prev_rev") <(helm -n "$NS" get manifest "$RELEASE" --revision "$rev") >/dev/null; then
  ok "upgraded to revision ${rev}: manifests identical to revision ${prev_rev}"
else
  ok "upgraded to revision ${rev}: manifests changed since revision ${prev_rev}"
fi
kubectl -n "$NS" get pods -l "app.kubernetes.io/instance=${RELEASE}" -o wide | sed 's/^/  /'
kubectl -n "$NS" get pvc | sed 's/^/  /'
telemetry="$(kubectl -n "$NS" get statefulset "$RELEASE" -o json | jq -r '.spec.template.spec.containers[0].env[] | select(.name == "OPENSHELL_TELEMETRY_ENABLED") | .value')"
if [[ "$telemetry" == false ]]; then ok "gateway env OPENSHELL_TELEMETRY_ENABLED=false"; else echo "  FAIL  telemetry is '${telemetry}'" >&2; exit 1; fi
sans="$(kubectl -n "$NS" get secret openshell-server-tls -o jsonpath='{.data.tls\.crt}' | base64 -d \
  | openssl x509 -noout -ext subjectAltName | tail -n 1 | tr -d ' ')"
if [[ "$sans" == *"DNS:${hostname}"* ]]; then ok "server certificate lists the NLB hostname"; else echo "  WARN  server certificate SANs don't include ${hostname}: ${sans}"; fi
phase_done

# --- 5. CLI ----------------------------------------------------------------------------------
phase "CLI: mTLS bundle, port-forward on 127.0.0.1:${LOCAL_PORT}, gateway ${GATEWAY_NAME}"

pf_alive() { [[ -f .port-forward.pid ]] && kill -0 "$(cat .port-forward.pid)" 2>/dev/null; }
if pf_alive; then
  ok "port-forward already running (pid $(cat .port-forward.pid))"
else
  nohup kubectl -n "$NS" port-forward "svc/${RELEASE}" "${LOCAL_PORT}:8080" >"$LOGS/port-forward.log" 2>&1 &
  echo $! >.port-forward.pid
  for _ in $(seq 1 20); do nc -z 127.0.0.1 "$LOCAL_PORT" 2>/dev/null && break; sleep 0.5; done
  ok "port-forward started (pid $(cat .port-forward.pid)), log in ${LOGS}/port-forward.log"
fi

# Register first, then write the cluster's client bundle. `gateway add --local` fills the
# registration's mtls directory with the certificates of the local (Homebrew/Docker) gateway
# if this machine has one, which the cluster gateway then rejects (BadSignature).
mkdir -p "$MTLS_DIR"
chmod 700 "$MTLS_DIR"
if ! openshell gateway list 2>/dev/null | grep -qw "$GATEWAY_NAME"; then
  openshell gateway add "https://127.0.0.1:${LOCAL_PORT}" --local --name "$GATEWAY_NAME" >/dev/null 2>&1 || true
fi
for f in ca.crt tls.crt tls.key; do
  kubectl -n "$NS" get secret openshell-client-tls -o json | jq -r --arg k "$f" '.data[$k]' | base64 -d >"${MTLS_DIR}/${f}"
done
chmod 600 "${MTLS_DIR}/tls.key"
ok "client bundle from secret openshell-client-tls in ${MTLS_DIR} (key mode 600)"
openshell gateway select "$GATEWAY_NAME" >/dev/null
openshell status -g "$GATEWAY_NAME"
phase_done

printf '\ntotal %s\n' "$(duration $(( $(date +%s) - script_start )))"
echo "OpenShell ${OPENSHELL_VERSION} is running; the CLI's active gateway is ${GATEWAY_NAME}"
