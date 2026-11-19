"""Failover. Priority 0 is a backend that fails, priority 1 the healthy one. Four runs:
A: priority 0 answers 429, no failure rules. B: priority 0 is a host that does not exist, no failure rules.
C: the 429 case again, with a failure rule. D: the dead host again, with the rules the product docs recommend."""
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
common.aig("model-provider", "create", "-n", "dead", "--display-name", "Dead backend", "--kind", "Custom", "--custom",
           '{"authentication":{"kind":"ApiKey","apiKey":{"headerName":"Authorization","value":"Bearer unused"}},'
           '"endpoint":"https://dead-backend.invalid"}', "-o", "none")
common.aig("model", "create", "--provider-name", "dead", "-n", "dead-model", "--display-name", "Dead model",
           "--api-format", "OpenAIChatCompletions", "--supported-endpoints", "/v1/chat/completions",
           "--deployment-config", '{"modelName":"dead-model"}', "-o", "none")
DEAD = common.aig("model", "show", "--provider-name", "dead", "-n", "dead-model", "--query", "id", "-o", "tsv")

RULE_429 = {"when": "@cel(response != null && response.status_code == 429)", "excludeFor": "PT30S"}
RECOMMENDED = [  # the three rules the product docs suggest, in order
    {"when": "@cel(response == null || response.status_code == 408)", "retryAttempts": 1, "excludeFor": "PT60S"},
    RULE_429,
    {"when": "@cel(response != null && response.status_code in [500, 502, 503, 504])", "excludeFor": "PT30S"},
]


def alias(*backends):
    args = []
    for rid, prio in backends:
        args += ["--backend-model", f"resource-id={rid}", f"priority={prio}", "weight=100"]
    common.aig("model-alias", "set", "-n", "chat", "--routing-name", "chat", *args, "-o", "none")


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


def settle():
    time.sleep(25)  # let the change apply


alias((TINY, 0), (MINI, 1)); settle()
run(20, "A: priority 0 answers 429, no failure rules")

alias((DEAD, 0), (MINI, 1)); settle()
run(6, "B: priority 0 is a host that does not exist, no failure rules")

alias((TINY, 0), (MINI, 1))
common.alias_rules(3, [RULE_429]); settle()
run(20, "C: priority 0 answers 429, with a failure rule on 429")

alias((DEAD, 0), (MINI, 1))
common.alias_rules(3, RECOMMENDED); settle()
run(6, "D: the dead host, with the three recommended rules")
alias((MINI, 1))
