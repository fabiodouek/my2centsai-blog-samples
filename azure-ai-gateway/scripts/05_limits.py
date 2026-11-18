"""Apply a token limit, then a cost limit, and send a burst through the alias."""
import pathlib
import subprocess

import requests

from importlib import import_module

STATE = pathlib.Path(__file__).resolve().parent.parent / ".state"
first = import_module("03_first_call")


def az(*args):
    subprocess.run(["az", "aigateway", *args], check=True)


def burst(n, label):
    url, key = first.gateway()
    print(f"--- {label}")
    for i in range(1, n + 1):
        r, dt = first.call(url, key)
        extra = {k: v for k, v in r.headers.items() if k.lower().startswith(("x-alias-cost", "retry"))}
        print(i, r.status_code, f"{dt:.2f}s", r.text[:80].replace("\n", " "), extra)


if __name__ == "__main__":
    gw = (STATE / "gateway").read_text().strip()
    rg = "rg-aigw-demo"
    # Token limit: 300 tokens a minute on the whole workspace, counted after each response.
    az("policy", "set", "-g", rg, "--gateway", gw, "--token-limit", "300", "--token-period", "minute",
       "--token-counter-key", "Identity")
    burst(12, "token limit 300/min")
    az("policy", "remove", "-g", rg, "--gateway", gw, "--token-limit")
    # Cost limit: $0.001 an hour. Check flags with: az aigateway policy set-cost-limit --help
    az("policy", "set-cost-limit", "-g", rg, "--gateway", gw, "--amount", "0.001", "--period", "hour")
    burst(12, "cost limit $0.001/hour")
