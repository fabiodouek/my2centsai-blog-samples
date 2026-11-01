#!/usr/bin/env bash
# Test 6 (Attacks): what the design does and does not stop, from inside one sandbox. Every value
# that could be a credential is printed only as a length and a SHA-256 prefix — never the value.
source "$(dirname "$0")/lib.sh"
NAME="p5-attack"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT
[[ -n "${BUCKET:-}" ]] || { echo "run ./01_setup.sh first (env.sh has no BUCKET)" >&2; exit 1; }

say "create ${NAME}"
signing_sandbox "$NAME" || exit 1

say "1. Signing needs no placeholder: unset AWS_* and curl the S3 endpoint unsigned"
# The supervisor signs any request that reaches a signed endpoint, so an unsigned request works.
in_sb "$NAME" bash -c '
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    curl -sS --cacert "'"$CA_BUNDLE"'" -X PUT --data-binary "unsigned client, signed by the supervisor" \
      "https://$BUCKET.s3.$AWS_REGION.amazonaws.com/unsigned.txt" -o /dev/null -w "  PUT  HTTP %{http_code}\n"
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    curl -sS --cacert "'"$CA_BUNDLE"'" "https://$BUCKET.s3.$AWS_REGION.amazonaws.com/unsigned.txt" \
      -o /dev/null -w "  GET  HTTP %{http_code}\n"
' 2>&1 | nocolor

say "2. AssumeRole / GetSessionToken through the signed STS endpoint (refused by IAM, not OpenShell)"
in_sb "$NAME" bash -c '
  a="$(aws sts assume-role --role-arn "'"$SANDBOX_ROLE_ARN"'" --role-session-name esc 2>&1)"
  echo "  assume-role:      $(grep -oE "AccessDenied|not authorized|explicit deny|Credentials" <<<"$a" | head -n1 || echo "see-output")"
  g="$(aws sts get-session-token 2>&1)"
  echo "  get-session-token:$(grep -oE "AccessDenied|not authorized|explicit deny|Credentials" <<<"$g" | head -n1 || echo "see-output")"
' 2>&1 | mask | nocolor

say "3. Presigned URL through the signed endpoint (S3 rejects: two auth mechanisms)"
in_sb "$NAME" bash -c '
  url="$(aws s3 presign "s3://$BUCKET/small.txt" --expires-in 300 2>/dev/null)"
  echo "  presign produced a URL: $([ -n "$url" ] && echo yes || echo no) (its X-Amz-Credential carries the placeholder key)"
  curl -sS --cacert "'"$CA_BUNDLE"'" "$url" -o /dev/null -w "  GET presigned HTTP %{http_code}\n" 2>&1
' 2>&1 | nocolor

say "4. IMDS and the EKS Pod Identity agent (always blocked by the supervisor, whatever the policy)"
in_sb "$NAME" bash -c '
  curl -sS -m 5 -o /dev/null -w "  169.254.169.254 (IMDS):         HTTP %{http_code} / " "http://169.254.169.254/latest/meta-data/" 2>&1; echo "(connect error = blocked)"
  curl -sS -m 5 -o /dev/null -w "  169.254.170.23 (Pod Identity):  HTTP %{http_code} / " "http://169.254.170.23/v1/credentials" 2>&1; echo "(connect error = blocked)"
' 2>&1 | nocolor

say "5. Scan the sandbox for real keys (env, /proc, ~/.aws) — only placeholders should exist"
in_sb "$NAME" bash -c '
  echo "  env AWS_* values that are placeholders / total:"
  tot=0; ph=0
  for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; do
    val="${!v:-}"; [ -n "$val" ] && tot=$((tot+1)); case "$val" in openshell:*) ph=$((ph+1));; esac
  done
  echo "    ${ph}/${tot}"
  echo "  AWS_* in /proc/self/environ that are NOT placeholders: $(tr "\0" "\n" </proc/self/environ | grep -E "^AWS_(ACCESS_KEY_ID|SECRET_ACCESS_KEY|SESSION_TOKEN)=" | grep -vc "=openshell:" )"
  echo "  ~/.aws/credentials or ~/.aws/config: $(ls ~/.aws/credentials ~/.aws/config 2>/dev/null | wc -l | tr -d " ") file(s)"
' 2>&1 | nocolor
