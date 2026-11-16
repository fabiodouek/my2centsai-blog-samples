#!/usr/bin/env python3
"""Test 3, side check: why a warm claim plus a first command took about 2.7 s end to end.
Times create_sandbox, then four commands on the same sandbox, then the first command on a second sandbox.
Results: results/t3-first-command.txt"""
import importlib, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
t3 = importlib.import_module("03_warmpool_latency")
from k8s_agent_sandbox import SandboxClient
from k8s_agent_sandbox.models import SandboxLocalTunnelConnectionConfig

t3.ensure_template()
t3.set_pool("bench-pool2", 2)
c = SandboxClient(connection_config=SandboxLocalTunnelConnectionConfig(), cleanup=True)
lines = []
def log(*a):
    lines.append(" ".join(str(x) for x in a)); print(lines[-1])
t = time.perf_counter(); sb = c.create_sandbox(warmpool="bench-pool2", namespace="default"); log("create", round(time.perf_counter() - t, 3))
for i in range(4):
    t = time.perf_counter(); sb.commands.run("echo ok"); log("command", i, round(time.perf_counter() - t, 3))
t3.wait_ready("bench-pool2", 2)
sb2 = c.create_sandbox(warmpool="bench-pool2", namespace="default")
t = time.perf_counter(); sb2.commands.run("echo ok"); log("second sandbox, first command", round(time.perf_counter() - t, 3))
(Path(__file__).parent / "results" / "t3-first-command.txt").write_text("\n".join(lines) + "\n")
