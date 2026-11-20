#!/usr/bin/env bash
# On the instance, as the agent user: what can a process inside an Enclave session reach?
# The probes run inside the sandbox with `enclave shell -- -c`, then the gateway's own log is read back.
set -uo pipefail
cd ~/project

cat > ~/project/probe.sh <<'PROBE'
probe() {  # name, command: print one line with the result
  local name="$1"; shift
  local out; out=$(timeout 8 bash -c "$*" 2>&1 | head -c 120 | tr '\n' ' '); local rc=${PIPESTATUS[0]}
  printf '%-34s %s\n' "$name" "${out:-<no answer, timed out or refused>}"
}
http() { curl -sS -m 6 -o /dev/null -w 'HTTP %{http_code}' "$1" 2>&1; }
probe "1  allowlisted site (pypi.org)"   "$(declare -f http); http https://pypi.org/simple/"
probe "2  site not on the list"          "$(declare -f http); http https://example.com"
probe "3  raw IP, no DNS (1.1.1.1:443)"  "$(declare -f http); http https://1.1.1.1"
probe "4  DNS name not on the list"      "getent hosts leak.attacker-example.net || echo 'does not resolve'"
probe "5  DNS straight to 8.8.8.8"       "python3 -c \"
import socket
s=socket.socket(2,2); s.settimeout(4)
try:
    s.sendto(bytes.fromhex('abcd01000001000000000000076578616d706c6503636f6d0000010001'),('8.8.8.8',53)); print('answered', len(s.recv(512)), 'bytes')
except Exception as e: print('blocked:', type(e).__name__)\""
probe "6  github.com port 22 (SSH banner)" "exec 3<>/dev/tcp/github.com/22 && head -n1 <&3 || echo blocked"
probe "7  github.com port 22 via IP only" "ip=\$(getent hosts github.com | cut -d' ' -f1); exec 3<>/dev/tcp/\$ip/22 && head -n1 <&3 || echo blocked"
probe "8  another host, port 22 (1.1.1.1)"  "exec 3<>/dev/tcp/1.1.1.1/22 && echo connected || echo blocked"
probe "9  metadata service (IMDSv2)"     "curl -sS -m 4 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token -w ' HTTP %{http_code}'"
PROBE
chmod +x ~/project/probe.sh

enclave network log --verdict deny --since 1s >/dev/null 2>&1   # touch the log
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "== probes inside the sandbox (restricted mode, the default)"
script -qec "enclave shell -- -c 'bash /home/agent/project/probe.sh'" /dev/null 2>&1 | grep -E '^[0-9]  ' 
echo
echo "== what the gateway logged since $START (denied)"
enclave network log --verdict deny --since "$START" 2>&1 | sort -u -k3,5 | head -12
echo "== events the gateway has for github.com in the same window"
n=$(enclave network log --domain github.com --since "$START" 2>&1 | grep -c '^ ' || true)
echo "$n events, although probes 6 and 7 opened SSH connections to github.com"
