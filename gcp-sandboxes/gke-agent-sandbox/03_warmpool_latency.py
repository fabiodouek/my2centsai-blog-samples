#!/usr/bin/env python3
"""Test 3: warm-pool claim latency vs Google's "300 sandboxes/s, 90% in 200 ms".

Client side (this machine, through the SDK's kubectl port-forward to the Sandbox Router):
  - create_sandbox(warmpool=...)   claim created -> claim Ready
  - commands.run("echo ok")         first round trip through router to the sandbox
Server side: the controller's agent_sandbox_claim_controller_startup_latency_ms histogram
(port-forward to the controller's :8080), as a before/after difference per phase.

Phases:  pool2   20 sequential claims against a pool of 2 (waits for the refill between claims)
         pool10  20 sequential claims against a pool of 10
         cold    10 sequential claims against a pool of 0 (the controller creates a sandbox from the template)
         burst10 / burst20  that many concurrent claims against a pool of 10

Usage: python 03_warmpool_latency.py [phase ...]   (default: all)   results -> results/t3-<phase>.json
"""
import json, math, re, statistics, subprocess, sys, threading, time, urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxLocalTunnelConnectionConfig

NS = "default"
TEMPLATE = "bench-template"
RESULTS = Path(__file__).parent / "results"
RESULTS.mkdir(exist_ok=True)
METRIC = "agent_sandbox_claim_controller_startup_latency_ms"


def kubectl(*args, check=True):
    return subprocess.run(["kubectl", *args], capture_output=True, text=True, check=check).stdout.strip()


def ensure_template():
    kubectl("apply", "-f", str(Path(__file__).parent / "manifests" / "template-bench.yaml"))


def set_pool(name, replicas, timeout=600):
    """Create or resize a SandboxWarmPool and wait until readyReplicas == replicas."""
    manifest = {"apiVersion": "extensions.agents.x-k8s.io/v1beta1", "kind": "SandboxWarmPool",
                "metadata": {"name": name, "namespace": NS},
                "spec": {"replicas": replicas, "sandboxTemplateRef": {"name": TEMPLATE}}}
    subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(manifest), text=True, check=True, capture_output=True)
    wait_ready(name, replicas, timeout)


def wait_ready(name, replicas, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = json.loads(kubectl("get", "sandboxwarmpool", name, "-n", NS, "-o", "jsonpath={.status}", check=False) or "{}")
        # for 0, the pool must also have no replicas left, or "cold" claims could still adopt one
        if int(st.get("readyReplicas", 0)) == replicas and (replicas > 0 or int(st.get("replicas", 0)) == 0):
            return time.time() - t0
        time.sleep(0.5)
    raise TimeoutError(f"pool {name} not at {replicas} ready replicas")


class Metrics:
    """Scrapes the controller's histogram through a port-forward."""

    def __init__(self, port=18080):
        self.port = port
        self.proc = subprocess.Popen(["kubectl", "-n", "agent-sandbox-system", "port-forward",
                                      "svc/agent-sandbox-controller", f"{port}:8080"],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            try:
                self.scrape()
                return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("controller metrics port-forward did not start")

    def scrape(self):
        text = urllib.request.urlopen(f"http://127.0.0.1:{self.port}/metrics", timeout=5).read().decode()
        out = {}  # (launch_type, le) -> count, plus sums and counts
        for m in re.finditer(rf'^{METRIC}_(bucket|sum|count)\{{([^}}]*)\}} (\S+)$', text, re.M):
            kind, labels, val = m.groups()
            lab = dict(re.findall(r'(\w+)="([^"]*)"', labels))
            if lab.get("sandbox_template") != TEMPLATE:
                continue
            key = (lab["launch_type"], kind, lab.get("le"))
            out[key] = out.get(key, 0) + float(val)
        return out

    def close(self):
        self.proc.terminate()


def hist_quantiles(before, after, launch_type):
    """p50/p90/p99 (linear interpolation inside the bucket) of the observations added between two scrapes."""
    buckets = sorted(((float("inf") if k[2] == "+Inf" else float(k[2])), after.get(k, 0) - before.get(k, 0))
                     for k in after if k[0] == launch_type and k[1] == "bucket")
    count = after.get((launch_type, "count", None), 0) - before.get((launch_type, "count", None), 0)
    total = after.get((launch_type, "sum", None), 0) - before.get((launch_type, "sum", None), 0)
    if count <= 0:
        return {"count": 0}
    res = {"count": int(count), "mean_ms": round(total / count, 1)}
    for q in (0.5, 0.9, 0.99):
        target, prev_le, prev_c = q * count, 0.0, 0.0
        for le, c in buckets:
            if c >= target:
                if math.isinf(le):
                    res[f"p{int(q*100)}_ms"] = f">{prev_le:.0f}"
                else:
                    frac = (target - prev_c) / max(c - prev_c, 1e-9)
                    res[f"p{int(q*100)}_ms"] = round(prev_le + frac * (le - prev_le), 1)
                break
            prev_le, prev_c = le, c
    res["bucket_deltas"] = {("+Inf" if math.isinf(le) else int(le)): int(c) for le, c in buckets}
    return res


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1)]


def summary(values):
    return {"n": len(values), "min": round(min(values), 3), "p50": round(statistics.median(values), 3),
            "p90": round(pct(values, 90), 3), "max": round(max(values), 3), "mean": round(statistics.mean(values), 3)}


def one(client, pool, run_cmd=True):
    t0 = time.perf_counter()
    sb = client.create_sandbox(warmpool=pool, namespace=NS, sandbox_ready_timeout=300)
    t1 = time.perf_counter()
    out = None
    if run_cmd:
        out = sb.commands.run("echo ok").stdout.strip()
    t2 = time.perf_counter()
    return sb, {"create_s": t1 - t0, "exec_s": t2 - t1, "total_s": t2 - t0, "out": out}


def phase_sequential(client, metrics, name, pool, replicas, n):
    set_pool(pool, replicas)
    before = metrics.scrape()
    rows = []
    for i in range(n):
        if replicas:
            wait_ready(pool, replicas)  # the pool refills asynchronously; claiming a depleted pool is a different test
        sb, row = one(client, pool)
        row["i"] = i
        # was it served from the pool? ownerReferences of the Sandbox say who owns it while idle; the label says how it started
        rows.append(row)
        sb.terminate()
        print(f"{row['create_s']*1000:.0f}", end=" ", flush=True)  # create_sandbox in ms, one number per claim
    after = metrics.scrape()
    print("ms")
    res = {"phase": name, "pool": pool, "replicas": replicas, "rows": rows,
           "client_create_s": summary([r["create_s"] for r in rows]),
           "client_total_s": summary([r["total_s"] for r in rows]),
           "server_warm": hist_quantiles(before, after, "warm"), "server_cold": hist_quantiles(before, after, "cold")}
    return res


def phase_burst(client, metrics, name, pool, replicas, n):
    set_pool(pool, replicas)
    before = metrics.scrape()
    t0 = time.perf_counter()
    rows, handles = [], []

    def worker(i):
        s = time.perf_counter()
        sb, row = one(client, pool)
        row["i"] = i
        row["done_at_s"] = time.perf_counter() - t0
        return sb, row

    with ThreadPoolExecutor(max_workers=n) as ex:
        for sb, row in ex.map(worker, range(n)):
            handles.append(sb)
            rows.append(row)
    wall = time.perf_counter() - t0
    for sb in handles:
        try:
            sb.terminate()
        except Exception as e:  # keep going, the teardown script sweeps claims
            print("  terminate failed:", e)
    after = metrics.scrape()
    res = {"phase": name, "pool": pool, "replicas": replicas, "n": n, "wall_s": round(wall, 3), "rows": rows,
           "client_create_s": summary([r["create_s"] for r in rows]),
           "client_total_s": summary([r["total_s"] for r in rows]),
           "server_warm": hist_quantiles(before, after, "warm"), "server_cold": hist_quantiles(before, after, "cold")}
    print(f"  {name}: wall {wall:.2f}s, create p50 {res['client_create_s']['p50']:.2f}s max {res['client_create_s']['max']:.2f}s")
    return res


PHASES = {
    "pool2": lambda c, m: phase_sequential(c, m, "pool2", "bench-pool2", 2, 20),
    "pool10": lambda c, m: phase_sequential(c, m, "pool10", "bench-pool10", 10, 20),
    "cold": lambda c, m: phase_sequential(c, m, "cold", "bench-pool0", 0, 10),
    "burst10": lambda c, m: phase_burst(c, m, "burst10", "bench-pool10", 10, 10),
    "burst20": lambda c, m: phase_burst(c, m, "burst20", "bench-pool10", 10, 20),
}

if __name__ == "__main__":
    ensure_template()
    wanted = sys.argv[1:] or list(PHASES)
    metrics = Metrics()
    client = SandboxClient(connection_config=SandboxLocalTunnelConnectionConfig(), cleanup=True)
    try:
        for name in wanted:
            print(f"== {name}")
            res = PHASES[name](client, metrics)
            res["controller"] = re.search(r'git_version="([^"]+)"', urllib.request.urlopen(
                f"http://127.0.0.1:{metrics.port}/metrics").read().decode()).group(1)
            (RESULTS / f"t3-{name}.json").write_text(json.dumps(res, indent=1))
            c, w, k = res["client_create_s"], res["server_warm"], res["server_cold"]
            print(f"   client create_sandbox  p50 {c['p50']:.2f}s  p90 {c['p90']:.2f}s  max {c['max']:.2f}s   (first command included: p50 {res['client_total_s']['p50']:.2f}s)")
            for label, h in (("warm", w), ("cold", k)):
                if h.get("count"):
                    print(f"   controller {label:4s} claims {h['count']:>2}  p50 {h['p50_ms']} ms  p90 {h['p90_ms']} ms  mean {h['mean_ms']} ms")
            if "wall_s" in res:
                print(f"   all {res['n']} claims done in {res['wall_s']:.1f}s")
    finally:
        metrics.close()
