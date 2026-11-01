"""API load for 05_control_plane.py: `level` parallel clients, each its own gRPC channel, cycling
through ListSandboxes (every page), GetSandbox and ExecSandbox (`true`) for `step_s` seconds.

05 imports run_level() to load the gateway from the laptop (through the port-forward), or runs
this file in the p6-load Pod (manifests/load-client.yaml) to load it from inside the cluster,
straight to the gateway's Service with the CLI's client certificate:

    python api_load.py '{"endpoint": "openshell.openshell.svc.cluster.local:8080", "tls_dir": "/etc/p6-client",
                         "level": 8, "step_s": 30, "ops": ["list", "get", "exec"], "targets": ["p6-cp01"]}'

Prints one JSON line: {"level", "t0", "t1", "calls": [[op, ms, ok, error], ...]}.
"""
import json
import random
import sys
import threading
import time

WORKSPACE = "default"


def call(c, op, target):
    if op == "list":
        c.list(workspace=WORKSPACE).all()
    elif op == "get":
        c.get(target, workspace=WORKSPACE)
    elif op == "exec":
        r = c.exec(target, ["true"], workspace=WORKSPACE, no_login_shell=True, timeout_seconds=60)
        if r.exit_code != 0:
            raise RuntimeError(f"exit {r.exit_code}")
    elif op == "health":
        c.health()
    else:
        raise ValueError(op)


def run_level(make_client, level, step_s, ops, targets):
    clients = [make_client() for _ in range(level)]
    calls, lock = [], threading.Lock()
    stop_at = time.monotonic() + step_s

    def worker(w):
        rnd, i, c = random.Random(w), 0, clients[w]
        while time.monotonic() < stop_at:
            op = ops[(w + i) % len(ops)]
            i += 1
            t = time.perf_counter()
            ok, err = True, ""
            try:
                call(c, op, rnd.choice(targets))
            except Exception as e:  # recorded per call
                ok, err = False, f"{type(e).__name__}: {e}"[:160].replace("\n", " ")
            with lock:
                calls.append([op, round((time.perf_counter() - t) * 1000, 3), ok, err])

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(level)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    t1 = time.time()
    for c in clients:
        c.close()
    return {"level": level, "t0": t0, "t1": t1, "calls": calls}


def health(make_client, n):
    c, out = make_client(), []
    for _ in range(n):
        t = time.perf_counter()
        c.health()
        out.append(round((time.perf_counter() - t) * 1000, 3))
    c.close()
    return out


if __name__ == "__main__":
    import pathlib

    from openshell import SandboxClient, TlsConfig

    cfg = json.loads(sys.argv[1])
    d = pathlib.Path(cfg["tls_dir"])
    tls = TlsConfig(ca_path=d / "ca.crt", cert_path=d / "tls.crt", key_path=d / "tls.key")

    def make():
        return SandboxClient(cfg["endpoint"], tls=tls, timeout=120)

    if cfg.get("health"):
        print(json.dumps({"health_ms": health(make, int(cfg["health"]))}))
    else:
        print(json.dumps(run_level(make, int(cfg["level"]), float(cfg["step_s"]), cfg["ops"], cfg["targets"])))
