#!/usr/bin/env bash
# Test 1: three ways to get Agent Sandbox onto the cluster, and what the admission policies say.
set -uo pipefail
cd "$(dirname "$0")"
: "${CLUSTER:?source env.sh first}" "${ZONE:?}" "${AGENT_SANDBOX_VERSION:?}"
V=$AGENT_SANDBOX_VERSION

echo "### A. The managed add-on flag (gcloud beta only; the console has no row for it)"
time gcloud beta container clusters update "$CLUSTER" --zone "$ZONE" --enable-agent-sandbox
gcloud beta container clusters describe "$CLUSTER" --zone "$ZONE" --format='yaml(addonsConfig.agentSandboxConfig)'
kubectl get crd | grep agents.x-k8s.io
kubectl get validatingadmissionpolicy | grep -i sandbox
kubectl get all -n gke-managed-agentsandbox
echo "### turning it off again"
time gcloud beta container clusters update "$CLUSTER" --zone "$ZONE" --no-enable-agent-sandbox
echo "left behind:"; kubectl get crd | grep agents.x-k8s.io; kubectl get validatingadmissionpolicy | grep -i sandbox
# remove the leftovers so the next step starts clean
kubectl delete validatingadmissionpolicybinding sandbox-core-binding sandbox-hardening-binding
kubectl delete validatingadmissionpolicy sandbox-core-policy sandbox-hardening-policy
kubectl delete crd sandboxclaims.extensions.agents.x-k8s.io sandboxes.agents.x-k8s.io \
  sandboxtemplates.extensions.agents.x-k8s.io sandboxwarmpools.extensions.agents.x-k8s.io

echo "### B. The documented path: the open-source controller ($V)"
time kubectl apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/$V/sandbox-with-extensions.yaml"
time kubectl rollout status deployment/agent-sandbox-controller -n agent-sandbox-system --timeout=120s
kubectl get crd | grep agents.x-k8s.io

echo "### C. The recommended admission policy, then two Sandboxes it must reject"
RAW=https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/$V
kubectl apply -f "$RAW/examples/policy/vap/secure-sandbox-policy.yaml" -f "$RAW/examples/policy/vap/secure-sandbox-binding.yaml"
kubectl apply -f manifests/sandbox-reject-root.yaml
kubectl apply -f manifests/sandbox-reject-microvm.yaml

echo "### D. The Sandbox Router (needed by the Python client)"
B=$RAW/sandbox-router/deploy
kubectl apply -f "$B/serviceaccount.yaml" -f "$B/rbac.yaml"
curl -sSL "$B/deployment.yaml" | sed "s|sandbox-router-go:latest|sandbox-router-go:$V|" | kubectl apply -f -
kubectl apply -f "$B/service.yaml"
kubectl rollout status deployment/sandbox-router -n agent-sandbox-system --timeout=180s
