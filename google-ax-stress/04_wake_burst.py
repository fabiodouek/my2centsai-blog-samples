"""Step 4: wake on request. Suspended tasks get an HTTP request through
Substrate's router, which resumes each one before forwarding. Timed by curl
inside a pod the router trusts, so no CLI or port-forward is in the number.

First, one task at a time at three CPU sizes (does the sandbox's CPU limit
change the wake?), then bursts of 1, 10 and 30 tasks woken at the same moment."""

import json

from common import (delete_names, delete_all, HERE, Sampler, ax, banner, create, describe, kubectl, parallel, run, save)

delete_all()  # start from an empty atespace

BURSTS = [1, 10, 30]
LIMITS = {"cpu": "250m", "memory": "256Mi"}
SIZES = ["50m", "250m", "1"]
CURL = ("curl -s -m 60 -o /dev/null -w '%{{http_code}} %{{time_total}}\\n' "
        "--cacert /run/servicedns-ca/trust-bundle.pem "
        "--cert /run/podidentity/credential-bundle.pem --key /run/podidentity/credential-bundle.pem "
        "-H 'ate-target-actor: stress/{name}' https://atenet-router.ate-system.svc/readyz")

banner("a client the router trusts (see ../google-ax-lifecycle/router-probe.yaml)")
kubectl("-n", "ax-system", "delete", "pod", "router-probe", "--ignore-not-found", "--wait=true", quiet=True)
kubectl("apply", "-f", str(HERE / "router-probe.yaml"))
kubectl("-n", "ax-system", "wait", "--for=condition=Ready", "pod/router-probe", "--timeout=120s")
run(["sleep", "5"])

def wake(names):
    """Fire one request per task at once from the probe pod; return ok times (ms) and codes."""
    script = " & ".join(CURL.format(name=nm) for nm in names) + " & wait"
    out = kubectl("-n", "ax-system", "exec", "router-probe", "--", "sh", "-c", script, quiet=True).stdout
    rows = [l.split() for l in out.splitlines() if l.strip()]
    return [float(t) * 1000 for code, t in rows if code == "200"], sorted({code for code, _ in rows})


results, by_cpu = {}, {}
with Sampler("wake"):
    for cpu in SIZES:
        banner(f"one task at cpu {cpu}, woken 5 times")
        nm = f"st-wc{cpu.replace('m', '')}"
        create(nm, cpu=cpu, memory="256Mi")
        ax("resume", "task", nm, quiet=True)
        ms = []
        for _ in range(5):
            ax("suspend", "task", nm, quiet=True)
            ms += wake([nm])[0]
        by_cpu[cpu] = {"ms": ms, "stats": describe(f"wake at cpu {cpu}", ms)}
        delete_names([nm])

    for n in BURSTS:
        banner(f"burst of {n}")
        names = [f"st-w{n}-{i:02d}" for i in range(n)]
        parallel(lambda nm: create(nm, **LIMITS), names, 10)
        parallel(lambda nm: ax("resume", "task", nm, quiet=True, check=False), names, 10)
        parallel(lambda nm: ax("suspend", "task", nm, quiet=True, check=False), names, 10)
        ok, codes = wake(names)
        results[n] = {"ms": ok, "codes": codes, "stats": describe(f"wake x{n}", ok)}
        print(f"  status codes: {codes}")
        delete_all(f"st-w{n}-")

kubectl("-n", "ax-system", "delete", "pod", "router-probe", "--wait=false", quiet=True)
save("wake", {"bursts": results, "by_cpu": by_cpu})
