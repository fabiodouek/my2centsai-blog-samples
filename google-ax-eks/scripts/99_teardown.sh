#!/usr/bin/env bash
# Delete everything: tasks, the cluster, the snapshot bucket, the images and the database volumes.
# EKS bills for the control plane, nodes and NAT gateway while they exist.
set -euo pipefail
source "$(dirname "$0")/env.sh"

step "AX tasks"
if k get namespace ax-system >/dev/null 2>&1 && [ -x "$AX_SRC/bin/ax" ]; then
  for t in $(ax get tasks -o name 2>/dev/null || true); do ax delete task "${t#*/}" || true; done
fi

step "Images under $CLUSTER_NAME/ (created on push, so Terraform doesn't own them)"
for repo in $(aws ecr describe-repositories --region "$AWS_REGION" \
    --query "repositories[?starts_with(repositoryName, '${CLUSTER_NAME}/')].repositoryName" --output text); do
  aws ecr delete-repository --region "$AWS_REGION" --repository-name "$repo" --force >/dev/null
  echo "deleted $repo"
done

step "terraform destroy"
terraform -chdir="$TF_DIR" destroy -input=false -auto-approve

step "PostgreSQL volumes (created by the EBS CSI driver, so they outlive the cluster)"
for vol in $(aws ec2 describe-volumes --region "$AWS_REGION" \
    --filters "Name=tag-key,Values=kubernetes.io/cluster/$CLUSTER_NAME" Name=status,Values=available \
    --query 'Volumes[].VolumeId' --output text); do
  aws ec2 delete-volume --region "$AWS_REGION" --volume-id "$vol"
  echo "deleted $vol"
done

kubectl config delete-context "$KUBE_CONTEXT" 2>/dev/null || true
