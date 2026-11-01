#!/usr/bin/env bash
# Test 4 (CA trust): the supervisor terminates TLS with the per-sandbox OpenShell Sandbox CA, so
# clients must trust that CA. OpenShell sets SSL_CERT_FILE, REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE and
# GIT_SSL_CAINFO (to a bundle) and NODE_EXTRA_CA_CERTS / DENO_CERT (to the CA), but NOT
# AWS_CA_BUNDLE. This settles which of those the AWS CLI, boto3 and the CLI's CRT transfer client
# actually read, by toggling them one at a time.
source "$(dirname "$0")/lib.sh"
NAME="p5-ca"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT
[[ -n "${BUCKET:-}" ]] || { echo "run ./01_setup.sh first (env.sh has no BUCKET)" >&2; exit 1; }

say "create ${NAME}"
signing_sandbox "$NAME" || exit 1

say "what OpenShell set (paths only, not contents)"
in_sb "$NAME" bash -c '
  for v in SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE AWS_CA_BUNDLE NODE_EXTRA_CA_CERTS; do
    printf "  %-20s %s\n" "$v" "${!v:-<unset>}"
  done
  echo "  aws real path:       $(readlink -f /usr/local/bin/aws)"
' 2>&1 | nocolor

# probe LABEL ENVSPEC COMMAND — run COMMAND inside the sandbox under ENVSPEC (env -u/… prefix),
# print exit code and a one-line outcome. Only reaches S3 (no secrets in output).
probe() {
  local label="$1" envspec="$2" cmd="$3" out rc
  out="$(in_sb "$NAME" bash -c "${envspec} ${cmd}" 2>&1 | nocolor)"; rc=$?
  local note
  note="$(grep -oE 'CERTIFICATE_VERIFY_FAILED|AWS_IO_TLS_ERROR_NEGOTIATION_FAILURE|SSLError|self-signed|unable to get local issuer|HTTP [0-9]+|ok' <<<"$out" | head -n1)"
  [[ -z "$note" && $rc -eq 0 ]] && note="ok"
  printf '  %-46s exit %-3s %s\n' "$label" "$rc" "${note:-$(head -n1 <<<"$out")}"
}

LS='aws s3 ls "s3://$BUCKET/" >/dev/null && echo ok'
B3='python3.12 -c "import os,boto3; boto3.client(\"s3\").list_objects_v2(Bucket=os.environ[\"BUCKET\"]); print(\"ok\")"'

say "AWS CLI v2 — which CA var does it need?"
probe "aws cli, env as set by OpenShell"            ""                           "$LS"
probe "aws cli, REQUESTS_CA_BUNDLE unset"           "env -u REQUESTS_CA_BUNDLE"   "$LS"
probe "aws cli, SSL_CERT_FILE unset"                "env -u SSL_CERT_FILE"        "$LS"
probe "aws cli, CURL_CA_BUNDLE unset"               "env -u CURL_CA_BUNDLE"       "$LS"
probe "aws cli, all three unset, AWS_CA_BUNDLE set" "env -u REQUESTS_CA_BUNDLE -u SSL_CERT_FILE -u CURL_CA_BUNDLE AWS_CA_BUNDLE=$CA_BUNDLE" "$LS"
probe "aws cli, all four unset"                     "env -u REQUESTS_CA_BUNDLE -u SSL_CERT_FILE -u CURL_CA_BUNDLE -u AWS_CA_BUNDLE" "$LS"

say "boto3 — which CA var does it need?"
probe "boto3, env as set by OpenShell"              ""                           "$B3"
probe "boto3, REQUESTS_CA_BUNDLE unset"             "env -u REQUESTS_CA_BUNDLE"   "$B3"
probe "boto3, REQUESTS unset, AWS_CA_BUNDLE set"    "env -u REQUESTS_CA_BUNDLE AWS_CA_BUNDLE=$CA_BUNDLE" "$B3"
probe "boto3, REQUESTS + AWS_CA_BUNDLE both unset"  "env -u REQUESTS_CA_BUNDLE -u AWS_CA_BUNDLE" "$B3"

say "aws s3 cp: the transfer client, CRT by default here, reads AWS_CA_BUNDLE only"
# Config files under /tmp (writable as UID 10001) pick the transfer client explicitly.
in_sb "$NAME" bash -c 'printf "[default]\ns3 =\n    preferred_transfer_client = classic\n" > /tmp/classic.cfg
  printf "[default]\ns3 =\n    preferred_transfer_client = crt\n" > /tmp/crt.cfg' 2>&1 | nocolor
in_sb "$NAME" bash -c 'head -c 4096 /dev/urandom > /tmp/cp.bin' >/dev/null 2>&1
# One command, so an env prefix applies to aws itself.
CP='aws s3 cp --no-progress /tmp/cp.bin "s3://$BUCKET/ca-probe.bin" >/dev/null && echo ok'
probe "s3 cp, default client, as set by OpenShell"   ""                                                  "$CP"
probe "s3 cp, default client, AWS_CA_BUNDLE set"     "AWS_CA_BUNDLE=$CA_BUNDLE"                          "$CP"
probe "s3 cp, classic client, as set by OpenShell"   "AWS_CONFIG_FILE=/tmp/classic.cfg"                  "$CP"
probe "s3 cp, CRT client, as set by OpenShell"       "AWS_CONFIG_FILE=/tmp/crt.cfg"                      "$CP"
probe "s3 cp, CRT client, AWS_CA_BUNDLE set"         "AWS_CONFIG_FILE=/tmp/crt.cfg AWS_CA_BUNDLE=$CA_BUNDLE" "$CP"
