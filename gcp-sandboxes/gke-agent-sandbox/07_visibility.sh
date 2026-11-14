#!/usr/bin/env bash
# Test 7 wrapper: start the same CPU-and-log workload as runc and gVisor, give Cloud Monitoring time, compare, clean up.
set -uo pipefail
cd "$(dirname "$0")"
kubectl apply -f manifests/burn.yaml
kubectl wait pod/burn-runc pod/burn-gvisor --for=condition=Ready --timeout=120s
for i in 1 2 3 4 5 6; do echo "waiting for Cloud Monitoring to collect samples... $((i*60))s of 360s"; sleep 60; done
python3 07_visibility.py | python3 -c "
import json, sys
r = json.load(sys.stdin)
print('--- kubectl top pod'); [print(r['top'][k].splitlines()[-1]) for k in ('runc', 'gvisor')]
print('--- kubelet /stats/summary (usageNanoCores, workingSet bytes)')
for k, v in r['kubelet_summary'].items(): print(f'{k:7s}', v['cpu_usageNanoCores'], v['memory_workingSetBytes'])
print('--- Cloud Monitoring, container CPU (cores) and non-evictable memory (bytes)')
for k, v in r['monitoring'].items():
    cpu = v['core_usage_time']['series'][0]['latest']; mem = [s['latest'] for s in v['used_bytes']['series'] if s['memory_type'] == 'non-evictable'][0]
    print(f'{k:7s} cpu {cpu:.2f}  memory {mem:.0f}')
print('--- Cloud Logging, last stdout lines')
for k, v in r['logging'].items(): print(f'{k:7s}', v)
"
kubectl delete -f manifests/burn.yaml --wait=false
