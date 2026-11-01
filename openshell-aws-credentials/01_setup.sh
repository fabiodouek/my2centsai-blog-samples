#!/usr/bin/env bash
# Setup for part 5. Run this once before the numbered tests. It:
#   1. reads the terraform outputs (bucket, role ARNs) and writes them to env.sh
#   2. restarts the gateway Pod so it picks up its EKS Pod Identity credentials (the Pod that
#      installed in part 2 started before the association existed), then restarts the port-forward
#   3. imports the aws / aws-s3 provider profiles (workspace-scoped)
#   4. creates the p5-aws provider (--runtime-credentials: the gateway mints the keys)
#   5. configures the aws_sts_assume_role refresh (role_arn from terraform), rotates once, status
#
#   ./01_setup.sh
#
# Safe to run again: it re-reads outputs, re-imports (import fails only on a duplicate ID — that's
# reported and skipped), reuses the provider, and reconfigures the refresh.
#
# terraform must already be applied (the IAM roles, the bucket and the Pod Identity association).
# Run `terraform init && terraform apply` in terraform/ first (see the README). No credential or
# the account id is ever printed.
source "$(dirname "$0")/lib.sh"
TF="${HERE}/terraform"
SESSION_NAME="p5-shared"   # CloudTrail session name for the shared provider (06_cloudtrail.sh)

[[ -d "${TF}/.terraform" ]] || { echo "terraform not initialized: run (cd ${TF} && terraform init && terraform apply)" >&2; exit 1; }

say "terraform outputs -> env.sh"
BUCKET="$(terraform -chdir="$TF" output -raw bucket_name)"
SANDBOX_ROLE_ARN="$(terraform -chdir="$TF" output -raw sandbox_role_arn)"
GATEWAY_ROLE_ARN="$(terraform -chdir="$TF" output -raw gateway_role_arn)"
P5_REGION="$(terraform -chdir="$TF" output -raw region)"
[[ -n "$BUCKET" && -n "$SANDBOX_ROLE_ARN" ]] || { echo "terraform outputs missing: apply terraform/ first" >&2; exit 1; }
{
  echo "# Part 5 AWS resources, written by 01_setup.sh from terraform outputs. Do not edit."
  echo "export BUCKET='${BUCKET}'"
  echo "export SANDBOX_ROLE_ARN='${SANDBOX_ROLE_ARN}'"
  echo "export GATEWAY_ROLE_ARN='${GATEWAY_ROLE_ARN}'"
  echo "export AWS_REGION='${P5_REGION}'"
} >"$P5_ENV"
# shellcheck source=/dev/null
source "$P5_ENV"
ok "bucket ${BUCKET}"
ok "sandbox role $(basename "$SANDBOX_ROLE_ARN"), gateway role $(basename "$GATEWAY_ROLE_ARN")"

say "kubectl rollout restart statefulset/openshell (pick up Pod Identity), then restart the port-forward"
assoc="$(aws eks list-pod-identity-associations --cluster-name "$CLUSTER_NAME" \
  --query "associations[?namespace=='${NS}' && serviceAccount=='openshell'].associationId | [0]" --output text 2>/dev/null)"
[[ -n "$assoc" && "$assoc" != None ]] && ok "Pod Identity association present (${assoc})" || echo "  WARN  no Pod Identity association for ${NS}/openshell; terraform apply may not have run"
kubectl -n "$NS" rollout restart statefulset/openshell >/dev/null
restart_pf && ok "gateway restarted, port-forward back up"
# The new gateway Pod should now have the container-credentials env the EKS Pod Identity agent injects.
if kubectl -n "$NS" get pod openshell-0 -o json | jq -e '.spec.containers[0].env[]? | select(.name=="AWS_CONTAINER_CREDENTIALS_FULL_URI")' >/dev/null 2>&1; then
  ok "gateway Pod has AWS_CONTAINER_CREDENTIALS_FULL_URI (Pod Identity webhook fired)"
else
  echo "  note  AWS_CONTAINER_CREDENTIALS_FULL_URI not visible on the Pod spec; the agent may inject it at admission — refresh status below is the real check"
fi

say "import provider profiles (workspace-scoped)"
for p in aws aws-s3; do
  out="$(os profile import -f "${HERE}/profiles/${p}.yaml" 2>&1 | nocolor)"
  if [[ $? -eq 0 ]]; then ok "imported ${p}"; else
    if grep -qiE 'already exists|duplicate' <<<"$out"; then ok "${p} already imported"; else echo "  FAIL  import ${p}: ${out}" >&2; exit 1; fi
  fi
done

say "create provider ${PROVIDER} (type aws, --runtime-credentials)"
if os provider get "$PROVIDER" >/dev/null 2>&1; then
  ok "${PROVIDER} already exists"
else
  os provider create --name "$PROVIDER" --type aws --runtime-credentials >/dev/null && ok "created ${PROVIDER}"
fi

say "provider refresh configure ${PROVIDER} --strategy aws-sts-assume-role"
os provider refresh configure "$PROVIDER" \
  --credential-key AWS_ACCESS_KEY_ID \
  --strategy aws-sts-assume-role \
  --material role_arn="$SANDBOX_ROLE_ARN" \
  --material session_name="$SESSION_NAME" \
  --material aws_region="$AWS_REGION" 2>&1 | nocolor | mask
ok "role_arn set to the sandbox role; session_name ${SESSION_NAME}; region ${AWS_REGION}"

say "provider refresh rotate ${PROVIDER} (mint the first STS session now)"
os provider refresh rotate "$PROVIDER" --credential-key AWS_ACCESS_KEY_ID 2>&1 | nocolor | mask

say "provider refresh status ${PROVIDER}"
# STATUS should read 'refreshed' with EXPIRES_AT about an hour out. 'error' with a dispatch
# failure means the gateway has no AWS identity yet — check the Pod Identity association.
os provider refresh status "$PROVIDER" 2>&1 | nocolor | mask

echo
ok "setup done. The provider mints the sandbox-role session; the tests attach it with --provider ${PROVIDER}"
ok "and pass --policy logs/aws-signing.${PROVIDER}.yaml (policy_file in lib.sh) so the supervisor signs S3, Bedrock and STS."
