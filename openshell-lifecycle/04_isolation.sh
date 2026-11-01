#!/usr/bin/env bash
# Test 4: what the workload can and can't do, with the default policy (no network rules).
# Every check runs inside one sandbox; each line shows the command and what happened.
source "$(dirname "$0")/lib.sh"
NAME="iso"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT
cleanup
os sandbox create --name "$NAME" --detach --no-tty >/dev/null 2>&1
check() {  # label, shell command run inside the sandbox
  local out rc
  out="$(in_sb "$NAME" sh -c "$2" 2>&1)"; rc=$?
  printf '  %-34s exit %-3s %s\n' "$1" "$rc" "$(tr '\n' ' ' <<<"$out" | cut -c1-90)"
}

say "who am I"
check "id" "id"
check "capabilities, seccomp" "grep -E '^(CapEff|NoNewPrivs|Seccomp):' /proc/self/status | tr -s '\t' ' '"
check "kernel (shared with the node)" "uname -r"
echo "  node kernel:                       $(kubectl get node "$(kubectl -n "$NS" get pod "default--${NAME}" -o jsonpath='{.spec.nodeName}')" -o jsonpath='{.status.nodeInfo.kernelVersion}')"

say "files (Landlock: read-write /sandbox and /tmp, read-only system paths, nothing else)"
check "write /sandbox/ok" "echo x > /sandbox/ok && echo written"
check "write /tmp/ok" "echo x > /tmp/ok && echo written"
check "write /var/tmp/x (not in policy)" "echo x > /var/tmp/x && echo written"
check "list /var/tmp (not in policy)" "ls /var/tmp"
check "write /etc/x (read-only)" "echo x > /etc/x && echo written"
check "read /etc/hostname (read-only)" "cat /etc/hostname"

say "processes and syscalls (seccomp, no privileges)"
check "unshare a user namespace" "unshare -U true && echo unshared"
check "raw packet socket" "python3 -c 'import socket; socket.socket(socket.AF_PACKET, socket.SOCK_RAW)' && echo opened"
check "mount a tmpfs" "mkdir -p /tmp/m && mount -t tmpfs none /tmp/m && echo mounted"
check "processes visible" "ps -eo pid,user,comm | head -n 6 | tr -s ' '"

say "credentials and network"
check "service account token" "ls /var/run/secrets/kubernetes.io/serviceaccount"
check "env: proxy or token variables" "env | grep -ciE 'proxy|token|secret' || true"
check "DNS for pypi.org" "getent hosts pypi.org || echo no-answer"
check "https://pypi.org" "curl -sS -m 5 -o /dev/null -w '%{http_code}' https://pypi.org/ || true"
check "instance metadata 169.254.169.254" "curl -sS -m 5 -o /dev/null -w '%{http_code}' http://169.254.169.254/latest/meta-data/ || true"
