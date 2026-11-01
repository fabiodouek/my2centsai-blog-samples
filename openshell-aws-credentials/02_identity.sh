#!/usr/bin/env bash
# Test 1 (Identity): what AWS thinks the sandbox is. The supervisor signs the STS
# GetCallerIdentity call with the gateway-minted sandbox-role session, so AWS returns the
# assumed-role ARN and the session name the provider was configured with. The sandbox holds
# only placeholder AWS_* values. Run through the AWS CLI and through boto3.
source "$(dirname "$0")/lib.sh"
NAME="p5-identity"
cleanup() { os sandbox delete "$NAME" >/dev/null 2>&1; }
trap cleanup EXIT
[[ -n "${BUCKET:-}" ]] || { echo "run ./01_setup.sh first (env.sh has no BUCKET)" >&2; exit 1; }

say "create ${NAME} with --provider ${PROVIDER} and the signing policy"
signing_sandbox "$NAME" || exit 1
ok "ready"

say "the AWS_* env vars inside the sandbox are placeholders, not keys"
# The provider injects opaque placeholders. Print only the length and a hash so the real value
# (if this ever regressed) never reaches output; a real key is ~20/40 chars of base64.
for v in AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN; do
  val="$(in_sb "$NAME" bash -c "printf '%s' \"\$$v\"" 2>/dev/null)"
  looks="$( [[ "$val" == openshell:* ]] && echo 'placeholder' || echo 'NOT a placeholder (investigate)')"
  printf '  %-22s %-12s len=%-4s sha256=%s…\n' "$v" "$looks" "${#val}" "$(printf '%s' "$val" | sha12)"
done

say "aws sts get-caller-identity (signed at the supervisor)"
cli="$(in_sb "$NAME" bash -c 'aws sts get-caller-identity --output json' 2>&1 | nocolor)"
if arn="$(jq -er .Arn <<<"$cli" 2>/dev/null)"; then
  echo "  Arn:     $(mask <<<"$arn")"
  echo "  role:    $(sed -E 's#.*assumed-role/([^/]+)/.*#\1#' <<<"$arn")"
  echo "  session: $(sed -E 's#.*assumed-role/[^/]+/##' <<<"$arn")   (the provider's session_name)"
else
  echo "  get-caller-identity did not return an ARN:"; mask <<<"$cli" | sed 's/^/    /'
fi

say "the same through boto3 (python3.12, no AWS_CA_BUNDLE — relies on REQUESTS_CA_BUNDLE)"
prog='import boto3,json; print(json.dumps(boto3.client("sts").get_caller_identity()["Arn"]))'
b3="$(in_sb "$NAME" /usr/bin/python3.12 -c "$prog" 2>&1 | nocolor)"
if arn="$(jq -er . <<<"$b3" 2>/dev/null)"; then
  echo "  boto3 Arn: $(mask <<<"$arn")"
  ok "AWS CLI and boto3 both resolve to the sandbox-role session"
else
  echo "  boto3 call failed (see 05_ca_trust.sh if this is a TLS error):"; mask <<<"$b3" | sed 's/^/    /'
fi
