#!/usr/bin/env bash
# What survives `ax suspend` / `ax resume`? Write a file to /tmp, one to
# /workspace and start a background process, suspend, resume, and look again.
# AX asks Substrate for a volumes-only snapshot, so only /workspace should
# come back (docs/runner.md: "the same files but a new process tree").
set -euo pipefail
source "$(dirname "$0")/env.sh"

TASK="suspend-$(date +%s)"
ms() { python3 -c 'import time; print(int(time.time()*1000))'; }
timed() { local t0; t0=$(ms); "$@"; echo "  -> $(( $(ms) - t0 )) ms"; }

cat > "$RECORDS/suspend-task.yaml" <<YAML
apiVersion: ax.io/v1alpha1
kind: Task
metadata:
  name: ${TASK}
  atespace: default
spec:
  debug: true
  command: ["sleep", "infinity"]
YAML

step "create and resume $TASK"
ax apply -f "$RECORDS/suspend-task.yaml"
ax resume task "$TASK" >/dev/null

step "write state: /tmp, /workspace (50 MB) and a background process"
ax ssh "$TASK" -- sh -c '
  echo tmp-ok > /tmp/marker
  echo workspace-ok > /workspace/marker
  dd if=/dev/urandom of=/workspace/blob bs=1M count=50 2>/dev/null
  nohup sleep 3000 >/dev/null 2>&1 &
  ls -la /tmp/marker /workspace/marker /workspace/blob
  echo "background sleep 3000 running: $(ps -eo args | grep -cx "sleep 3000")"; uptime'

step "suspend"
timed ax suspend task "$TASK"
ax describe task "$TASK" | grep -E 'Phase'

step "objects in the S3 snapshot bucket"
aws s3 ls --recursive --summarize "$SNAPSHOT_LOCATION" | tail -2

step "resume"
timed ax resume task "$TASK"

step "what came back"
ax ssh "$TASK" -- sh -c '
  cat /workspace/marker; ls -la /workspace/blob
  cat /tmp/marker 2>&1 || true
  echo "background sleep 3000 running: $(ps -eo args | grep -cx "sleep 3000")"; uptime' || true

step "where the checkpoint went"
for p in $(k -n ate-system get pods -l app=atelet -o name); do
  k -n ate-system logs "$p" -c atelet --since=5m 2>/dev/null
done | grep -o '"msg":"pruned local checkpoint","path":"[^"]*"' | tail -1 || true

ax delete task "$TASK" >/dev/null
"$AX_SRC/bin/ax" tunnel stop "$KUBE_CONTEXT" >/dev/null 2>&1 || true
