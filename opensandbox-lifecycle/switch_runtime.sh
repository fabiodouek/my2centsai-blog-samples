#!/usr/bin/env bash
# Switch the lifecycle server between runc and Kata: ./switch_runtime.sh runc|kata|kata-qemu|kata-clh
#
# "kata" means kata-qemu. One server process has one [secure_runtime], so this changes the runtime
# of every sandbox created after it (a create request cannot choose). It is a Helm upgrade of the
# server through ./server_config.py, which keeps everything else as it is (the gateway settings,
# part 5's egress image if set) and checks /health and /version through kubectl exec when the new
# pod is up. The server Deployment uses Recreate: the API is down for a few seconds, and a
# kubectl port-forward to the old pod ends (the Python scripts here start their own).
#
# Running sandboxes keep the runtime they were created with. Switch back to runc when you are
# done: parts 4 and 5 expect runc.
set -euo pipefail
cd "$(dirname "$0")"

case "${1:-}" in
  runc) runtime=runc ;;
  kata | kata-qemu) runtime=kata-qemu ;;
  kata-clh) runtime=kata-clh ;;
  *) echo "usage: $0 runc|kata|kata-qemu|kata-clh" >&2; exit 2 ;;
esac
[[ -f ../opensandbox-eks/env.sh ]] || { echo "run ../opensandbox-eks/01_cluster.sh first" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 (3.11 or later) is not installed" >&2; exit 1; }

started=$(date +%s)
./server_config.py --runtime "$runtime"
echo "switched to ${runtime} in $(( $(date +%s) - started )) s"
