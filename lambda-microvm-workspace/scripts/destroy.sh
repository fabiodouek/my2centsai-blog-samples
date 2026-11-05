#!/usr/bin/env bash
# Tear down the workspace:
#   1. terminate every MicroVM started from the workspace images (one per size; they run on part 2's connector)
#   2. force-delete the credentials admins added from the workspace (under part 2's user prefix), so their names can
#      be reused at once
#   3. terraform destroy: CloudFront, ALB, API service, Cognito, DynamoDB, ECR, the API's log group
#   4. delete the sandbox images (small, medium, large, xlarge)
# Part 2 (the two VPCs, iron-proxy, the control plane and the sandbox roles) is left alone; destroy it from part 2.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION" AWS_PAGER=""
PART2_INFRA="${PART2_INFRA:-$HOME/Code/lambda-microvm-iron-proxy/lambda-microvm-iron-proxy/infra}"
IMAGE_NAME="${IMAGE_NAME:-my2cents-ws-sandbox}"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
# One image per sandbox size (scripts/build_sandbox_image.py): medium keeps the original name
IMAGES=("${IMAGE_NAME}-1g" "${IMAGE_NAME}" "${IMAGE_NAME}-4g" "${IMAGE_NAME}-8g")
image_arn() { echo "arn:aws:lambda:${AWS_REGION}:${ACCOUNT}:microvm-image:$1"; }
redact() { sed -E "s/${ACCOUNT}/<account>/g"; }

live() {  # live <image name>: MicroVMs from that image that aren't terminated
  aws lambda-microvms list-microvms --image-identifier "$(image_arn "$1")" \
    --query "items[?state!='TERMINATED'].microvmId" --output text 2>/dev/null || true
}
live_any() { for img in "${IMAGES[@]}"; do live "$img"; done | tr -s ' \t\n' ' ' | sed 's/^ *//; s/ *$//'; }

for img in "${IMAGES[@]}"; do
  echo "==> terminating MicroVMs from ${img}"
  for id in $(live "$img"); do
    echo "    $id"
    aws lambda-microvms terminate-microvm --microvm-identifier "$id" >/dev/null || true
  done
done

echo "==> force-deleting the credentials admins added"
PREFIX="$(terraform -chdir="$PART2_INFRA" output -raw user_secret_prefix 2>/dev/null || true)"
if [[ -n "$PREFIX" && "$PREFIX" != "null" ]]; then
  for arn in $(aws secretsmanager list-secrets --include-planned-deletion \
                 --filters Key=name,Values="$PREFIX" --query 'SecretList[].ARN' --output text); do
    echo "    $(echo "$arn" | sed -E 's/.*:secret://')"
    aws secretsmanager delete-secret --secret-id "$arn" --force-delete-without-recovery >/dev/null \
      || echo "      (couldn't force-delete it; it leaves at the end of its recovery window)"
  done
fi

echo "==> terraform destroy (CloudFront takes several minutes)"
terraform -chdir="$ROOT/infra" init -input=false >/dev/null
terraform -chdir="$ROOT/infra" destroy -input=false -auto-approve | redact | tail -5

echo "==> deleting the sandbox images"
for _ in $(seq 1 30); do
  [[ -z "$(live_any)" ]] && break
  sleep 10
done
for img in "${IMAGES[@]}"; do
  aws lambda-microvms delete-microvm-image --image-identifier "$(image_arn "$img")" >/dev/null 2>&1 \
    && echo "    ${img}: deleted" || echo "    ${img}: not found or already deleted"
done

echo "done. Part 2 (VPCs, iron-proxy, control plane) is still running: destroy it from part 2."
