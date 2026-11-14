"""T3: identity, metadata server and egress, per type x internet setting x destination."""
from sbx import *

rt = runtime_name()
R = {}

PROBE_PY = """import sys, urllib.request as u, urllib.error as ue, socket
try:
    r = u.urlopen(u.Request(sys.argv[1], headers={"Metadata-Flavor": "Google"}), timeout=5)
    print("HTTP", r.status)
except ue.HTTPError as e:
    print("HTTP", e.code, "(reached the server)")
except Exception as e:
    why = getattr(e, "reason", e)
    print("no DNS" if isinstance(why, socket.gaierror) else "timeout" if "timed out" in str(why) else "unreachable" if "unreachable" in str(why) else str(why)[:30])
"""
# the Shell image has no curl, so every probe is python3 probe.py URL
URLS = {
    "metadata server (169.254.169.254)": "http://169.254.169.254/computeMetadata/v1/",
    "www.google.com": "https://www.google.com",
    "storage.googleapis.com": "https://storage.googleapis.com",
    "8.8.8.8 (raw IP)": "https://8.8.8.8",
    "10.128.0.2 (RFC 1918)": "http://10.128.0.2/",
    "192.168.0.1 (RFC 1918)": "http://192.168.0.1/",
}
PROBES = {"metadata.google.internal (DNS)": "getent hosts metadata.google.internal || echo no-dns"}
PROBES.update({k: f"python3 /workspace/probe.py {v}" for k, v in URLS.items()})

print("== identity inside a Shell sandbox")
sb, _ = create_from_template("shell-net-off", ttl="600s", display_name="t3-identity")
r = sh(sb.name, "whoami; id; echo sudo: $(sudo -n true 2>&1 | head -1); "
                "echo env vars: $(env | wc -l), of which named like token/secret/key/cred: $(env | cut -d= -f1 | grep -ciE 'token|secret|key|cred'); "
                "echo KUBERNETES_* vars: $(env | grep -c ^KUBERNETES); echo curl: $(which curl || echo none); python3 -V")
print(r["stdout"], r["stderr"])
R["identity"] = r
client.sandboxes.delete(name=sb.name)

print("== egress matrix (Shell)")
matrix = {}
for tname in ("shell-net-off", "shell-net-on"):
    sb, _ = create_from_template(tname, ttl="600s", display_name=f"t3-{tname}")
    col = {}
    sh(sb.name, "cat > /workspace/probe.py <<'PY'\n" + PROBE_PY + "PY")
    for label, cmd in PROBES.items():
        r = sh(sb.name, cmd)
        col[label] = (r["stdout"] + r["stderr"]).strip()
    r = sh(sb.name, "pip install --retries 0 --timeout 5 cowsay 2>&1 | grep -E 'Successfully|ERROR' | head -1", timeout=60)
    col["pip install cowsay"] = (r["stdout"] + r["stderr"]).strip()[:48] or "(no output)"
    matrix[tname] = col
    client.sandboxes.delete(name=sb.name)

print("== control: Code Execution (no template, no egress setting)")
sb = client.sandboxes.create(name=rt, spec={"code_execution_environment": {"code_language": "LANGUAGE_PYTHON"}},
                             config={"ttl": "600s", "display_name": "t3-ce"}).response
code = "import json, socket, urllib.request as u, urllib.error as ue\nURLS = " + json.dumps(URLS) + """
def tag(url):
    try:
        r = u.urlopen(u.Request(url, headers={"Metadata-Flavor": "Google"}), timeout=5)
        return "HTTP %d" % r.status
    except ue.HTTPError as e:
        return "HTTP %d (reached the server)" % e.code
    except Exception as e:
        why = getattr(e, "reason", e)
        return "no DNS" if isinstance(why, socket.gaierror) else "timeout" if "timed out" in str(why) else "unreachable" if "unreachable" in str(why) else str(why)[:30]
out = {label: tag(url) for label, url in URLS.items()}
try:
    socket.gethostbyname("metadata.google.internal"); out["metadata.google.internal (DNS)"] = "resolves"
except OSError:
    out["metadata.google.internal (DNS)"] = "no DNS"
print(json.dumps(out))
"""
out, stderr, _ = run(sb.name, code)
ce = json.loads(out.strip().splitlines()[-1])
client.sandboxes.delete(name=sb.name)
matrix["code-execution"] = ce
R["matrix"] = matrix

print("\n== matrix")
labels = list(PROBES) + ["pip install cowsay"]
w = max(len(l) for l in labels) + 2
print(" " * w + "shell, off".ljust(24) + "shell, on".ljust(28) + "code execution")
for l in labels:
    a = matrix["shell-net-off"].get(l, "-")
    b = matrix["shell-net-on"].get(l, "-")
    c = matrix["code-execution"].get(l, "-")
    print(l.ljust(w) + a[:22].ljust(24) + b[:26].ljust(28) + c[:24])
save_result("t3", R)
