"""Services with a request body, and the 1 MiB limit: DynamoDB and Bedrock Converse through the SigV4 addon.

  uv run python 04_nons3_body.py

S3 clients declare the payload hash in a header, so the addon can sign while the request streams. Other
services (DynamoDB, Bedrock) sign a hash of the whole body, so the addon has to hold the body to hash it,
and it holds at most about 1 MiB. Past that it FAILS CLOSED: it kills the connection instead of forwarding a
request it cannot re-sign (an unsigned request, or one still carrying the carrier headers).

  1. DynamoDB PutItem and GetItem on osb-cred-test (AWS CLI), and Bedrock Converse on the pinned Haiku model.
  2. A DynamoDB BatchWriteItem of about 0.83 MiB works (three items of 290 KB; one item is capped at 400 KB,
     which is why a batch is used).
  3. The same call at 1.11 MiB (four items) is refused: the connection closes, the client sees no HTTP
     status and no AWS error, the addon logs `killed ... reason=body-may-stream-without-payload-hash`, and
     nothing reaches DynamoDB (a GetItem for those keys finds nothing).

Needs ./01_iam.sh, ./02_broker.sh and ./00_image.sh to have run.
"""

import uuid

from common import (
    MODEL_ID, Checks, cleanup, ddb_table, ensure_broker_forward, ensure_port_forward, error_code, heading, log_mark,
    log_since, new_sandbox, note, put_file, run, sh, show_log, sigv4_events,
)

SCRIPT = "04_nons3_body"
ITEM_CHARS = 290_000  # DynamoDB caps one item at 400 KB
BUILD = """
import json, sys
table, tag, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
items = [{"PutRequest": {"Item": {"pk": {"S": f"{tag}-{i}"}, "blob": {"S": "x" * %d}}}} for i in range(count)]
body = json.dumps({table: items})
open(f"/tmp/{tag}.json", "w").write(body)
print(f"{tag}: {count} items, request body {len(body):,} bytes ({len(body) / 1048576:.2f} MiB)")
""" % ITEM_CHARS

ensure_port_forward()
ensure_broker_forward()
table = ddb_table()
run_tag = uuid.uuid4().hex[:8]
checks = Checks()
try:
    heading("1. a sandbox with AWS credentials bound")
    sb, view = new_sandbox(script=SCRIPT)
    sandbox_id = view["sandbox_id"]
    put_file(sb, "/tmp/build.py", BUILD)

    heading("2. DynamoDB PutItem and GetItem (SigV4, small body)")
    key = f"item-{run_tag}"
    rc = run(sb, f"aws dynamodb put-item --table-name {table} --item '{{\"pk\":{{\"S\":\"{key}\"}},\"note\":{{\"S\":\"written through the addon\"}}}}'")
    checks.check("DynamoDB PutItem", rc == 0)
    got = sh(sb, f"aws dynamodb get-item --table-name {table} --key '{{\"pk\":{{\"S\":\"{key}\"}}}}' --query Item.note.S --output text")
    print(f"  $ aws dynamodb get-item --table-name {table} --key ... --query Item.note.S --output text\n      {got.text.strip()}")
    checks.check("DynamoDB GetItem returns what PutItem wrote", got.rc == 0 and got.text.strip() == "written through the addon")
    sh(sb, f"aws dynamodb delete-item --table-name {table} --key '{{\"pk\":{{\"S\":\"{key}\"}}}}'")

    heading("3. Bedrock Converse (SigV4) on the pinned Haiku model")
    prompt = '[{"role":"user","content":[{"text":"Reply with exactly one word: ready"}]}]'
    cmd = (f"aws bedrock-runtime converse --model-id {MODEL_ID} --messages '{prompt}' "
           "--inference-config '{\"maxTokens\":20}' --query output.message.content[0].text --output text")
    rc = run(sb, cmd, timeout=120, label=f"aws bedrock-runtime converse --model-id {MODEL_ID} --messages ... --query output.message.content[0].text")
    checks.check("Bedrock Converse returns a reply", rc == 0)

    heading("4. the 1 MiB limit on a non-S3 body")
    run(sb, f"python3 /tmp/build.py {table} under-{run_tag} 3", label="python3 /tmp/build.py  # 3 items of 290 KB")
    run(sb, f"python3 /tmp/build.py {table} over-{run_tag} 4", label="python3 /tmp/build.py  # 4 items of 290 KB")
    mark = log_mark(sandbox_id)
    rc = run(sb, f"aws dynamodb batch-write-item --request-items file:///tmp/under-{run_tag}.json",
             label="aws dynamodb batch-write-item --request-items file:///tmp/under.json   # 0.83 MiB", timeout=120)
    unprocessed = sh(sb, f"aws dynamodb batch-write-item --request-items file:///tmp/under-{run_tag}.json --query 'length(keys(UnprocessedItems))' --output text", timeout=120).text.strip()
    written = sh(sb, f"aws dynamodb get-item --table-name {table} --key '{{\"pk\":{{\"S\":\"under-{run_tag}-0\"}}}}' --query Item.pk.S --output text").text.strip()
    print(f"  $ aws dynamodb get-item ... under-0 --query Item.pk.S --output text\n      {written}")
    checks.check("a 0.83 MiB DynamoDB body is signed and forwarded", rc == 0)
    checks.check("DynamoDB processed every item (UnprocessedItems empty) and the first item can be read back",
                 unprocessed == "0" and written == f"under-{run_tag}-0", f"unprocessed key count {unprocessed}")

    mark = log_mark(sandbox_id)
    over = sh(sb, f"aws dynamodb batch-write-item --request-items file:///tmp/over-{run_tag}.json", timeout=240)
    print("  $ aws dynamodb batch-write-item --request-items file:///tmp/over.json   # 1.11 MiB")
    print(f"      exit code {over.rc}; client saw: {error_code(over.text)}")
    killed = [line for line in sigv4_events(log_since(sandbox_id, mark, "killed", wait=20))
              if "body-may-stream-without-payload-hash" in line and "service=dynamodb" in line]
    show_log(killed, limit=2)
    checks.check("the 1.11 MiB request fails on the client", over.rc != 0)
    checks.check("the client saw a closed connection, not an AWS answer or a 403", error_code(over.text) == "connection closed", error_code(over.text))
    checks.check("the sidecar log says killed ... reason=body-may-stream-without-payload-hash", len(killed) >= 1, f"{len(killed)} line(s)")
    seen = sh(sb, f"aws dynamodb get-item --table-name {table} --key '{{\"pk\":{{\"S\":\"over-{run_tag}-0\"}}}}' --query Item.pk.S --output text")
    print(f"  $ aws dynamodb get-item ... over-0 --query Item.pk.S --output text\n      {seen.text.strip()}")
    checks.check("nothing reached DynamoDB: the item is not there", seen.rc == 0 and seen.text.strip() == "None")
    note("the 0.83 MiB write is cleaned up below; the limit is a property of the addon (about 1 MiB), not of DynamoDB")

    heading("5. clean up")
    for index in range(3):
        sh(sb, f"aws dynamodb delete-item --table-name {table} --key '{{\"pk\":{{\"S\":\"under-{run_tag}-{index}\"}}}}'")
    left = sh(sb, f"aws dynamodb get-item --table-name {table} --key '{{\"pk\":{{\"S\":\"under-{run_tag}-0\"}}}}' --query Item.pk.S --output text")
    checks.check("test items deleted", left.text.strip() == "None")
finally:
    cleanup()
checks.finish()
