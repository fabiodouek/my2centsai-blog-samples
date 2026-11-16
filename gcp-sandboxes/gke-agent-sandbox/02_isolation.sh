#!/usr/bin/env bash
# Test 2: the same probe from a runc pod, a templated Sandbox and a directly created Sandbox.
# Needs: controller + secure-sandbox-policy installed (01_install.sh), a gVisor pool, manifests/*.
set -uo pipefail
cd "$(dirname "$0")"
kubectl apply -f manifests/runc-pods.yaml -f manifests/template-secure.yaml -f manifests/warmpool.yaml -f manifests/sandbox-direct-secure.yaml
kubectl wait pod/runc-probe pod/target --for=condition=Ready --timeout=180s
kubectl wait sandboxwarmpool python-sandbox-warmpool --for=jsonpath='{.status.readyReplicas}'=2 --timeout=300s
kubectl wait sandbox direct-sandbox --for=condition=Ready --timeout=300s
cat <<YAML | kubectl apply -f -
apiVersion: extensions.agents.x-k8s.io/v1beta1
kind: SandboxClaim
metadata: {name: t2-claim, namespace: default}
spec: {warmPoolRef: {name: python-sandbox-warmpool}}
YAML
kubectl wait sandboxclaim t2-claim --for=condition=Ready --timeout=120s
TPL=$(kubectl get sandboxclaim t2-claim -o jsonpath='{.status.sandbox.name}')
TARGET_IP=$(kubectl get pod target -o jsonpath='{.status.podIP}')
for who in "runc pod (system node)|runc-probe|probe" "templated Sandbox (gVisor)|$TPL|python-runtime" "direct Sandbox (gVisor)|direct-sandbox|probe"; do
  IFS='|' read -r label pod ctr <<<"$who"
  echo; echo "=== $label ==="
  kubectl exec -i "$pod" -c "$ctr" -- sh -s "$TARGET_IP" < probe.sh
done
echo; echo "=== reach the templated Sandbox from the runc pod (NetworkPolicy ingress) ==="
TPL_IP=$(kubectl get pod "$TPL" -o jsonpath='{.status.podIP}')
kubectl exec runc-probe -- python3 -c "
import urllib.request as u
try: print('reachable: HTTP', u.urlopen('http://$TPL_IP:8888/', timeout=4).status)
except Exception as e: print('blocked:', type(e).__name__)"
echo; kubectl get networkpolicy -A
echo; echo "=== what the controller put in the templated Sandbox's pod spec ==="
kubectl get pod "$TPL" -o jsonpath='dnsPolicy={.spec.dnsPolicy} dnsConfig={.spec.dnsConfig}{"\n"}'
kubectl get pod direct-sandbox -o jsonpath='dnsPolicy={.spec.dnsPolicy} dnsConfig={.spec.dnsConfig}{"\n"}'
echo; echo "=== kubectl port-forward into a gVisor pod ==="
(kubectl port-forward pod/direct-sandbox 18889:8888 >/tmp/pf.log 2>&1 &) ; sleep 2
curl -s -m 3 -o /dev/null -w 'curl through the port-forward: HTTP %{http_code}\n' localhost:18889/; grep -o 'connection refused' /tmp/pf.log | head -1; pkill -f "port-forward pod/direct-sandbox" || true
