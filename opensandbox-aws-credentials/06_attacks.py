"""Try to get a credential out of the sandbox, or to use one the sandbox should not be able to use.

  uv run python 06_attacks.py

Every attack runs INSIDE a sandbox that holds placeholder keys only. The pass condition is the same for all
of them: no real credential comes back. The script prints status codes, error codes and counts, never a
response body.

  A  credential vending   sts:AssumeRole, GetSessionToken, GetFederationToken (the addon refuses them with a 403
                          and the reason credential-vending; the role and session policy Deny the same actions,
                          proved by the broker's `deny-check` in ./02_broker.sh), and ecr get-login-password
                          (the ECR host is not on the sandbox's allow list at all).
  B  presigned URL        a URL presigned with the placeholder key. The addon forwards query-authenticated
                          requests unchanged (it strips the carriers, never signs them), so S3 rejects the
                          placeholder: 403.
  C  Host spoofing        the request's Host header or the dialed address disagrees with the TLS SNI (#1758).
                          1. SNI sts, Host s3 (another host the vault serves): the vault adds s3's carriers, the
                             addon sees Host != SNI and kills the connection (host-sni-mismatch).
                          2. A control: a plain request for the bound host logs `applied binding`.
                          3. SNI sts, Host outside AWS: no binding matches, so nothing is added; the request goes
                             to the SNI host and its answer is STS's own.
                          4. SNI sts, but the connection is dialed to an S3 address (--resolve): the addon pins the
                             upstream to the SNI name, so STS answers (not S3, and no TLS failure) and the binding
                             applied is the SNI host's.
                          5. No SNI at all (an IP URL): the sidecar passes a ClientHello without SNI through
                             undecrypted, so no binding and no addon ever sees it, and nothing can be added.
  D  metadata services    the EC2 instance metadata service (169.254.169.254) and the Pod Identity agent
                          (169.254.170.23) are unreachable, and the container-credentials variables are unset.
  E  a search             /proc/*/environ and the files under /root, /home, /tmp, /etc, /var and /opt are searched for
                          a temporary key (ASIA...) or a session token; the count must be 0.
  F  the sidecar's API    the egress sidecar's API on 127.0.0.1:18080 refuses a caller without its token, so the
                          vault cannot be read from inside the sandbox.
  G  bearer on SigV4      a Bearer Authorization header on a SigV4-bound host is refused (bearer-on-sigv4-binding).

Needs ./01_iam.sh, ./02_broker.sh and ./00_image.sh to have run.
"""

import re
import time

from common import (
    Checks, cleanup, ensure_broker_forward, ensure_port_forward, error_code, heading, log_mark, log_since, new_sandbox,
    note, python_in, sandbox_role_arn, sh, show_log, sigv4_events, test_bucket,
)

SCRIPT = "06_attacks"
PRESIGN = """
import boto3
print(boto3.client("s3").generate_presigned_url("get_object", Params={"Bucket": "%s", "Key": "osb-test/never-written.txt"}, ExpiresIn=300))
"""
# Counts only, never the matches.
FILE_SCAN = "grep -rIlE 'ASIA[0-9A-Z]{16}|IQoJb3JpZ2lu|FwoGZXIvYXdz|x-osb-aws-' /root /home /tmp /etc /var /opt 2>/dev/null | wc -l"
PROC_SCAN = "cat /proc/[0-9]*/environ 2>/dev/null | tr '\\0' '\\n' | grep -cE 'ASIA[0-9A-Z]{16}|IQoJb3JpZ2lu|FwoGZXIvYXdz|x-osb-aws-'"

ensure_port_forward()
ensure_broker_forward()
bucket = test_bucket()
checks = Checks()
STS_ID = re.compile(r"x-amz-sts-extended-request-id", re.I)
try:
    heading("0. a sandbox with AWS credentials bound")
    sb, view = new_sandbox(script=SCRIPT)
    sid = view["sandbox_id"]
    role = sandbox_role_arn()

    heading("A. credential-vending calls from inside the sandbox")
    vending = [
        ("assume-role", f"aws sts assume-role --role-arn {role} --role-session-name escape"),
        ("get-session-token", "aws sts get-session-token"),
        ("get-federation-token", "aws sts get-federation-token --name escape"),
    ]
    for name, command in vending:
        mark = log_mark(sid)
        out = sh(sb, command, timeout=120)
        code = error_code(out.text)
        refused = [l for l in sigv4_events(log_since(sid, mark, "reason=credential-vending", wait=10)) if "refused 403" in l]
        reason = re.search(r"reason=(\S+)", refused[0]).group(1) if refused else "-"
        print(f"  $ {command.replace(role, 'arn:aws:iam::123456789012:role/osb-sandbox')}\n      exit code {out.rc}; "
              f"client saw: {code} (the CLI cannot parse the addon's 403); the addon's reason: {reason}")
        show_log(refused, limit=1)
        leaked = bool(re.search(r'"(AccessKeyId|SecretAccessKey|SessionToken)"', out.text))
        checks.check(f"sts {name}: refused, no credential returned", out.rc != 0 and not leaked and len(refused) >= 1, f"{len(refused)} log line(s)")
    out = sh(sb, "aws ecr get-login-password --region us-east-1", timeout=90)
    print(f"  $ aws ecr get-login-password --region us-east-1\n      exit code {out.rc}; client saw: {error_code(out.text)}; output {len(out.text)} characters")
    checks.check("ecr get-login-password: no token (the ECR host is not on the allow list)", out.rc != 0 and len(out.text) < 400)
    note("the Deny in the role and in every session policy is a second layer behind the addon; ./02_broker.sh's deny-check shows AWS enforcing it")

    heading("B. a presigned URL made with the placeholder key")
    url = python_in(sb, PRESIGN % bucket, label="python3 (boto3 generate_presigned_url, no network call)").text.strip().splitlines()
    url = url[-1] if url else ""
    mark = log_mark(sid)
    status = sh(sb, f"curl -s -m 20 -o /tmp/presigned.out -w '%{{http_code}}' '{url}'", timeout=60)
    print(f"  $ curl <presigned URL>\n      HTTP {status.text.strip()[-3:]}")
    body_has_key = sh(sb, "grep -cE 'ASIA[0-9A-Z]{16}' /tmp/presigned.out").text.strip()
    pres_lines = [l for l in sigv4_events(log_since(sid, mark, "presigned", wait=10)) if "reason=query-auth-forwarded-unchanged" in l]
    show_log(pres_lines, limit=1)
    checks.check("presigned URL with the placeholder key is rejected by S3 (403)", status.text.strip()[-3:] == "403", f"HTTP {status.text.strip()[-3:]}")
    checks.check("the addon forwarded it unchanged and logged presigned ... query-auth-forwarded-unchanged", len(pres_lines) >= 1)
    checks.check("the response holds no temporary key", body_has_key == "0")

    heading("C. Host / SNI spoofing (#1758)")
    ips = sh(sb, "getent hosts sts.us-east-1.amazonaws.com | cut -d' ' -f1 | head -1").text.strip()
    checks.check("an allowed AWS IP resolved for the spoof tests", bool(ips))
    head = "curl -sS -m 20 -D - -o /dev/null"
    # 1. Host names another AWS host the vault serves: killed.
    mark = log_mark(sid)
    out = sh(sb, f"{head} https://sts.us-east-1.amazonaws.com/ -H 'Host: s3.us-east-1.amazonaws.com'", timeout=60)
    killed = [l for l in sigv4_events(log_since(sid, mark, "host-sni-mismatch", wait=10)) if "killed" in l]
    print(f"  SNI sts, Host s3 (another bound host):\n      curl exit {out.rc}, no HTTP answer")
    show_log(killed, limit=1)
    checks.check("SNI sts, Host s3: connection killed, host-sni-mismatch logged", out.rc != 0 and len(killed) >= 1, f"curl exit {out.rc}")
    # 2. A control: a plain request to the bound host leaves an 'applied binding' line, so the next two tests can rely on its absence.
    mark = log_mark(sid)
    ctrl = sh(sb, f"{head} https://sts.us-east-1.amazonaws.com/", timeout=60)
    applied = log_since(sid, mark, "applied binding", wait=10)
    print(f"  control, SNI sts, Host sts:\n      {ctrl.text.splitlines()[0] if ctrl.text else '-'}; binding applied: {len(applied)} log line(s)")
    checks.check("control: a request for the bound host shows 'applied binding' in the log", len(applied) >= 1)
    # 3. Host outside AWS: no binding matches. 4. Dialed to an S3 address with SNI sts: the pin brings it back to sts.
    ip_s3 = sh(sb, "getent hosts s3.us-east-1.amazonaws.com | cut -d' ' -f1 | head -1").text.strip()
    checks.check("an S3 address resolved for the pin test", bool(ip_s3))
    cases = [
        ("3. SNI sts, Host other.example.com (no binding matches)",
         f"{head} https://sts.us-east-1.amazonaws.com/ -H 'Host: other.example.com'", False),
        ("4. SNI sts, dialed to an S3 address (the upstream is pinned to the SNI name)",
         f"{head} --resolve sts.us-east-1.amazonaws.com:443:{ip_s3} https://sts.us-east-1.amazonaws.com/", True),
    ]
    for label, command, expect_binding in cases:
        mark = log_mark(sid)
        out = sh(sb, command, timeout=60)
        time.sleep(6)  # let the sidecar's log catch up before asserting that a line is absent
        lines = log_since(sid, mark, "credential proxy")
        applied = [line for line in lines if "applied binding" in line]
        signed = [line for line in lines if "re-signed" in line]
        pinned = bool(STS_ID.search(out.text))
        print(f"  {label}:\n      {out.text.splitlines()[0] if out.text else '-'}; answered by the sts host: {pinned}; "
              f"binding applied: {len(applied)}; request signed: {len(signed)}")
        if expect_binding:
            host_ok = bool(applied) and all("host=sts.us-east-1.amazonaws.com" in line for line in applied)
            checks.check(f"{label}: STS answers and the binding applied is sts's", out.rc == 0 and pinned and host_ok)
        else:
            checks.check(f"{label}: no credential applied; the answer is the connected host's own", out.rc == 0 and pinned and not applied and not signed)
    # 5. No SNI: passed through undecrypted.
    mark = log_mark(sid)
    out = sh(sb, f"{head} -k https://{ips}/ -H 'Host: s3.us-east-1.amazonaws.com'", timeout=60)
    time.sleep(6)
    lines = log_since(sid, mark, "credential proxy")
    print(f"  5. allowed IP, no SNI, Host s3:\n      {out.text.splitlines()[0] if out.text else '-'}; answered by the sts host: {bool(STS_ID.search(out.text))}; "
          f"addon or vault log lines: {len(lines)}")
    checks.check("5. no SNI: passed through undecrypted, so neither the vault nor the addon saw it (no log line) and nothing was added",
                 out.rc == 0 and bool(STS_ID.search(out.text)) and not lines)
    note("what each case proves: 1 kills a decrypted request whose Host names a bound host other than the SNI; 3 and 5 show nothing is added "
         "when no binding matches or the connection is never decrypted; 4 shows the upstream follows the SNI name, not the dialed address")

    heading("D. the metadata services")
    for label, url in (("EC2 instance metadata", "http://169.254.169.254/latest/meta-data/"),
                       ("Pod Identity agent", "http://169.254.170.23/v1/credentials")):
        out = sh(sb, f"curl -sS -m 6 -o /dev/null -w '%{{http_code}}' {url}", timeout=40)
        print(f"  {label} ({url.split('/')[2]}): curl exit {out.rc}; HTTP {out.text.strip()[-3:] if out.rc == 0 else '-'}")
        checks.check(f"{label} is unreachable", out.rc != 0)
    out = sh(sb, "env | grep -c '^AWS_CONTAINER_' || true")
    checks.check("no AWS_CONTAINER_* credentials variables in the sandbox", out.text.strip() == "0")

    heading("E. search the sandbox for a temporary key or session token")
    # positive control: a made-up key in a file and in a process environment must be found, or a count of 0 means nothing
    canary = "ASIA" + "EXAMPLECANARY0001"
    sh(sb, f"printf '%s' {canary} > /tmp/osb-canary; (FAKE_KEY={canary} sleep 90 &) ; sleep 1")
    found_file = sh(sb, FILE_SCAN, timeout=180).text.strip()
    found_proc = sh(sb, PROC_SCAN, timeout=60).text.strip()
    checks.check("the scans can find a key (a made-up canary in a file and in a process environment)", found_file == "1" and found_proc == "1", f"{found_file} file, {found_proc} process")
    sh(sb, "rm -f /tmp/osb-canary; pkill -x sleep || true")
    files = sh(sb, FILE_SCAN, timeout=180).text.strip()
    procs = sh(sb, PROC_SCAN, timeout=60).text.strip()
    print("  $ grep -rIlE '<temporary key | session token | carrier header name>' /root /home /tmp /etc /var /opt | wc -l\n      " + files)
    print("  $ cat /proc/*/environ | grep -cE '<same patterns>'\n      " + procs)
    checks.check("no file holds a temporary key, a session token or a carrier header", files == "0", f"{files} file(s)")
    checks.check("no process environment holds one", procs == "0", f"{procs} match(es)")

    heading("F. the sidecar's API without its token")
    for path in ("/credential-vault", "/credential-vault/_active", "/policy"):
        out = sh(sb, f"curl -s -m 5 -o /dev/null -w '%{{http_code}}' http://127.0.0.1:18080{path}", timeout=30)
        code = out.text.strip()[-3:]
        print(f"  GET 127.0.0.1:18080{path:26} -> HTTP {code}")
        checks.check(f"sidecar API {path} refuses a caller without its token", code in ("401", "403"), f"HTTP {code}")

    heading("G. a Bearer header on a SigV4-bound host")
    out = sh(sb, "curl -s -m 20 -D - -o /dev/null -X POST -d '{}' https://dynamodb.us-east-1.amazonaws.com/ "
                 "-H 'Authorization: Bearer not-a-real-token' -H 'X-Amz-Target: DynamoDB_20120810.ListTables' "
                 "-H 'Content-Type: application/x-amz-json-1.0' | grep -iE '^HTTP|^x-osb-egress-refused'", timeout=60)
    print("  $ curl -X POST https://dynamodb.us-east-1.amazonaws.com/ -H 'Authorization: Bearer ...'")
    for line in out.text.splitlines():
        print(f"      {line.strip()[:120]}")
    checks.check("403 with x-osb-egress-refused: bearer-on-sigv4-binding", "403" in out.text and "bearer-on-sigv4-binding" in out.text)

    if not checks.failures:
        print("\n  summary: every attack above ended without a credential in the sandbox's hands.")
finally:
    cleanup()
checks.finish()
