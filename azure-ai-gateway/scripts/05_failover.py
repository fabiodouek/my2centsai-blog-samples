"""Failover. A: priority 0 is a deployment with a tiny rate limit (it answers 429), priority 1 the real one.
B: priority 0 is a host that does not exist."""
import time

import common

prov = common.names()["FOUNDRY"]
MINI = common.model_id(prov, "main")
# A deployment with a capacity of 1 has a tiny rate limit, so it soon answers 429.
common.az("cognitiveservices", "account", "deployment", "create", "-g", common.RG, "-n", prov,
          "--deployment-name", "capped", "--model-name", "gpt-4.1-mini", "--model-version", "2025-04-14",
          "--model-format", "OpenAI", "--sku-name", "GlobalStandard", "--sku-capacity", "1", "-o", "none")
fid = common.az("cognitiveservices", "account", "show", "-g", common.RG, "-n", prov, "--query", "id", "-o", "tsv")
common.aig("model-provider", "import", "--foundry-resource-id", fid, "-o", "none")
TINY = common.aig("model", "show", "--provider-name", prov, "-n", "capped", "--query", "id", "-o", "tsv")


def alias(*backends):
    args = []
    for rid, prio in backends:
        args += ["--backend-model", f"resource-id={rid}", f"priority={prio}", "weight=100"]
    common.aig("model-alias", "set", "-n", "chat", "--routing-name", "chat", *args, "-o", "none")
    time.sleep(25)  # let the change apply


def run(n, label):
    print(f"--- {label}")
    t0, counts = time.time(), {}
    for i in range(1, n + 1):
        r, dt = common.call(prompt="Reply with one word.")
        key = (r.status_code, common.served_by(r))
        counts[key] = counts.get(key, 0) + 1
        print(f"{time.time()-t0:5.1f}s  call {i:2d}  HTTP {r.status_code}  {dt:4.1f}s  {common.served_by(r):16s} {common.short(r, 50)}")
        time.sleep(1)
    print("summary:", {f"{c} {w}": k for (c, w), k in counts.items()})


alias((TINY, 0), (MINI, 1))
run(20, "A: priority 0 answers 429, priority 1 is healthy")

common.aig("model-provider", "create", "-n", "dead", "--display-name", "Dead backend", "--kind", "Custom", "--custom",
           '{"authentication":{"kind":"ApiKey","apiKey":{"headerName":"Authorization","value":"Bearer unused"}},'
           '"endpoint":"https://dead-backend.invalid"}', "-o", "none")
common.aig("model", "create", "--provider-name", "dead", "-n", "dead-model", "--display-name", "Dead model",
           "--api-format", "OpenAIChatCompletions", "--supported-endpoints", "/v1/chat/completions",
           "--deployment-config", '{"modelName":"dead-model"}', "-o", "none")
DEAD = common.aig("model", "show", "--provider-name", "dead", "-n", "dead-model", "--query", "id", "-o", "tsv")
alias((DEAD, 0), (MINI, 1))
run(6, "B: priority 0 is a host that does not exist")
alias((MINI, 1))
