#!/usr/bin/env bash
# Test 2 (S3): the data-plane cases. All signed at the supervisor with the sandbox-role session.
#   - list, small put, a 50 MB `aws s3 cp` (aws-chunked with trailer), download + checksum, boto3
#   - keys that need percent-encoding: a space, `+`, non-ASCII, and `a//b`
#     (expect SignatureDoesNotMatch or a rewritten key — double encoding + path normalization are
#      on for every service; this records what the client actually sees)
#   - DELETE vs POST: `aws s3 rm` (HTTP DELETE, denied by the read-write preset) vs
#     `delete-objects` (POST ?delete, allowed)
#   - the explicit signing modes: `sigv4:body` (<=10 MiB) and `sigv4:no_body`, each in its own
#     sandbox with a one-endpoint policy, since the mode is a per-endpoint setting.
source "$(dirname "$0")/lib.sh"
NAME="p5-s3"
cleanup() { for n in "$NAME" p5-s3-body p5-s3-no-body; do os sandbox delete "$n" >/dev/null 2>&1 & done; wait; }
trap cleanup EXIT
[[ -n "${BUCKET:-}" ]] || { echo "run ./01_setup.sh first (env.sh has no BUCKET)" >&2; exit 1; }

# A one-S3-endpoint policy in a given signing mode (sigv4 | sigv4:body | sigv4:no_body).
s3_only_policy() {  # mode
  cat <<YAML
version: 1
filesystem_policy:
  include_workdir: true
  read_only: [/bin, /usr, /lib, /proc, /dev/urandom, /etc, /var/log]
  read_write: [/tmp, /dev/null]
landlock:
  compatibility: best_effort
network_policies:
  s3_bucket:
    endpoints:
      - host: "${BUCKET}.s3.${AWS_REGION}.amazonaws.com"
        port: 443
        protocol: rest
        access: read-write
        enforcement: enforce
        credential_binding: { provider: ${PROVIDER} }
        credential_signing: $1
        signing_service: s3
    binaries:
      - { path: /usr/bin/curl }
      - { path: /usr/local/bin/aws }
      - { path: /usr/local/aws-cli/v2/*/dist/aws }
      - { path: /usr/bin/python3.12 }
YAML
}

# Start from an empty bucket (from the host, with your own AWS credentials), so the listing is this run's.
aws s3 rm "s3://${BUCKET}/" --recursive >/dev/null 2>&1 || true

say "create ${NAME} (default sigv4 auto-detect mode)"
# This test is about signing, not CA trust: aws s3 cp uses the CRT transfer client, which reads
# AWS_CA_BUNDLE only (05_ca_trust.sh shows that), so set it here.
signing_sandbox "$NAME" --env AWS_CA_BUNDLE="$CA_BUNDLE" || exit 1
ok "ready"

say "aws s3 ls (list), then a small put"
in_sb "$NAME" bash -c 'aws s3 ls "s3://$BUCKET/" 2>&1 | sed "s/^/  /" || true'
in_sb "$NAME" bash -c 'echo "hello from the sandbox via STS" > /tmp/small.txt; aws s3 cp --no-progress /tmp/small.txt "s3://$BUCKET/small.txt" 2>&1 | sed "s/^/  /"' 2>&1 | nocolor

say "a 50 MB upload (aws-chunked, STREAMING-UNSIGNED-PAYLOAD-TRAILER), then download + checksum"
in_sb "$NAME" bash -c '
  head -c 52428800 /dev/urandom > /tmp/big.bin
  up="$(sha256sum /tmp/big.bin | cut -d" " -f1)"
  aws s3 cp /tmp/big.bin "s3://$BUCKET/big.bin" >/dev/null 2>&1 && echo "  upload ok" || echo "  upload FAILED"
  aws s3 cp "s3://$BUCKET/big.bin" /tmp/back.bin >/dev/null 2>&1 && echo "  download ok" || echo "  download FAILED"
  down="$(sha256sum /tmp/back.bin 2>/dev/null | cut -d" " -f1)"
  [ "$up" = "$down" ] && echo "  checksum matches (sha256 ${up:0:12}…)" || echo "  checksum MISMATCH"
' 2>&1 | nocolor

say "boto3 put/list/get (python3.12)"
in_sb "$NAME" /usr/bin/python3.12 - <<'PY' 2>&1 | nocolor | sed 's/^/  /'
import os, boto3
b = os.environ["BUCKET"]
s3 = boto3.client("s3")
s3.put_object(Bucket=b, Key="from-boto3.txt", Body=b"hello from boto3")
got = s3.get_object(Bucket=b, Key="from-boto3.txt")["Body"].read().decode()
n = len(s3.list_objects_v2(Bucket=b).get("Contents", []))
print(f"boto3 round-trip: {got!r}; objects now: {n}")
PY

say "keys that need percent-encoding (expect SignatureDoesNotMatch or a rewritten key)"
# S3 wants single encoding and no path normalization; the signer uses double encoding + path
# normalization for every service. Record the HTTP status / error the client sees.
in_sb "$NAME" bash -c '
  for key in "my file.txt" "a+b.txt" $'"'"'caf\xc3\xa9.txt'"'"' "a//b.txt"; do
    printf "x" > /tmp/enc.bin
    out="$(aws s3 cp --no-progress /tmp/enc.bin "s3://$BUCKET/$key" 2>&1)"; rc=$?
    reason="$(grep -oE "SignatureDoesNotMatch|AccessDenied|InvalidRequest|HTTP [0-9]+|error occurred \([A-Za-z]+\)" <<<"$out" | head -n1)"
    printf "  key %-14q exit %-3s %s\n" "$key" "$rc" "${reason:-$(tail -n 1 <<<"$out" | cut -c1-90)}"
  done
' 2>&1 | nocolor
in_sb "$NAME" bash -c 'echo "  object stored for \"a//b.txt\": $(aws s3api list-objects-v2 --bucket "$BUCKET" --prefix a/ --query "Contents[].Key" --output text)"' 2>&1 | nocolor

say "DELETE vs POST: aws s3 rm (DELETE, read-write preset blocks it) vs delete-objects (POST ?delete)"
in_sb "$NAME" bash -c '
  rm_out="$(aws s3 rm "s3://$BUCKET/small.txt" 2>&1)"; echo "  s3 rm:          $(tail -n 1 <<<"$rm_out" | cut -c1-110)"
  po_out="$(aws s3api delete-objects --bucket "$BUCKET" --delete "Objects=[{Key=from-boto3.txt}]" 2>&1)"; echo "  delete-objects: $(grep -oE "\"Key\": \"[^\"]+\"|policy_denied|HTTP [0-9]+|AccessDenied" <<<"$po_out" | head -n1 || echo "ok")"
' 2>&1 | nocolor

# --- explicit signing modes --------------------------------------------------------------------
# AWS CLI 2.39 uploads with aws-chunked encoding and a trailing checksum by default. Each mode is
# tried as is, and with AWS_REQUEST_CHECKSUM_CALCULATION=when_required (a plain body with a
# Content-Length).
put() {  # sandbox size-MiB label [env]
  in_sb "$1" bash -c "head -c \$(( $2 * 1048576 )) /dev/urandom > /tmp/f.bin; o=\"\$(${4:-} aws s3 cp --no-progress /tmp/f.bin s3://\$BUCKET/$1-$2m.bin 2>&1)\" \
    && echo '  $3: ok' || echo \"  $3: FAILED \$(grep -oE 'error occurred \\([A-Za-z]+\\)|reset|broken pipe' <<<\"\$o\" | head -n 1)\"" 2>&1 | nocolor
}
for mode in "sigv4:body" "sigv4:no_body"; do
  sb="p5-s3-${mode#sigv4:}"; sb="${sb//_/-}"
  say "${mode} mode"
  s3_only_policy "$mode" >"${HERE}/logs/s3-${mode/:/-}.yaml"
  make_sandbox "$sb" --provider "$PROVIDER" --policy "${HERE}/logs/s3-${mode/:/-}.yaml" --env AWS_CA_BUNDLE="$CA_BUNDLE" \
    --env AWS_DEFAULT_REGION="$AWS_REGION" --env AWS_REGION="$AWS_REGION" --env BUCKET="$BUCKET" >/dev/null 2>&1 || { echo "  (sandbox ${sb} create failed)"; continue; }
  put "$sb" 1  "1 MiB,  CLI defaults (aws-chunked + trailer)"
  put "$sb" 1  "1 MiB,  checksum when_required        " "AWS_REQUEST_CHECKSUM_CALCULATION=when_required"
  # aws s3 cp switches to multipart above 8 MiB, so each request stays small; one 11 MiB PutObject:
  in_sb "$sb" bash -c "head -c 11534336 /dev/urandom > /tmp/big.bin; o=\"\$(AWS_REQUEST_CHECKSUM_CALCULATION=when_required aws s3api put-object --bucket \$BUCKET --key $sb-11m.bin --body /tmp/big.bin 2>&1)\" \
    && echo '  11 MiB, one PutObject, when_required : ok' || echo \"  11 MiB, one PutObject, when_required : FAILED \$(grep -oE 'error occurred \\([A-Za-z]+\\)|Connection was closed[^:]*|reset|broken pipe' <<<\"\$o\" | head -n 1)\"" 2>&1 | nocolor
  os sandbox delete "$sb" >/dev/null 2>&1 &
done
wait
