"""Deletes every file this API key's project holds in the Files API, and prints
the token and cost totals the scripts recorded.

Files uploaded through the Files API expire on their own after 48 hours; this
just does it now. The API key itself is deleted in AI Studio or with gcloud
(see the README).
"""

import json

from common import SPEND, banner, sdk_client

client = sdk_client()
banner("Files API")
files = list(client.files.list())
if not files:
    print("no files")
for f in files:
    client.files.delete(name=f.name)
    print(f"deleted {f.name} ({f.mime_type}, {f.size_bytes} bytes)")
print(f"files left: {len(list(client.files.list()))}")

banner("what the scripts sent (from results/spend.json)")
if SPEND.exists():
    spend = json.loads(SPEND.read_text())
    for model, s in spend.items():
        print(f"{model:<24} requests={s['requests']:<6} tokens={s['input_tokens']:<12,} ${s['usd']:.4f}")
    print(f"{'total':<24} ${sum(s['usd'] for s in spend.values()):.4f}")
