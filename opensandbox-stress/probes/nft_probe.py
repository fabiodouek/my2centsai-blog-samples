"""What the ~60 ms of a dns+nft lookup is made of.

Run from the stress directory, on a quiet cluster (no sandboxes; the server on the baseline):
    uv run python probes/nft_probe.py [--dry-run] [--n 30]

The egress sidecar (1.2.0) answers an allowed DNS lookup only after `nft -f -` has added, deleted
and re-added the answer's address in its dynamic allow set (components/egress/pkg/nftables/
dynamic.go:354-356, run by manager.go:381-385 under one mutex). The ladder measured ~60 ms per
lookup, in 10 ms steps, with little CPU. This probe times that nft run on its own, next to
controls that commit less, in the sidecar of one dns+nft sandbox:

  1. preconditions: a quiet cluster, the server on the baseline (runc, base egress image, dns+nft,
     gateway); kubectl pinned to --context osb-eks
  2. echo-stress up (manifests/echo-stress.yaml): its Service gives the allowed name an A record
  3. one sandbox (2 CPU / 2 GiB, the ladder's shape) with the deny-by-default policy allowing the
     echo host; metadata example=opensandbox-stress, script=nft_probe, run=<id>
  4. the kernel of the sandbox's node: uname -r, the CONFIG_HZ / NO_HZ / PREEMPT / RCU lines of its
     config, RCU's fqs parameters. Read by a short-lived debug pod (namespace default,
     --profile=general: the node's root mounted at /host, not privileged, only files are read;
     busybox pinned by digest, the VM sampler's image), found by its marker and deleted after
  5. in the sandbox's egress sidecar (kubectl exec; the sidecar's own python3 times each run with
     perf_counter around fork + exec + wait, the way the sidecar's Go runner waits for nft):
       list          nft list tables                  read-only: no batch, no commit
       noop          nft -f - holding only a comment  no commands (if nft accepts it)
       empty_commit  nft -f - "add table inet osb_probe" on the existing table: a batch that
                     changes nothing
       renew         nft -f - the add / delete / add-with-timeout of dynamic.go, element 192.0.2.1,
                     50 ms apart
       renew_b2b     the same renew, back to back (the ladder's lookups came ~2 ms apart)
       list_after    nft list tables again (drift check)
     N runs each (default 30) after 3 warm-ups; then 5 renews under strace -f -tt -T (only syscall
     names and durations are kept). Every write goes to a throwaway table inet osb_probe (one set,
     no chain, so no packet ever passes it) that the probe creates and deletes. The sidecar's own
     tables are never named: each script line must be one of the probe's six exact command forms
     (GUARD: one command per line, no `;`), which the sidecar checks again; the sidecar refuses to
     start if osb_probe already exists; the table list and the terse, stateless ruleset
     (`nft -s -t list ruleset`, osb_probe cut out) before and after must match
  6. in the sandbox: N getaddrinfo lookups of the echo host (allowed: one nft run each, as in the
     ladder) and N of a denied name (answered by the proxy without nft)
  7. finally: the probe table (if left), the debug pod, the sandbox and echo-stress are deleted; the
     server is read back (this script never changes it)

The reading at the end sets the series against these hypotheses:
  H1   the wait comes with committing real set changes: renew is slow, list and empty_commit are fast
  H1b  every commit waits, even one that changes nothing: renew and empty_commit are slow, list fast
  H2   starting nft costs it: every series is slow, noop included
  H3   nft's own userspace work: list is slow too, but noop (start, parse, no commands) is fast
  H4   nft is fast in the sidecar: the time is elsewhere (the Go caller, or the DNS path)

Output: this terminal and logs/probes/nft_probe.log, masked (no IP but the probe's 192.0.2.1, no
node name, no account ID, no token). Nothing goes to results/.

If the run is interrupted between `kubectl debug` and its deletes, the finished debug pod stays in the
default namespace (99_cleanup.py does not look there). Delete it by hand:
    kubectl --context osb-eks -n default get pods -o json | jq -r '.items[]
      | select(.metadata.name | startswith("node-debugger-"))
      | select(any((.spec.containers[0].command // []) + (.spec.containers[0].args // []) | .[]; . == "osb-nft-probe"))
      | .metadata.name' | xargs -r kubectl --context osb-eks -n default delete pod
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # the stress directory: common, checks

from checks import BASELINE, helm_problems, helm_state, server_problems  # noqa: E402
from common import (  # noqa: E402
    ECHO_HOST,
    KUBE_CONTEXT,
    LOGS,
    SANDBOX_NAMESPACE,
    VMM_IMAGE,
    Failures,
    Plan,
    cli,
    create_sandbox,
    delete_run,
    deny_policy,
    describe,
    echo_down,
    echo_up,
    heading,
    helm_history,
    kube_json,
    kubectl,
    new_run_id,
    node_alias,
    own_metadata,
    pct,
    preconditions,
    put_file,
    run_in,
    say,
    server_show,
    show,
    wait_no_pods,
    wait_until,
)

SCRIPT = "nft_probe"
CONTEXT = "osb-eks"
SHAPE = {"cpu": "2", "memory": "2Gi"}          # the ladder's sandbox shape
DEBUG_NAMESPACE = "default"                     # where the series' node debug pods run
MARKER = "osb-nft-probe"                        # $0 of the debug pod's shell: how its pod is found
DENIED_NAME = "osb-nft-probe-denied.invalid"    # not in the policy: the proxy answers NXDOMAIN itself
LOG_PATH = LOGS / "probes" / "nft_probe.log"
STRACE_RUNS = 5

# The probe's only nft writes. Table, set and element are the probe's own; 192.0.2.1 is TEST-NET-1
# (RFC 5737). The renew is dynamic.go:354-356's shape (element timeout 90 s: clampTTL of a 30 s TTL),
# the set is manager.go:268's dyn_allow_v4 definition.
PROBE_TABLE = "osb_probe"
PROBE_SET = "probe_v4"
PROBE_ELEMENT = "192.0.2.1"
SCRIPTS = {
    "setup": (f"add table inet {PROBE_TABLE}\n"
              f"add set inet {PROBE_TABLE} {PROBE_SET} {{ type ipv4_addr; timeout 360s; }}\n"),
    "renew": (f"add element inet {PROBE_TABLE} {PROBE_SET} {{ {PROBE_ELEMENT} }}\n"
              f"delete element inet {PROBE_TABLE} {PROBE_SET} {{ {PROBE_ELEMENT} }}\n"
              f"add element inet {PROBE_TABLE} {PROBE_SET} {{ {PROBE_ELEMENT} timeout 90s }}\n"),
    "empty_commit": f"add table inet {PROBE_TABLE}\n",
    "noop": "# osb-nft-probe: a comment and no commands\n",
    "teardown": f"delete table inet {PROBE_TABLE}\n",
}
# Every command line of a script must be exactly one of these forms (fullmatch: one command per
# line, nothing after it, so `;` cannot chain a second command). Comment lines (#...) are allowed.
GUARD_PATTERN = r"(?:add|delete) table inet osb_probe|add set inet osb_probe probe_v4 \{ type ipv4_addr; timeout \d+s; \}|(?:add|delete) element inet osb_probe probe_v4 \{ 192\.0\.2\.1(?: timeout \d+s)? \}"
GUARD = re.compile(GUARD_PATTERN)
MANUAL_DEBUG_CLEANUP = ("kubectl --context osb-eks -n default get pods -o json | jq -r '.items[] | select(.metadata.name "
                        "| startswith(\"node-debugger-\")) | select(any((.spec.containers[0].command // []) + "
                        "(.spec.containers[0].args // []) | .[]; . == \"osb-nft-probe\")) | .metadata.name' | xargs -r "
                        "kubectl --context osb-eks -n default delete pod")


def guarded(script: str) -> str:
    """The script, if every command line is one of the probe's own exact forms (else exit)."""
    for line in script.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not GUARD.fullmatch(line):
            sys.exit(f"refusing an nft line that is not the probe's own: {line[:80]}")
    return script


for _script in SCRIPTS.values():
    guarded(_script)

# Runs inside the egress sidecar (python3 -I -, the config as argv[1]). It checks the config again
# against its own copy of the guard, refuses to start when the probe's table already exists, and
# deletes the table in a finally. Prints one line: "@@probe <json>".
SIDECAR_PROBE = r'''
import json, os, re, shutil, subprocess, sys, time

TABLE = "osb_probe"
GUARD = re.compile(r"(?:add|delete) table inet osb_probe|add set inet osb_probe probe_v4 \{ type ipv4_addr; timeout \d+s; \}|(?:add|delete) element inet osb_probe probe_v4 \{ 192\.0\.2\.1(?: timeout \d+s)? \}")
cfg = json.loads(sys.argv[1])
N, PAUSE, STRACE_RUNS, S = int(cfg["n"]), float(cfg["pause"]), int(cfg["strace_runs"]), cfg["scripts"]
for text in S.values():
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and not GUARD.fullmatch(line):
            sys.exit("refusing an nft line that is not the probe's own")
NFT = shutil.which("nft") or "/usr/sbin/nft"
result = {"method": "in-sidecar python3: time.perf_counter around subprocess.run (fork + exec + wait)",
          "python": sys.version.split()[0], "kernel": os.uname().release, "euid": os.geteuid(), "blocks": {}}

def timed(argv, stdin=None):
    t0 = time.perf_counter()
    done = subprocess.run(argv, input=stdin if stdin is not None else "", capture_output=True, text=True)
    return (time.perf_counter() - t0) * 1000.0, done.returncode, (done.stderr or done.stdout or "").strip()[:200]

def nft_file(name):
    return [NFT, "-f", "-"], S[name]

def tables():
    done = subprocess.run([NFT, "list", "tables"], capture_output=True, text=True, input="")
    if done.returncode != 0:
        raise SystemExit("nft list tables failed: " + (done.stderr or "").strip()[:200])
    return sorted(l.strip()[len("table "):] for l in done.stdout.splitlines() if l.strip().startswith("table "))

def ruleset():
    """`nft -s -t list ruleset` (stateless: no counters; terse: no set elements) without the probe's
    table, as lines; None when it cannot be listed."""
    done = subprocess.run([NFT, "-s", "-t", "list", "ruleset"], capture_output=True, text=True, input="")
    if done.returncode != 0:
        result["ruleset_error"] = (done.stderr or "").strip()[:200]
        return None
    out, skip = [], False
    for line in done.stdout.splitlines():
        if line.startswith("table ") and line.split()[2:3] == [TABLE]:
            skip = True
        if not skip:
            out.append(line)
        elif line == "}":
            skip = False
    return out

def block(name, argv, stdin=None, pause=None, warm=3):
    pause = PAUSE if pause is None else pause
    for _ in range(warm):
        timed(argv, stdin)
        time.sleep(pause)
    ms, errors = [], []
    for _ in range(N):
        t, rc, err = timed(argv, stdin)
        if rc == 0:
            ms.append(round(t, 3))
        else:
            errors.append(err)
        time.sleep(pause)
    result["blocks"][name] = {"ms": ms, "failed": len(errors), "error": errors[0] if errors else "",
                              "pause_ms": round(pause * 1000)}

STAMP = re.compile(r"^(?:\d+\s+)?(\d\d):(\d\d):(\d\d\.\d+) (.*)$")
CALL = re.compile(r"^(?:<\.\.\. )?(\w+)(?:\(| resumed>)")
DUR = re.compile(r"<(\d+\.\d+)>$")

def parse_trace(path):
    first = exit_call = exited = None
    calls = []
    with open(path, errors="replace") as trace:
        for raw in trace:
            m = STAMP.match(raw.rstrip("\n"))
            if not m:
                continue
            ts = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            rest = m.group(4)
            if first is None:
                first = ts
            if rest.startswith("+++ exited"):
                exited = ts
                continue
            if rest.startswith("exit_group("):
                exit_call = ts
            c, d = CALL.match(rest), DUR.search(rest)
            if c and d:
                calls.append((float(d.group(1)) * 1000.0, c.group(1)))
    calls.sort(reverse=True)
    return {"slowest": [[name, round(ms, 3)] for ms, name in calls[:4]], "syscalls": len(calls),
            "exit_gap_ms": round((exited - exit_call) * 1000, 3) if exited is not None and exit_call is not None else None,
            "traced_ms": round((exited - first) * 1000, 3) if exited is not None and first is not None else None}

version = subprocess.run([NFT, "--version"], capture_output=True, text=True, input="")
result["nft_version"] = (version.stdout or version.stderr).strip()[:80]
before = tables()
result["tables_before"] = before
rules_before = ruleset()
result["ruleset_lines_before"] = len(rules_before) if rules_before is not None else None
if "inet " + TABLE in before:
    result["error"] = "inet osb_probe already exists: left alone, nothing measured"
    result["preexisting"] = True
    print("@@probe " + json.dumps(result), flush=True)
    sys.exit(3)
created = False
try:
    t, rc, err = timed(*nft_file("setup"))
    if rc != 0:
        result["error"] = "setup failed: " + err
        raise SystemExit(4)
    created = True
    block("list", [NFT, "list", "tables"])
    t, rc, err = timed(*nft_file("noop"))
    if rc == 0:
        block("noop", *nft_file("noop"))
    else:
        result["blocks"]["noop"] = {"ms": [], "failed": 0, "error": "not accepted: " + err, "pause_ms": 0}
    block("empty_commit", *nft_file("empty_commit"))
    block("renew", *nft_file("renew"))
    block("renew_b2b", *nft_file("renew"), pause=0.0)
    shown = subprocess.run([NFT, "list", "set", "inet", TABLE, "probe_v4"], capture_output=True, text=True, input="")
    m = re.search(r"timeout (\S+) expires (\S+)", shown.stdout)
    result["element_after_renew"] = {"timeout": m.group(1), "expires": m.group(2)} if m else (shown.stdout or shown.stderr).strip()[-120:]
    block("list_after", [NFT, "list", "tables"])
    strace = shutil.which("strace")
    traces = []
    if not strace:
        result["strace"] = "not installed"
    else:
        for i in range(STRACE_RUNS):
            path = "/tmp/osb_nft_probe_strace.%d" % i
            t, rc, err = timed([strace, "-f", "-tt", "-T", "-o", path, NFT, "-f", "-"], S["renew"])
            entry = {"wall_ms": round(t, 3)}
            if rc == 0 and os.path.exists(path):
                entry.update(parse_trace(path))
            else:
                entry["error"] = err
            try:
                os.remove(path)
            except OSError:
                pass
            traces.append(entry)
            time.sleep(PAUSE)
        result["strace"] = traces
finally:
    if created:
        t, rc, err = timed(*nft_file("teardown"))
        result["teardown"] = {"rc": rc, "error": err}
    try:
        after = tables()
    except SystemExit as error:
        after = None
        result["tables_error"] = str(error)
    result["tables_after"] = after or []
    result["tables_unchanged"] = after == before
    rules_after = ruleset()
    result["ruleset_lines_after"] = len(rules_after) if rules_after is not None else None
    result["ruleset_unchanged"] = rules_before == rules_after if rules_before is not None and rules_after is not None else None
    if result["ruleset_unchanged"] is False:
        import difflib
        result["ruleset_diff"] = [l for l in difflib.unified_diff(rules_before, rules_after, lineterm="", n=0)
                                  if l[:1] in "+-" and not l.startswith(("+++", "---"))][:12]
    print("@@probe " + json.dumps(result), flush=True)
'''

if f'GUARD = re.compile(r"{GUARD_PATTERN}")' not in SIDECAR_PROBE:
    sys.exit("the sidecar's copy of the guard differs from GUARD_PATTERN: refusing")

# Runs in the sandbox (python3 -I <file> <host> <denied name> <n>). Prints "@@lookup <json>".
LOOKUP = r'''
import json, socket, sys, time
host, denied, n = sys.argv[1], sys.argv[2], int(sys.argv[3])

def series(name, warm=3):
    for _ in range(warm):
        try:
            socket.getaddrinfo(name, 80, socket.AF_INET, socket.SOCK_STREAM)
        except socket.gaierror:
            pass
    ms, failed = [], 0
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            socket.getaddrinfo(name, 80, socket.AF_INET, socket.SOCK_STREAM)
            ok = True
        except socket.gaierror:
            ok = False
        took = round((time.perf_counter() - t0) * 1000, 3)
        if ok or name != host:
            ms.append(took)
        else:
            failed += 1
    return {"ms": ms, "failed": failed}

print("@@lookup " + json.dumps({"allowed": series(host), "denied": series(denied)}), flush=True)
'''

# Runs in the debug pod's busybox: files only (the node's root is at /host).
KERNEL = r'''
echo "kernel $(uname -r)"
echo "cpus $(nproc 2>/dev/null)"
config="/host/boot/config-$(uname -r)"
pattern='^CONFIG_HZ|^CONFIG_NO_HZ|^CONFIG_PREEMPT|^CONFIG_RCU'
if [ -r "$config" ]; then echo "config /boot/config-$(uname -r)"; grep -E "$pattern" "$config"
elif [ -r /proc/config.gz ]; then echo "config /proc/config.gz"; zcat /proc/config.gz | grep -E "$pattern"
else echo "config none found"; fi
for p in rcutree/parameters/jiffies_till_first_fqs rcutree/parameters/jiffies_till_next_fqs \
         rcutree/parameters/jiffies_till_sched_qs rcupdate/parameters/rcu_expedited \
         rcupdate/parameters/rcu_normal; do
  if [ -r "/sys/module/$p" ]; then echo "param ${p##*/} $(cat "/sys/module/$p")"; fi
done
'''


# --- output ----------------------------------------------------------------------------------------

class _Tee:
    """Writes to the terminal and the log file."""

    def __init__(self, stream, file):
        self.stream, self.file = stream, file

    def write(self, text):
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self):
        self.stream.flush()
        self.file.flush()

    def isatty(self):
        return False


def tee_to_log() -> None:
    """Copy stdout and stderr to logs/probes/nft_probe.log, unless stdout already is that file (a
    driver's `step nft_probe ...` redirects there itself)."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        if LOG_PATH.exists() and os.path.samestat(os.fstat(1), os.stat(LOG_PATH)):
            return
    except OSError:
        pass
    out = LOG_PATH.open("w")
    sys.stdout = _Tee(sys.stdout, out)
    sys.stderr = _Tee(sys.stderr, out)


def histogram(ms: list[float], width: float = 10.0, cap: float = 200.0) -> str:
    """Counts per 10 ms bin from 0 to the maximum (everything at or above `cap` in one bin)."""
    if not ms:
        return "-"
    top = int(min(max(ms), cap) // width)
    counts = [0] * (top + 1)
    for v in ms:
        counts[min(int(v // width), top)] += 1
    labels = [f"{int(i * width)}-{int((i + 1) * width)}" for i in range(top + 1)]
    if max(ms) >= cap:
        labels[-1] = f"{int(top * width)}+"
    return " ".join(f"{label}:{count}" for label, count in zip(labels, counts))


def report(label: str, ms: list[float], failed: int = 0, note: str = "") -> None:
    d = describe(label, ms)
    if not d["n"]:
        print(f"  {label:<13} no successful runs{(': ' + show(note)) if note else ''}"
              + (f" ({failed} failed)" if failed else ""))
        return
    print(f"  {label:<13} n {d['n']:>2}  min {d['min']:7.1f}  p50 {d['p50']:7.1f}  p95 {d['p95']:7.1f}  "
          f"max {d['max']:7.1f}  mean {d['mean']:7.1f} ms" + (f"  ({failed} failed: {show(note)[:80]})" if failed else ""))
    print(f"  {'':<13} 10 ms bins  {histogram(ms)}")
    print(f"  {'':<13} sorted      {' '.join(f'{v:.1f}' for v in sorted(ms))}", flush=True)


def p50(ms: list[float]) -> float | None:
    return pct(ms, 50) if ms else None


# --- the cluster -----------------------------------------------------------------------------------

def kexec(pod: str, container: str, *argv: str, stdin: str | None = None, timeout: float = 300) -> tuple[int, str, str]:
    """kubectl exec into a container of the sandbox's pod (--context pinned). stdin is always given
    (an empty one when None), so kubectl never reads this terminal."""
    try:
        done = subprocess.run(["kubectl", "--context", KUBE_CONTEXT, "-n", SANDBOX_NAMESPACE, "exec", "-i", pod,
                               "-c", container, "--", *argv], input=stdin if stdin is not None else "",
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", f"kubectl exec did not finish within {timeout:.0f} s"
    return done.returncode, done.stdout, done.stderr


def sandbox_pod(sb) -> tuple[str, str, str]:
    """(pod name, node name, egress sidecar container name) of the sandbox."""
    pods = kube_json("-n", SANDBOX_NAMESPACE, "get", "pods", "-l", f"opensandbox.io/id={sb.id}").get("items", [])
    if len(pods) != 1:
        raise RuntimeError(f"expected one pod for the sandbox, found {len(pods)}")
    spec = pods[0]["spec"]
    names = [c["name"] for c in spec.get("containers", [])] + [c["name"] for c in spec.get("initContainers", [])]
    egress = next((n for n in names if "egress" in n), None)
    if not egress:
        raise RuntimeError(f"the sandbox's pod has no egress sidecar (containers: {', '.join(names)})")
    return pods[0]["metadata"]["name"], spec.get("nodeName", ""), egress


def debug_pods(node: str | None = None) -> list[str]:
    """This script's debug pods (node-debugger-*, MARKER in the command), on `node` or any node."""
    found = []
    for pod in kube_json("-n", DEBUG_NAMESPACE, "get", "pods", check=False).get("items", []):
        name = pod["metadata"]["name"]
        container = (pod["spec"].get("containers") or [{}])[0]
        if not name.startswith("node-debugger-") or MARKER not in (container.get("command") or []) + (container.get("args") or []):
            continue
        if node is None or pod["spec"].get("nodeName") == node:
            found.append(name)
    return found


_IPV6 = re.compile(r"(?<![\w:])(?:(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}|[0-9a-fA-F:]*::[0-9a-fA-F:]*)"
                   r"(?:/\d{1,3})?(?![\w:])")


def mask_ipv6(text: str) -> str:
    """show() masks IPv4 only: also hide IPv6 addresses (a ruleset line can carry one)."""
    return _IPV6.sub("<ip6>", text)


def masked_node(text: str, node: str) -> str:
    return show(text.replace(node, node_alias(node)) if node else text)


def kernel_info(node: str) -> dict:
    """The node's kernel from a short-lived debug pod; the pod is deleted before returning."""
    try:
        done = subprocess.run(["kubectl", "--context", KUBE_CONTEXT, "debug", "-n", DEBUG_NAMESPACE, f"node/{node}",
                               "--quiet", "--profile=general", f"--image={VMM_IMAGE}", "--", "sh", "-c", KERNEL, MARKER],
                              capture_output=True, text=True, timeout=120, input="")
    except subprocess.TimeoutExpired:
        raise RuntimeError("kubectl debug did not finish within 120 s") from None
    if done.returncode != 0:
        noise = "a DNS label is recommended"
        why = " ".join(line for line in (done.stderr or done.stdout).splitlines() if noise not in line)
        raise RuntimeError(f"kubectl debug failed: {masked_node(why, node)[:200]}")
    pods: list[str] = []

    def created() -> bool:
        pods[:] = debug_pods(node)
        return bool(pods)

    wait_until(created, timeout=30, every=1)
    if not pods:
        raise RuntimeError("kubectl debug ran, but no debug pod with this script's marker was found")
    pod = pods[0]
    phase = ""

    def finished() -> bool:
        nonlocal phase
        phase = kubectl("-n", DEBUG_NAMESPACE, "get", "pod", pod, "-o", "jsonpath={.status.phase}", check=False).strip()
        return phase in ("Succeeded", "Failed")

    try:
        wait_until(finished, timeout=150, every=2)
        text = kubectl("-n", DEBUG_NAMESPACE, "logs", pod, check=False)
    finally:
        kubectl("-n", DEBUG_NAMESPACE, "delete", "pod", pod, "--ignore-not-found", "--wait=false", check=False)
    if phase != "Succeeded":
        raise RuntimeError(f"the debug pod ended {phase or 'without a status'}: {masked_node(text, node)[:200]}")
    info: dict = {"config": {}, "params": {}}
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("kernel "):
            info["kernel"] = line.split(None, 1)[1] if " " in line else ""
        elif line.startswith("cpus "):
            info["cpus"] = line.split(None, 1)[1] if " " in line else ""
        elif line.startswith("config "):
            info["config_source"] = line.split(None, 1)[1]
        elif line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            info["config"][key] = value
        elif line.startswith("param "):
            parts = line.split()
            if len(parts) >= 3:
                info["params"][parts[1]] = parts[2]
    hz = info["config"].get("CONFIG_HZ", "")
    info["hz"] = int(hz) if hz.isdigit() else None
    return info


def sidecar_probe(pod: str, container: str, n: int) -> dict:
    """The nft timings from inside the egress sidecar (python3 there), or from outside when the
    sidecar has no python3 (each run timed around its kubectl exec, minus an exec of `true`)."""
    rc, out, err = kexec(pod, container, "python3", "-c", "print('py-ok')", timeout=60)
    if rc == 0 and "py-ok" in out:
        config = json.dumps({"n": n, "pause": 0.05, "strace_runs": STRACE_RUNS, "scripts": SCRIPTS})
        rc, out, err = kexec(pod, container, "python3", "-I", "-", config, stdin=SIDECAR_PROBE, timeout=420)
        line = next((ln for ln in reversed(out.splitlines()) if ln.startswith("@@probe ")), "")
        if not line:
            raise RuntimeError(f"the sidecar probe printed no result (exit {rc}): {show((err or out).strip()[-300:])}")
        result = json.loads(line[len("@@probe "):])
        result["exit"] = rc
        return result
    return outside_probe(pod, container, n)


def outside_probe(pod: str, container: str, n: int) -> dict:
    """Fallback without python3 in the sidecar: kubectl exec round trips, the control `true`."""
    def round_trip(*argv: str, stdin: str | None = None) -> tuple[float, int]:
        t0 = time.perf_counter()
        code, _, _ = kexec(pod, container, *argv, stdin=stdin, timeout=60)
        return (time.perf_counter() - t0) * 1000.0, code

    def listed() -> list[str]:
        code, text, _ = kexec(pod, container, "nft", "list", "tables", timeout=60)
        if code != 0:
            raise RuntimeError("nft list tables failed in the sidecar")
        return sorted(ln.strip()[len("table "):] for ln in text.splitlines() if ln.strip().startswith("table "))

    def rules() -> list[str] | None:
        code, text, _ = kexec(pod, container, "nft", "-s", "-t", "list", "ruleset", timeout=60)
        if code != 0:
            return None
        out, skip = [], False
        for line in text.splitlines():
            if line.startswith("table ") and line.split()[2:3] == [PROBE_TABLE]:
                skip = True
            if not skip:
                out.append(line)
            elif line == "}":
                skip = False
        return out

    result: dict = {"method": "outside: each run timed around its kubectl exec round trip; subtract the "
                              "`true` control", "blocks": {}}
    before = listed()
    result["tables_before"] = before
    rules_before = rules()
    if f"inet {PROBE_TABLE}" in before:
        result["error"] = "inet osb_probe already exists: left alone, nothing measured"
        result["preexisting"] = True
        result["tables_after"], result["tables_unchanged"] = before, True
        return result
    created = False
    try:
        if round_trip("nft", "-f", "-", stdin=guarded(SCRIPTS["setup"]))[1] != 0:
            result["error"] = "setup failed"
            return result
        created = True
        for name, argv, stdin in (("true", ("true",), None), ("list", ("nft", "list", "tables"), None),
                                  ("renew", ("nft", "-f", "-"), guarded(SCRIPTS["renew"]))):
            ms, failed = [], 0
            for _ in range(n):
                took, code = round_trip(*argv, stdin=stdin)
                if code == 0:
                    ms.append(round(took, 3))
                else:
                    failed += 1
            result["blocks"][name] = {"ms": ms, "failed": failed, "error": "", "pause_ms": 0}
    finally:
        if created or f"inet {PROBE_TABLE}" in listed():
            result["teardown"] = {"rc": round_trip("nft", "-f", "-", stdin=guarded(SCRIPTS["teardown"]))[1], "error": ""}
        after = listed()
        result["tables_after"], result["tables_unchanged"] = after, after == before
        rules_after = rules()
        result["ruleset_unchanged"] = (rules_before == rules_after
                                       if rules_before is not None and rules_after is not None else None)
        result["ruleset_lines_before"] = len(rules_before) if rules_before is not None else None
        result["ruleset_lines_after"] = len(rules_after) if rules_after is not None else None
    return result


def lookups(sb, n: int) -> dict:
    put_file(sb, "/tmp/osb_nft_lookup.py", LOOKUP)
    out = run_in(sb, f"python3 -I /tmp/osb_nft_lookup.py {ECHO_HOST} {DENIED_NAME} {n}", timeout=180)
    line = next((ln for ln in reversed(out.text.splitlines()) if ln.startswith("@@lookup ")), "")
    if out.rc != 0 or not line:
        raise RuntimeError(f"the lookup series failed (exit {out.rc}): {show(out.text[-300:])}")
    return json.loads(line[len("@@lookup "):])


# --- the reading -----------------------------------------------------------------------------------

def reading(probe: dict, kernel: dict | None, looked: dict | None) -> list[str]:
    """Which hypothesis (H1 to H4, in the module docstring) the numbers fit: a first reading, to
    check against the numbers themselves."""
    blocks = probe.get("blocks", {})
    med = {name: p50(b.get("ms") or []) for name, b in blocks.items()}

    def slow(name: str) -> bool:
        return med.get(name) is not None and med[name] >= 20

    def fast(name: str) -> bool:
        return med.get(name) is not None and med[name] < 10

    lines = []
    if probe.get("method", "").startswith("outside"):
        base = med.get("true")
        lines.append(f"timed from outside: subtract the `true` control (p50 {base:.1f} ms) from each p50"
                     if base is not None else "timed from outside, and the control failed: no reading")
    elif slow("renew") and fast("list") and fast("empty_commit"):
        lines.append("renew waits while the read-only list and the empty commit do not: the wait comes with "
                     "committing real changes (H1)")
    elif slow("renew") and slow("empty_commit") and fast("list"):
        lines.append("every commit waits, even one that changes nothing: the wait is in batch processing, not in "
                     "the element changes (H1b)")
    elif slow("renew") and slow("list"):
        lines.append("even the read-only list is slow: process start or nft's own userspace work, not the commit "
                     "(H2, or H3 if noop is fast)")
    elif fast("renew") and fast("renew_b2b"):
        lines.append("nft is fast in the sidecar: the ~60 ms is not in nft itself (H4): look at the Go caller or "
                     "the DNS path")
    else:
        lines.append("no clean split between the series: read the numbers")
    if med.get("renew") is not None and med.get("renew_b2b") is not None:
        lines.append(f"renew 50 ms apart p50 {med['renew']:.1f} ms, back to back p50 {med['renew_b2b']:.1f} ms"
                     + (": spacing matters (leftover work from the previous commit)"
                        if med["renew_b2b"] > 1.5 * med["renew"] + 5 else ": spacing does not matter"))
    hz = (kernel or {}).get("hz")
    if hz:
        tick = 1000 / hz
        fqs = (kernel or {}).get("params", {}).get("jiffies_till_first_fqs")
        lines.append(f"CONFIG_HZ={hz}: one tick is {tick:g} ms; the ladder's steps were 10 ms "
                     f"({'the same' if abs(tick - 10) < 0.5 else 'not the same'})"
                     + (f"; RCU's first force-quiescent-state scan after {fqs} tick(s) = {int(fqs) * tick:g} ms"
                        if fqs and fqs.isdigit() else ""))
    traces = [t for t in probe.get("strace") or [] if isinstance(t, dict) and t.get("slowest")]
    if traces:
        top = [t["slowest"][0] for t in traces]
        names = sorted({name for name, _ in top})
        gaps = [t["exit_gap_ms"] for t in traces if t.get("exit_gap_ms") is not None]
        lines.append(f"under strace the slowest syscall of each renew: {', '.join(f'{n} {ms:.1f} ms' for n, ms in top)}"
                     + (f"; exit_group to exit: p50 {pct(gaps, 50):.1f} ms" if gaps else ""))
        # A netlink batch is processed by the kernel inside the sender's sendmsg (commit included),
        # so a waiting commit shows as a long sendmsg; the acknowledgements are then already queued.
        where = {"sendmsg": "inside the batch send: the kernel runs the batch, commit included, in sendmsg (the "
                            "commit path)",
                 "sendto": "inside the batch send (sendto): the commit path",
                 "recvmsg": "in a receive: a netlink dump or the batch's answer (cache fetch or the commit's reply)",
                 "recvfrom": "in a receive: a netlink dump or the batch's answer (cache fetch or the commit's reply)",
                 "close": "in close(): the release of nft's netlink socket",
                 "execve": "in execve: process start"}
        top_p50 = pct([ms for _, ms in top], 50)
        if gaps and pct(gaps, 50) >= 10:
            lines.append("the time sits after exit_group: the release of nft's netlink socket at exit")
        elif top_p50 < 10:
            lines.append("no single syscall holds the time (the slowest is under 10 ms): it is spread over the run")
        elif len(names) == 1 and names[0] in where:
            lines.append(f"the time sits {where[names[0]]}")
        else:
            lines.append(f"the slowest syscall differs between runs ({', '.join(names)}): read the traces")
    if looked:
        allowed, denied = p50(looked["allowed"]["ms"]), p50(looked["denied"]["ms"])
        if allowed is not None and denied is not None and med.get("renew_b2b") is not None:
            lines.append(f"lookup of the echo host p50 {allowed:.1f} ms, of a denied name {denied:.1f} ms: "
                         f"{allowed - denied:.1f} ms for the forward and the nft run, against renew_b2b p50 "
                         f"{med['renew_b2b']:.1f} ms in the sidecar")
    return lines


# --- main ------------------------------------------------------------------------------------------

def arguments(parser) -> None:
    parser.add_argument("--n", type=int, default=30, help="runs per series (default 30)")


def plan(n: int) -> Plan:
    p = Plan(f"nft_probe.py: what the ~60 ms of a dns+nft lookup is made of ({n} runs per series)")
    p.checks += [f"kubectl context {CONTEXT}",
                 "preconditions: no sandboxes, no Pools, none of this directory's objects; server runc, base egress "
                 "image, dns+nft, gateway; 2 nodes Ready"]
    p.step("echo-stress up (manifests/echo-stress.yaml): the allowed name's A record", 0.5)
    p.step("one sandbox, 2 CPU / 2 GiB, deny by default and allow the echo host; metadata "
           "example=opensandbox-stress, script=nft_probe", 0.5)
    p.step("its node's kernel from a debug pod (namespace default, --profile=general, busybox by digest, files "
           "only): uname -r, CONFIG_HZ / NO_HZ / PREEMPT / RCU, RCU fqs parameters; the pod deleted", 0.4)
    p.step(f"egress sidecar, throwaway table inet {PROBE_TABLE} (one set, no chain; element {PROBE_ELEMENT}): "
           f"{n} runs each of list, noop, empty_commit, renew, renew_b2b, list_after; {STRACE_RUNS} renews "
           "under strace; the table deleted and the table list compared", 0.6)
    p.step(f"sandbox: {n} getaddrinfo lookups of the echo host, {n} of a denied name", 0.2)
    p.step("finally: the probe table (if left), the debug pod, the sandbox, echo-stress deleted; the server "
           "read back", 0.8)
    p.restores.append("nothing on the server (never changed); the cluster objects above are deleted")
    p.writes.append("logs/probes/nft_probe.log (masked); nothing in results/")
    return p


def main() -> int:
    args = cli(__doc__, arguments)
    if KUBE_CONTEXT != CONTEXT:
        sys.exit(f"the kubectl context is {KUBE_CONTEXT!r}, not {CONTEXT}: refusing")
    if not 5 <= args.n <= 200:
        sys.exit("--n must be within 5..200")
    the_plan = plan(args.n)
    if args.dry_run:
        the_plan.print()
        return 0
    tee_to_log()
    the_plan.print()
    failures = Failures()
    preconditions()
    run_id = new_run_id()
    meta = own_metadata(SCRIPT, run_id)
    sb = None
    pod = container = node = ""
    probe: dict = {}
    kernel: dict | None = None
    looked: dict | None = None
    try:
        heading("echo-stress")
        echo_up()
        say(f"echo-stress ready at {ECHO_HOST}")

        heading("the sandbox")
        sb = create_sandbox(meta, resource=SHAPE, policy=deny_policy(ECHO_HOST), ttl_minutes=15)
        pod, node, container = sandbox_pod(sb)
        say(f"sandbox up on {node_alias(node)}; egress sidecar container {container}")

        heading(f"1. the kernel of {node_alias(node)}")
        try:
            kernel = kernel_info(node)
            print(f"  kernel {show(kernel.get('kernel', '?'))}, {kernel.get('cpus', '?')} CPUs, config from "
                  f"{show(kernel.get('config_source', '?'))}")
            for key, value in sorted(kernel["config"].items()):
                print(f"    {key}={show(value)}")
            for key, value in sorted(kernel["params"].items()):
                print(f"    {key} = {show(value)}")
            failures.check(kernel.get("hz") is not None, f"CONFIG_HZ read: {kernel.get('hz')}")
        except Exception as error:  # noqa: BLE001 - the rest of the probe still runs
            failures.add(f"kernel: {masked_node(str(error), node)}")

        heading(f"2. nft in the egress sidecar (table inet {PROBE_TABLE}, element {PROBE_ELEMENT})")
        probe = sidecar_probe(pod, container, args.n)
        print(f"  method: {probe.get('method')}")
        print(f"  sidecar: python {probe.get('python', '?')}, {show(probe.get('nft_version', '?'))}, kernel "
              f"{show(probe.get('kernel', '?'))}, euid {probe.get('euid', '?')}")
        if probe.get("error"):
            failures.add(f"sidecar probe: {probe['error']}")
        for name in ("true", "list", "noop", "empty_commit", "renew", "renew_b2b", "list_after"):
            block = probe.get("blocks", {}).get(name)
            if block is not None:
                report(name, block.get("ms") or [], block.get("failed", 0), block.get("error", ""))
        if probe.get("element_after_renew"):
            print(f"  the probe element after the renews: {show(json.dumps(probe['element_after_renew']))}")
        traces = probe.get("strace")
        if isinstance(traces, list):
            print(f"  strace -f -tt -T of {len(traces)} renews (syscall names and durations only):")
            for i, t in enumerate(traces, 1):
                if t.get("error"):
                    print(f"    {i}. wall {t.get('wall_ms', 0):.1f} ms, strace failed: {show(t['error'])[:120]}")
                    continue
                slowest = ", ".join(f"{name} {ms:.2f}" for name, ms in t.get("slowest", []))
                print(f"    {i}. wall {t['wall_ms']:.1f} ms, traced {t.get('traced_ms')} ms, {t.get('syscalls')} "
                      f"syscalls; slowest (ms): {slowest}; exit_group to exit {t.get('exit_gap_ms')} ms")
        elif traces:
            print(f"  strace: {show(str(traces))}")
        others_before = [t for t in probe.get("tables_before", []) if t != f"inet {PROBE_TABLE}"]
        print(f"  tables before: {show(', '.join(probe.get('tables_before', [])))}")
        print(f"  tables after:  {show(', '.join(probe.get('tables_after', [])))}")
        failures.check(probe.get("tables_unchanged") is True and f"inet {PROBE_TABLE}" not in probe.get("tables_after", []),
                       f"the sidecar's table list is unchanged ({len(others_before)} tables) and inet {PROBE_TABLE} "
                       "is gone")
        for line in probe.get("ruleset_diff") or []:
            print(f"    ruleset {mask_ipv6(show(line))[:160]}")
        if probe.get("ruleset_error"):
            print(f"    nft -s -t list ruleset failed: {mask_ipv6(show(probe['ruleset_error']))}")
        failures.check(probe.get("ruleset_unchanged") is True,
                       f"the sidecar's ruleset (nft -s -t list ruleset, osb_probe cut out) is unchanged: "
                       f"{probe.get('ruleset_lines_before')} lines before, {probe.get('ruleset_lines_after')} after")

        heading("3. lookups from the sandbox (getaddrinfo, AF_INET, one after another)")
        looked = lookups(sb, args.n)
        report("echo host", looked["allowed"]["ms"], looked["allowed"]["failed"], "lookup failed")
        report("denied name", looked["denied"]["ms"])
        failures.check(looked["allowed"]["failed"] == 0, f"all {args.n} lookups of the echo host answered")

        heading("reading (a first reading of the numbers above; the hypotheses are in this file's docstring)")
        for line in reading(probe, kernel, looked):
            print(f"  - {show(line)}")
    except Exception as error:  # noqa: BLE001 - reported; the clean-up below still runs
        failures.add(f"{type(error).__name__}: {masked_node(str(error), node)}")
    finally:
        heading("clean up")
        if pod and container and not probe.get("preexisting") and probe.get("tables_unchanged") is not True:
            code, _, _ = kexec(pod, container, "nft", "-f", "-", stdin=guarded(SCRIPTS["teardown"]), timeout=60)
            print(f"  probe table delete from outside: exit {code} (1: it was already gone)")
        try:
            for name in debug_pods():
                kubectl("-n", DEBUG_NAMESPACE, "delete", "pod", name, "--ignore-not-found", "--wait=false", check=False)
            gone = wait_until(lambda: not debug_pods(), timeout=90, every=3)
            left = [] if gone else debug_pods()
            failures.check(not left, "no debug pod of this script is left" + (f" ({len(left)} still there)" if left else ""))
            if left:
                print(f"  delete it by hand: {MANUAL_DEBUG_CLEANUP}")
        except Exception as error:  # noqa: BLE001
            failures.add(f"debug pod clean up: {masked_node(str(error), node)}")
        if sb is not None:
            try:
                sb.close()
            except Exception:  # noqa: BLE001 - the delete below is what matters
                pass
        delete_run(run_id)
        failures.check(wait_no_pods(f"run={run_id}", timeout=300), "the sandbox's pod is gone")
        echo_down()
        say("echo-stress deleted")
        try:
            state = server_show()
            problems = server_problems(state, BASELINE) + helm_problems(helm_state(helm_history()))
            failures.check(not problems, f"server read back (not changed): {json.dumps(BASELINE)}"
                           + (f"; {'; '.join(problems)}" if problems else ""))
        except Exception as error:  # noqa: BLE001
            failures.add(f"server read-back: {error}")
    return failures.report()


if __name__ == "__main__":
    sys.exit(main())
