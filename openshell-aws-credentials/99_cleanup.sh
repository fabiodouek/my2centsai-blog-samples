#!/usr/bin/env bash
# Remove everything this directory creates on the gateway: the p5-* sandboxes, the p5-* providers,
# and the imported aws / aws-s3 profiles. It empties the test bucket too, so `terraform destroy`
# (which removes the IAM roles, the Pod Identity association and the bucket) has nothing to trip on.
# It does NOT touch part 4's providers (github, anthropic, echo-demo) or sandboxes, and it leaves
# the gateway, the port-forward and the cluster alone.
#
#   ./99_cleanup.sh
#   (cd terraform && terraform destroy)   # then remove the AWS side
source "$(dirname "$0")/lib.sh"

say "delete p5-* sandboxes"
for n in $(os sandbox list -o json 2>/dev/null | jq -r '.sandboxes[].name' | grep '^p5-' || true); do
  os sandbox delete "$n" >/dev/null 2>&1 && echo "  deleted sandbox ${n}" &
done
wait

say "delete p5-* providers"
for p in $(os provider list -o json 2>/dev/null | jq -r '.providers[].name' | grep '^p5-' || true); do
  os provider delete "$p" >/dev/null 2>&1 && echo "  deleted provider ${p}" || echo "  provider ${p} not deleted (still attached?)"
done

say "delete the aws / aws-s3 profiles this part imported"
for prof in aws aws-s3; do
  os profile delete "$prof" >/dev/null 2>&1 && echo "  deleted profile ${prof}" || echo "  profile ${prof} not present"
done

say "empty the test bucket (objects only; terraform destroy removes the bucket itself)"
if [[ -n "${BUCKET:-}" ]] && aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  aws s3 rm "s3://${BUCKET}/" --recursive >/dev/null 2>&1 && echo "  emptied s3://${BUCKET}/"
else
  echo "  no bucket to empty (BUCKET unset or already gone)"
fi

echo
echo "p5 sandboxes left: $(os sandbox list -o json 2>/dev/null | jq '[.sandboxes[].name | select(startswith("p5-"))] | length')"
echo "p5 providers left: $(os provider list -o json 2>/dev/null | jq '[.providers[].name | select(startswith("p5-"))] | length')"
echo "Next: (cd terraform && terraform destroy) to remove the IAM roles, Pod Identity association and bucket."
