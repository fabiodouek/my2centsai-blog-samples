#!/bin/sh
# Runs inside each workload (runc pod, templated Sandbox, direct Sandbox, microVM sandbox).
# Usage: probe.sh <other-pod-ip>
OTHER_IP="${1:-}"
echo "kernel:   $(uname -r)"
echo "dmesg:    $(dmesg 2>&1 | head -2 | tr '\n' '|')"
echo "virt:     $(systemd-detect-virt 2>/dev/null || echo n/a)"
python3 - "$OTHER_IP" <<'PY'
import socket, sys, urllib.request, time
other = sys.argv[1]
def http(url, headers=None, t=4):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        r = urllib.request.urlopen(req, timeout=t)
        return f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception as e:
        return type(e).__name__ + ": " + str(getattr(e, 'reason', e))[:40]
print("metadata token:", http("http://169.254.169.254/computeMetadata/v1/instance/service-accounts/default/token", {"Metadata-Flavor": "Google"}))
try:
    socket.setdefaulttimeout(4); print("cluster DNS:", socket.gethostbyname("kubernetes.default.svc.cluster.local"))
except Exception as e: print("cluster DNS:", type(e).__name__)
if other: print("pod-to-pod:", http(f"http://{other}:8888/"))
print("internet (google.com):", http("https://www.google.com", t=5))
PY
