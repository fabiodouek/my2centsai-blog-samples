"""S3 from inside the sandbox, through the SigV4 addon: small, single-PUT and multipart uploads, a checksum round trip.

  uv run python 03_s3.py

Every request leaves the sandbox signed with the placeholder key. The egress sidecar adds the real
temporary credentials (as carrier headers), and the addon re-signs the request and strips the carriers.

  - `aws s3 ls` with no bucket is DENIED by design: the role has no s3:ListAllMyBuckets.
  - `aws s3 ls s3://example-bucket/` works.
  - a 19-byte object; a 5 MiB object (one PutObject, above the ~1 MiB streaming threshold: the CLI sends
    a declared payload hash, so the addon signs it as the headers arrive); a 20 MiB object (multipart).
  - the 20 MiB object is downloaded and its sha256 compared with the one taken before the upload.
  - boto3 put_object and list_objects_v2.

The bucket name is read from the secrets file and shown as example-bucket. Objects go under a prefix
named for the sandbox and are deleted at the end.
"""

import re
from collections import Counter

from common import (
    Checks, cleanup, ensure_broker_forward, ensure_port_forward, error_code, heading, log_mark, log_since, new_sandbox,
    note, python_in, run, sh, sigv4_events, test_bucket,
)

SCRIPT = "03_s3"
BOTO3 = """
import boto3
s3 = boto3.client("s3")
s3.put_object(Bucket="{bucket}", Key="{prefix}/boto3.txt", Body=b"written by boto3\\n")
listing = s3.list_objects_v2(Bucket="{bucket}", Prefix="{prefix}/")
keys = sorted(item["Key"].rsplit("/", 1)[-1] for item in listing.get("Contents", []))
print("put_object ok; list_objects_v2 sees:", ", ".join(keys))
"""

ensure_port_forward()
ensure_broker_forward()
bucket = test_bucket()
checks = Checks()
try:
    heading("1. a sandbox with AWS credentials bound")
    sb, view = new_sandbox(script=SCRIPT)
    prefix = f"osb-test/{view['sandbox_id']}"
    uri = f"s3://{bucket}/{prefix}"
    mark = log_mark(view["sandbox_id"])

    heading("2. listing")
    rc = run(sb, "aws s3 ls")
    denied = sh(sb, "aws s3 ls")
    checks.check("aws s3 ls without a bucket is denied (no s3:ListAllMyBuckets)", rc != 0 and error_code(denied.text) == "AccessDenied", error_code(denied.text))
    rc = run(sb, f"aws s3 ls s3://{bucket}/ | head -3")
    checks.check("aws s3 ls s3://example-bucket/ works", sh(sb, f"aws s3 ls s3://{bucket}/ >/dev/null").rc == 0)

    heading("3. uploads: 19 bytes, 5 MiB (one PutObject), 20 MiB (multipart)")
    sh(sb, "printf 'hello from a sandbox\\n' | head -c 19 > /tmp/small.txt; head -c 5242880 /dev/urandom > /tmp/five.bin; head -c 20971520 /dev/urandom > /tmp/twenty.bin")
    run(sb, "wc -c /tmp/small.txt /tmp/five.bin /tmp/twenty.bin")
    checks.check("19-byte upload", run(sb, f"aws s3 cp /tmp/small.txt {uri}/small.txt --only-show-errors") == 0)
    checks.check("5 MiB upload (single PutObject)", run(sb, f"aws s3 cp /tmp/five.bin {uri}/five.bin --only-show-errors", timeout=180) == 0)
    checks.check("20 MiB upload (multipart)", run(sb, f"aws s3 cp /tmp/twenty.bin {uri}/twenty.bin --only-show-errors", timeout=300) == 0)
    run(sb, f"aws s3 ls {uri}/")

    heading("4. download the 20 MiB object and compare checksums")
    before = sh(sb, "sha256sum /tmp/twenty.bin | cut -c1-64").text.strip()
    run(sb, f"aws s3 cp {uri}/twenty.bin /tmp/twenty.back --only-show-errors", timeout=300)
    after = sh(sb, "sha256sum /tmp/twenty.back | cut -c1-64").text.strip()
    print(f"  sha256 before upload  {before[:16]}...")
    print(f"  sha256 after download {after[:16]}...")
    checks.check("20 MiB round trip: the checksums match", bool(before) and before == after)

    heading("5. boto3 from inside the sandbox")
    out = python_in(sb, BOTO3.format(bucket=bucket, prefix=prefix), label="python3 (boto3 put_object + list_objects_v2)")
    checks.check("boto3 put_object and list_objects_v2", out.rc == 0 and "boto3.txt" in out.text)

    heading("6. what the sidecar did (its log, event and payload type only)")
    lines = sigv4_events(log_since(view["sandbox_id"], mark, "credential proxy: [sigv4]", wait=15))
    events = Counter(re.search(r"\[sigv4\] (\S+)", line).group(1) for line in lines if re.search(r"\[sigv4\] (\S+)", line))
    payloads = Counter(m.group(1) for line in lines if (m := re.search(r"payload=(\S+)", line)))
    print(f"  addon events: {dict(events)}")
    print(f"  payload types re-signed: {dict(payloads)}")
    checks.check("the addon re-signed the S3 requests", events.get("re-signed", 0) >= 6, f"{events.get('re-signed', 0)} re-signed")
    checks.check("the addon refused or killed none of them", not (events.get("refused") or events.get("killed")))
    note("S3 PutObject from the AWS CLI declares its payload hash up front, so the addon signs it without buffering the body")

    heading("7. clean up the objects")
    run(sb, f"aws s3 rm {uri}/ --recursive --only-show-errors")
finally:
    cleanup()
checks.finish()
