"""Test 8: AWS beyond Bedrock. Why S3 can't use a placeholder, and what works without AWS keys inside.

Bedrock accepts a bearer API key, which is just a header, so substitution works. Most AWS APIs use
SigV4: the client signs each request with the secret access key and never sends the key itself.
Daytona can only swap strings in headers, so a placeholder secret key produces a wrong signature.
The way out without credentials in the sandbox: presign the requests outside and hand over URLs.
"""

import os
import time

import boto3
from botocore.config import Config
from daytona import CreateSecretParams

from common import banner, create_sandbox, daytona, find_secret, require_env, run

require_env("AWS_TEST_BUCKET")
BUCKET = os.environ["AWS_TEST_BUCKET"]
REGION = os.environ.get("AWS_REGION", "us-east-1")
S3_HOSTS = [f"{BUCKET}.s3.amazonaws.com", f"{BUCKET}.s3.{REGION}.amazonaws.com", f"s3.{REGION}.amazonaws.com"]

s3 = boto3.client("s3", region_name=REGION, config=Config(signature_version="s3v4"))
banner(f"Test bucket {BUCKET}")
try:
    s3.head_bucket(Bucket=BUCKET)
    print("exists")
except s3.exceptions.ClientError:
    s3.create_bucket(Bucket=BUCKET)
    s3.put_bucket_tagging(Bucket=BUCKET, Tagging={"TagSet": [{"Key": "demo", "Value": "my2cents-credentials"}]})
    print("created")
s3.put_object(Bucket=BUCKET, Key="hello.txt", Body=b"hello from my2cents\n")

# Short-lived session credentials: the key ID and session token travel in headers, the secret key never does.
creds = boto3.client("sts").get_session_token(DurationSeconds=900)["Credentials"]
SECRETS = {"aws-secret-access-key": creds["SecretAccessKey"], "aws-session-token": creds["SessionToken"]}
for name, value in SECRETS.items():
    if old := find_secret(name):
        daytona.secret.delete(old.id)
    daytona.secret.create(CreateSecretParams(name=name, value=value, hosts=S3_HOSTS))

sbx = create_sandbox(
    "09",
    secrets={"AWS_SECRET_ACCESS_KEY": "aws-secret-access-key", "AWS_SESSION_TOKEN": "aws-session-token"},
    env_vars={"AWS_ACCESS_KEY_ID": creds["AccessKeyId"], "AWS_DEFAULT_REGION": REGION},
    user=None,  # throwaway secrets, named as they are
)
try:
    banner("A. boto3 in the sandbox with a placeholder secret key")
    run(sbx, "python3 -c 'import boto3' 2>/dev/null || pip install -q boto3", timeout=180)
    # Key IDs encode the AWS account number, so show only their prefix.
    print(run(sbx, "env | grep -E '^AWS_' | sed -E 's/^(AWS_ACCESS_KEY_ID=ASIA).*/\\1.../; s/^(AWS_S[A-Z_]+=.{24}).*/\\1.../' | sort"))
    print(run(sbx, f"""python3 -c "
import boto3
try:
    print(boto3.client('s3').get_object(Bucket='{BUCKET}', Key='hello.txt')['Body'].read())
except Exception as e:
    print(type(e).__name__, str(e)[:160])
" """, timeout=60))

    banner("B. Presigned URLs minted outside, used inside with curl")
    get_url = s3.generate_presigned_url("get_object", Params={"Bucket": BUCKET, "Key": "hello.txt"}, ExpiresIn=300)
    put_url = s3.generate_presigned_url("put_object", Params={"Bucket": BUCKET, "Key": "from-sandbox.txt"}, ExpiresIn=300)
    print("GET:", run(sbx, f"curl -s -w ' [%{{http_code}}]' '{get_url}'"))
    print("PUT:", run(sbx, f"date -u | curl -s -o /dev/null -w '[%{{http_code}}]' -X PUT -H 'Content-Type:' --data-binary @- '{put_url}'"))
    print("written by the sandbox:", s3.get_object(Bucket=BUCKET, Key="from-sandbox.txt")["Body"].read().decode().strip())
    print("PUT to a key the URL wasn't signed for:",
          run(sbx, f"echo x | curl -s -o /dev/null -w '[%{{http_code}}]' -X PUT -H 'Content-Type:' --data-binary @- '{put_url.replace('from-sandbox.txt', 'other.txt')}'"))
finally:
    sbx.delete()
    for name in SECRETS:
        if s := find_secret(name):
            daytona.secret.delete(s.id)
