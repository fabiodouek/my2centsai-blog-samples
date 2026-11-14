#!/usr/bin/env bash
# What the metadata server tells each workload (run after 02_isolation.sh, which leaves the pods in place).
cd "$(dirname "$0")"
TPL=$(kubectl get sandboxclaim t2-claim -o jsonpath='{.status.sandbox.name}')
echo "=== runc pod ===";                kubectl exec -i runc-probe -c probe -- python3 - < metadata_probe.py
echo "=== direct Sandbox (gVisor) ==="; kubectl exec -i direct-sandbox -c probe -- python3 - < metadata_probe.py
echo "=== templated Sandbox (gVisor) ==="; kubectl exec -i "$TPL" -c python-runtime -- python3 - < metadata_probe.py
