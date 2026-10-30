#!/usr/bin/env bash
# The install guide's smoke test: create a task with no Workspace or Model,
# resume it, run a command in it over `ax ssh`, then delete it.
set -euo pipefail
source "$(dirname "$0")/env.sh"

TASK="ax-install-$(date +%s)"
ms() { python3 -c 'import time; print(int(time.time()*1000))'; }
timed() { local t0; t0=$(ms); "$@"; echo "  -> $(( $(ms) - t0 )) ms"; }

cat > "$RECORDS/task.yaml" <<EOF
apiVersion: ax.io/v1alpha1
kind: Task
metadata:
  name: ${TASK}
  atespace: default
spec:
  debug: true
  command: ["sleep", "infinity"]
EOF

step "apply (a new task starts Suspended)"
timed ax apply -f "$RECORDS/task.yaml"
ax describe task "$TASK"

step "resume"
timed ax resume task "$TASK"
ax describe task "$TASK"

step "ssh"
timed ax ssh "$TASK" -- sh -c 'echo installation-ok; uname -a; cat /proc/version'

step "delete"
timed ax delete task "$TASK"
"$AX_SRC/bin/ax" tunnel stop "$KUBE_CONTEXT" || true
