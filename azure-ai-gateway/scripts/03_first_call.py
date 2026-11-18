"""Call the alias through the gateway with the gateway key and print what came back."""
import argparse
import pathlib
import statistics
import time

import requests

STATE = pathlib.Path(__file__).resolve().parent.parent / ".state"


def gateway():
    host = (STATE / "host").read_text().strip()
    key = (STATE / "key").read_text().strip()
    return f"https://{host}/default/models/openai/v1/chat/completions", key


def call(url, token, alias="chat", prompt="Say hello in five words."):
    t0 = time.time()
    r = requests.post(
        url,
        headers={"Authorization": f"Bearer {token}"},
        json={"model": alias, "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    return r, time.time() - t0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--alias", default="chat")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    url, key = gateway()
    times, codes = [], {}
    for _ in range(a.repeat):
        r, dt = call(url, key, a.alias)
        times.append(dt)
        codes[r.status_code] = codes.get(r.status_code, 0) + 1
        if not a.quiet:
            print(r.status_code, f"{dt:.2f}s", r.text[:300])
            print({k: v for k, v in r.headers.items() if k.lower().startswith(("x-", "retry"))})
    print("status codes:", codes, "median latency:", f"{statistics.median(times):.2f}s")
