"""Shared helpers: where the gateway is, how to call an alias, how to run az aigateway."""
import json
import os
import pathlib
import subprocess
import time

import requests

STATE = pathlib.Path(__file__).resolve().parent.parent / ".state"
RG = os.environ.get("RG", "rg-aigw-demo")
os.environ.setdefault("AZURE_CORE_ONLY_SHOW_ERRORS", "1")


def names():
    return dict(kv.split("=") for kv in (STATE / "names").read_text().split())


def gateway_name():
    return names()["GATEWAY"]


def host():
    return (STATE / "host").read_text().strip()


def key():
    return (STATE / "key").read_text().strip()


def models_url(path="chat/completions"):
    return f"https://{host()}/default/models/openai/v1/{path}"


def call(token=None, alias="chat", prompt="Say hello in five words.", timeout=90):
    """One chat call through the gateway. Returns (response, seconds)."""
    t0 = time.time()
    r = requests.post(
        models_url(),
        headers={"Authorization": f"Bearer {token or key()}"},
        json={"model": alias, "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout,
    )
    return r, time.time() - t0


def served_by(r):
    """Which Foundry deployment answered, from the response headers."""
    return r.headers.get("x-ratelimit-key") or "-"


def short(r, n=70):
    try:
        j = r.json()
        if r.status_code == 200:
            return j["choices"][0]["message"]["content"][:n].replace("\n", " ")
        return (j.get("message") or j.get("error", {}).get("message") or str(j))[:n]
    except ValueError:
        return r.text[:n].replace("\n", " ")


def az(*args, check=True, quiet=True):
    """Run an az command, return stdout. Output is not echoed unless quiet=False."""
    p = subprocess.run(["az", *args], capture_output=True, text=True)
    if check and p.returncode:
        raise SystemExit(f"az {' '.join(args[:3])} failed: {p.stderr.strip()[-400:]}")
    if not quiet:
        print(p.stdout)
    return p.stdout.strip()


def aig(*args, **kw):
    return az("aigateway", *args, "-g", RG, "--gateway-name", gateway_name(), **kw)


def model_id(provider, model):
    return json.loads((STATE / "models.json").read_text())[f"{provider}/{model}"]


API = "api-version=2026-11-01"


def alias_rules(max_retries, failures):
    """Set failure rules and the retry limit on the alias 'chat'. The CLI has no options for them yet,
    so this PATCHes the management API (Azure/aigateway lab 29 does the same)."""
    gw_id = az("aigateway", "show", "-g", RG, "-n", gateway_name(), "--query", "id", "-o", "tsv")
    url = f"https://management.azure.com{gw_id}/workspaces/default/aliases/chat?{API}"
    body = json.dumps({"properties": {"maxRetryAttempts": max_retries, "failures": failures}})
    az("rest", "--method", "patch", "--url", url, "--body", body, "-o", "none")
