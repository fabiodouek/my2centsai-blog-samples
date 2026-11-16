"""Test 2a: what does an AX suspend keep? Port of the EKS I8 + L4 probes, plus a background process that holds a
plain-HTTP keep-alive connection to www.google.com open (through the egress gateway) and logs every request it makes.
Five suspend/resume cycles on one task; after each, what is back. AX asks Substrate for a volumes-only snapshot."""
import base64, os
os.environ.setdefault("ATESPACE", "lifecycle")
from common import ATESPACE, ax, banner, create, delete, kate, sh, status, summary, timed

NAME = "keeps-ax"
CYCLES = 5
BOOT = ["sh", "-c", 'echo "boot $(date +%T) pid $$" >> /workspace/boots.log; exec sleep infinity']
TCP = r'''exec 3<>/dev/tcp/www.google.com/80
while true; do
  printf 'HEAD / HTTP/1.1\r\nHost: www.google.com\r\nConnection: keep-alive\r\n\r\n' >&3 \
    && IFS= read -r -t 5 -u 3 l && echo "$(date +%T) ${l%%$'\r'}" >> /workspace/tcp.log || echo "$(date +%T) failed" >> /workspace/tcp.log
  while IFS= read -r -t 1 -u 3 h; do [ "$h" = $'\r' ] && break; done
  sleep 2
done'''

banner("create the task, give it an egress rule for www.google.com:80, resume it")
create(NAME, BOOT)
kate("create", "egress-policy", NAME, "-f", "-", atespace=ATESPACE, quiet=True,
     input='rules:\n- http:\n    hostnames: ["www.google.com"]\n    ports:\n      numbers: [80]\n')
ax("resume", "task", NAME, quiet=True)

banner("write state: /tmp, /workspace (50 MB), a counter, a background sleep, a keep-alive connection")
print(sh(NAME, "echo tmp-ok > /tmp/marker; echo ws-ok > /workspace/marker; echo 0 > /workspace/counter; "
               "dd if=/dev/urandom of=/workspace/blob bs=1M count=50 2>/dev/null; sha256sum /workspace/blob > /workspace/blob.sha; "
               "setsid nohup sleep 3000 >/dev/null 2>&1 & "
               f"echo {base64.b64encode(TCP.encode()).decode()} | base64 -d > /workspace/keepalive.sh; "
               "setsid nohup bash /workspace/keepalive.sh >/dev/null 2>&1 & "
               "sleep 5; echo tcp.log: $(wc -l < /workspace/tcp.log) lines, last: $(tail -1 /workspace/tcp.log); cat /workspace/boots.log"))

susp, res = [], []
for c in range(1, CYCLES + 1):
    banner(f"cycle {c}")
    _, ms = timed("ax suspend", ax, "suspend", "task", NAME); susp.append(ms)
    _, ms = timed("ax resume ", ax, "resume", "task", NAME); res.append(ms)
    print(sh(NAME, "echo workspace-marker: $(cat /workspace/marker 2>&1); "
                   "echo blob: $(sha256sum -c /workspace/blob.sha 2>&1 | head -1); "
                   "echo tmp-marker: $(cat /tmp/marker 2>&1); "
                   "echo sleep3000-processes: $(ps -eo args | grep -cx 'sleep 3000'); "
                   "echo keepalive-processes: $(ps -eo args | grep -c '[d]ev/tcp'); "
                   "n1=$(wc -l < /workspace/tcp.log); sleep 5; n2=$(wc -l < /workspace/tcp.log); echo tcp.log-grew-in-5s: $((n2-n1)) lines; "
                   "echo uptime: $(cut -d. -f1 /proc/uptime) s; echo boot-lines: $(wc -l < /workspace/boots.log)"))
banner("results")
summary("suspend", susp)
summary("resume ", res)
print(sh(NAME, "cat /workspace/boots.log; tail -3 /workspace/tcp.log"))
delete(NAME)
