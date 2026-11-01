#!/usr/bin/env bash
# Test 7 (Lifetime): the STS session's refresh timing, a manual rotate, and provider delete as
# revocation. The gateway refreshes 300 s before expiry (worker tick 60 s, supervisor poll 10 s),
# caps the session at max_lifetime_seconds (3600), and retries STS errors every 60 s forever.
source "$(dirname "$0")/lib.sh"
NAME="p5-life"
TMP="p5-aws-tmp"
cleanup() {
  os sandbox delete "$NAME" >/dev/null 2>&1
  os sandbox provider detach "$NAME" "$TMP" >/dev/null 2>&1
  os provider delete "$TMP" >/dev/null 2>&1
}
trap cleanup EXIT
[[ -n "${BUCKET:-}" ]] || { echo "run ./01_setup.sh first (env.sh has no BUCKET)" >&2; exit 1; }

say "refresh status of ${PROVIDER} (EXPIRES_AT ~1h out; NEXT_REFRESH ~5 min before that)"
os provider refresh status "$PROVIDER" --credential-key AWS_ACCESS_KEY_ID 2>&1 | nocolor | mask

say "manual rotate, then status again (a new session; EXPIRES_AT moves forward)"
expires() { os provider refresh status "$PROVIDER" --credential-key AWS_ACCESS_KEY_ID 2>/dev/null | nocolor | awk -v p="$PROVIDER" '$1 == p {print $6" "$7}'; }
before="$(expires)"
os provider refresh rotate "$PROVIDER" --credential-key AWS_ACCESS_KEY_ID 2>&1 | nocolor | mask
sleep 3
after="$(expires)"
echo "  expiry before rotate: ${before:-<n/a>}"
echo "  expiry after rotate:  ${after:-<n/a>}"
os provider refresh status "$PROVIDER" --credential-key AWS_ACCESS_KEY_ID 2>&1 | nocolor | mask

say "provider delete as revocation: a throwaway provider, then delete it and watch signing stop"
os provider get "$TMP" >/dev/null 2>&1 || os provider create --name "$TMP" --type aws --runtime-credentials >/dev/null
os provider refresh configure "$TMP" --credential-key AWS_ACCESS_KEY_ID --strategy aws-sts-assume-role \
  --material role_arn="$SANDBOX_ROLE_ARN" --material session_name="p5-tmp" --material aws_region="$AWS_REGION" 2>&1 | nocolor | mask >/dev/null
os provider refresh rotate "$TMP" --credential-key AWS_ACCESS_KEY_ID >/dev/null 2>&1
make_sandbox "$NAME" --provider "$TMP" --policy "$(policy_file "$TMP")" \
  --env AWS_DEFAULT_REGION="$AWS_REGION" --env AWS_REGION="$AWS_REGION" --env BUCKET="$BUCKET" >/dev/null || exit 1

ok1="$(in_sb "$NAME" bash -c 'aws sts get-caller-identity --query Arn --output text' 2>&1 | nocolor)"
echo "  before delete: $(mask <<<"$ok1")"
say "revocation: detach ${TMP} from ${NAME}"
os sandbox provider detach "$NAME" "$TMP" --wait 2>&1 | nocolor | tr '\n' ' ' | sed -E 's/ +/ /g; s/│//g; s/^/  /'; echo
# The policy binds the AWS endpoints to this provider (credential_binding), so the gateway refuses
# the detach. Drop the AWS rules first, then detach and delete.
say "openshell policy set ${NAME} with no AWS endpoints, then detach and delete ${TMP}"
cat >"${HERE}/logs/no-aws.yaml" <<'YAML'
version: 1
filesystem_policy:
  include_workdir: true
  read_only: [/bin, /usr, /lib, /proc, /dev/urandom, /etc, /var/log]
  read_write: [/tmp, /dev/null]
landlock:
  compatibility: best_effort
network_policies: {}
YAML
os policy set "$NAME" --policy "${HERE}/logs/no-aws.yaml" --wait 2>&1 | nocolor | tail -n 1 | sed 's/^/  /'
t=$(now); os sandbox provider detach "$NAME" "$TMP" --wait >/dev/null 2>&1 && echo "  detached after $(since "$t") s"
os provider delete "$TMP" 2>&1 | nocolor | mask | sed 's/^/  /'
after_del="$(in_sb "$NAME" bash -c 'aws sts get-caller-identity 2>&1' | nocolor)"
echo "  aws sts get-caller-identity: $(grep -oE 'Unable to locate credentials|AccessDenied|InvalidClientTokenId|Could not connect[^:]*|error occurred \([A-Za-z]+\)' <<<"$after_del" | head -n1)"
echo "  unsigned curl PUT to the bucket: $(in_sb "$NAME" bash -c 'curl -sS --cacert '"$CA_BUNDLE"' -X PUT --data-binary x -o /dev/null -w "HTTP %{http_code}" "https://$BUCKET.s3.$AWS_REGION.amazonaws.com/after-revoke.txt" 2>&1 | tail -n 1')"
