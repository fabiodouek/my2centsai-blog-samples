"""Who is the sandbox to AWS? The broker's role session, named after the sandbox, and no key in the sandbox.

  uv run python 02_identity.py

1. Ask the broker for a sandbox. It assumes the IAM role osb-sandbox with the sandbox ID as the session
   name, puts the temporary credentials in the sandbox's Credential Vault and gives the sandbox placeholder
   keys only.
2. `aws sts get-caller-identity` and boto3 (inside the sandbox) both answer with
   arn:aws:sts::<account>:assumed-role/osb-sandbox/<sandbox-id>.
3. `env | grep ^AWS_` inside the sandbox shows placeholders and nothing else: no temporary key, no session
   token, no account ID.

Needs ./01_iam.sh, ./02_broker.sh and ./00_image.sh to have run.
"""


from common import (
    PLACEHOLDER_ACCESS_KEY_ID, PLACEHOLDER_SECRET, TEMP_KEY_SHAPE, KEY_ID_SHAPE, CA_BUNDLE, Checks, account_id,
    aws_env, cleanup, ensure_broker_forward, ensure_port_forward, heading, new_sandbox, note, python_in, run, sh,
)

SCRIPT = "02_identity"
BOTO3 = """
import boto3
print(boto3.client("sts").get_caller_identity()["Arn"])
"""

ensure_port_forward()
ensure_broker_forward()
checks = Checks()
try:
    heading("1. the broker makes a sandbox and gives it credentials")
    sb, view = new_sandbox(script=SCRIPT)
    sandbox_id = view["sandbox_id"]
    expected = f"arn:aws:sts::{account_id()}:assumed-role/osb-sandbox/{sandbox_id}"

    heading("2. the sandbox's identity, from the AWS CLI and from boto3")
    cli_rc = run(sb, "aws sts get-caller-identity --query Arn --output text", label="aws sts get-caller-identity --query Arn --output text")
    cli = sh(sb, "aws sts get-caller-identity --query Arn --output text")
    py = python_in(sb, BOTO3, label="python3 -c 'print(boto3.client(\"sts\").get_caller_identity()[\"Arn\"])'")
    checks.check("the AWS CLI returns the assumed-role ARN with the sandbox ID as the session name", cli.rc == 0 and cli.text.strip() == expected.replace(account_id(), "123456789012"),
                 f"session name {cli.text.strip().rsplit('/', 1)[-1]}")
    checks.check("boto3 returns the same ARN", py.rc == 0 and py.text.strip() == expected.replace(account_id(), "123456789012"))
    session_name = cli.text.strip().rsplit("/", 1)[-1]
    print(f"\n  session name = {session_name}")
    print(f"  sandbox id   = {sandbox_id}")
    checks.check("session name equals the sandbox ID", session_name == sandbox_id)
    note("this is the name CloudTrail records for every call (05_cloudtrail.py)")

    heading("3. what the sandbox holds: placeholders only")
    run(sb, "env | grep '^AWS_' | sort")
    env = aws_env(sb)
    checks.check("AWS_ACCESS_KEY_ID is the placeholder", env.get("AWS_ACCESS_KEY_ID") == PLACEHOLDER_ACCESS_KEY_ID)
    checks.check("the placeholder does not match the access-key pattern the secret scan looks for",
                 not KEY_ID_SHAPE.search(env.get("AWS_ACCESS_KEY_ID", "")) and not KEY_ID_SHAPE.search(PLACEHOLDER_ACCESS_KEY_ID))
    checks.check("AWS_SECRET_ACCESS_KEY is the placeholder", env.get("AWS_SECRET_ACCESS_KEY") == PLACEHOLDER_SECRET)
    checks.check("no AWS_SESSION_TOKEN and no AWS_ACCOUNT_ID in the sandbox", "AWS_SESSION_TOKEN" not in env and "AWS_ACCOUNT_ID" not in env)
    checks.check("no variable holds a temporary key", not any(TEMP_KEY_SHAPE.search(v) for v in env.values()))
    checks.check("the proxy's CA bundle is set for the AWS CLI and boto3", env.get("AWS_CA_BUNDLE") == CA_BUNDLE)
    checks.check("the instance metadata service is switched off", env.get("AWS_EC2_METADATA_DISABLED") == "true")
    leftovers = sh(sb, "grep -rIlE 'ASIA[0-9A-Z]{16}' /root /home /tmp /etc 2>/dev/null | wc -l")
    checks.check("no temporary key in /root, /home, /tmp or /etc", leftovers.text.strip() == "0", f"{leftovers.text.strip()} file(s)")
    note("the real credentials live in the broker and in the egress sidecar's vault; the sidecar adds them to AWS requests")
finally:
    cleanup()
checks.finish()
