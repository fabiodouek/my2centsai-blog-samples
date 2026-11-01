#!/usr/bin/env bash
# Remove everything this directory created, in an order that leaves nothing billing:
#
#   0. a check that part 5's own Terraform (../openshell-aws-credentials: two IAM roles, a Pod
#      Identity association, a bucket) is already destroyed; it stops here if not
#   1. every sandbox (their workspace volumes are EBS)
#   2. the gateway NLB (06_remote.sh --delete waits until AWS has deleted it)
#   3. the Helm releases: openshell, kata-deploy, the AWS Load Balancer Controller
#   4. Agent Sandbox (its CRD takes any remaining Sandbox objects with it)
#   5. what the chart leaves behind on uninstall: its TLS, JWT and key-encryption Secrets, the
#      gateway data PVC and any workspace PVC; then the namespace; then a wait for the EBS
#      volumes to go
#   6. every openshell-eks/* ECR repository
#   7. terraform destroy
#   8. the Container Insights log groups (part 6), which its agent creates outside Terraform
#   9. a check for leftover EBS volumes, load balancers, ECR repositories, osh-eks IAM roles and
#      buckets, and Container Insights log groups; exit 1 if any
#
# It also stops the port-forward and removes the osh-eks CLI registrations.
set -euo pipefail
cd "$(dirname "$0")"

[[ -f env.sh ]] || { echo "env.sh not found: nothing to tear down from here" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
helm() { command helm --kube-context "$KUBE_CONTEXT" "$@"; }
say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
NS="openshell"

say "0. part 5's Terraform"
p5_state="../openshell-aws-credentials/terraform/terraform.tfstate"
if [[ -f "$p5_state" && "$(jq '.resources | length' "$p5_state" 2>/dev/null)" != 0 ]]; then
  echo "  part 5's roles and bucket still exist. Destroy them first:" >&2
  echo "  (cd ../openshell-aws-credentials && ./99_cleanup.sh && terraform -chdir=terraform destroy)" >&2
  exit 1
fi
echo "  nothing left"

say "1. sandboxes"
if openshell status -g osh-eks >/dev/null 2>&1; then
  uv run python 98_delete_sandboxes.py || true
else
  echo "gateway not reachable; Sandbox objects go with the Agent Sandbox CRD in step 4"
fi

say "2. gateway NLB"
./06_remote.sh --delete || true

say "3. Helm releases"
helm -n "$NS" uninstall openshell --wait --timeout 5m 2>/dev/null || echo "  openshell: not installed"
helm -n kube-system uninstall kata-deploy --wait --timeout 5m 2>/dev/null || echo "  kata-deploy: not installed"
helm -n kube-system uninstall aws-load-balancer-controller --wait --timeout 5m 2>/dev/null || echo "  load balancer controller: not installed"

say "4. Agent Sandbox"
if [[ -f charts/agent-sandbox-v1.0.3.yaml ]]; then
  kubectl delete -f charts/agent-sandbox-v1.0.3.yaml --ignore-not-found --wait=true --timeout=5m >/dev/null || true
fi
echo "  CRD sandboxes.agents.x-k8s.io: $(kubectl get crd sandboxes.agents.x-k8s.io -o name 2>/dev/null || echo gone)"

say "5. what the chart leaves behind"
kubectl -n "$NS" delete secret openshell-server-tls openshell-client-tls openshell-jwt-keys --ignore-not-found >/dev/null 2>&1 || true
kubectl -n "$NS" delete secret -l app.kubernetes.io/instance=openshell --ignore-not-found >/dev/null 2>&1 || true
kubectl -n "$NS" delete pvc --all --wait=true --timeout=5m >/dev/null 2>&1 || true
kubectl delete namespace "$NS" fence-test --ignore-not-found --wait=true --timeout=5m >/dev/null 2>&1 || true
for _ in $(seq 1 60); do
  vols="$(aws ec2 describe-volumes --region "$AWS_REGION" \
    --filters "Name=tag:kubernetes.io/cluster/${CLUSTER_NAME},Values=owned" "Name=tag-key,Values=kubernetes.io/created-for/pvc/name" \
    --query 'Volumes[].VolumeId' --output text)"
  [[ -z "$vols" ]] && break
  sleep 10
done
echo "  PVC volumes left: ${vols:-none}"

say "6. ECR repositories under ${ECR_PREFIX}/"
for repo in $(aws ecr describe-repositories --region "$AWS_REGION" \
    --query "repositories[?starts_with(repositoryName, '${ECR_PREFIX}/')].repositoryName" --output text); do
  aws ecr delete-repository --region "$AWS_REGION" --repository-name "$repo" --force >/dev/null
  echo "  deleted ${repo}"
done

say "7. terraform destroy"
MY_IP="${MY_IP:-$(curl -fsS -m 10 https://checkip.amazonaws.com)}"
terraform -chdir=terraform destroy -input=false -auto-approve -no-color \
  -var "region=${AWS_REGION}" -var "public_access_cidr=${MY_IP}/32" >logs/terraform-destroy.log 2>&1 \
  || { tail -n 30 logs/terraform-destroy.log >&2; exit 1; }
grep -E '^Destroy complete' logs/terraform-destroy.log

say "8. Container Insights log groups"
# Part 6's cloudwatch-agent creates /aws/containerinsights/<cluster>/performance itself, with no
# retention, so terraform destroy leaves it behind.
for lg in $(aws logs describe-log-groups --region "$AWS_REGION" --log-group-name-prefix "/aws/containerinsights/${CLUSTER_NAME}/" \
    --query 'logGroups[].logGroupName' --output text); do
  aws logs delete-log-group --region "$AWS_REGION" --log-group-name "$lg"
  echo "  deleted ${lg}"
done

say "local cleanup"
if [[ -f .port-forward.pid ]]; then kill "$(cat .port-forward.pid)" 2>/dev/null || true; rm -f .port-forward.pid; fi
openshell gateway remove osh-eks >/dev/null 2>&1 || true
openshell gateway remove osh-eks-nlb >/dev/null 2>&1 || true
rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateways/osh-eks" "${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateways/osh-eks-nlb"
kubectl config delete-context "$KUBE_CONTEXT" >/dev/null 2>&1 || true
rm -f terraform/container_insights.auto.tfvars   # part 6's setting, or a new cluster gets the add-on

say "9. leftovers"
left=0
vols="$(aws ec2 describe-volumes --region "$AWS_REGION" --filters "Name=tag:project,Values=openshell-eks" --query 'Volumes[].VolumeId' --output text)"
lbs="$(aws elbv2 describe-load-balancers --region "$AWS_REGION" --query "LoadBalancers[?starts_with(LoadBalancerName, 'k8s-openshel')].LoadBalancerName" --output text)"
repos="$(aws ecr describe-repositories --region "$AWS_REGION" --query "repositories[?starts_with(repositoryName, '${ECR_PREFIX}/')].repositoryName" --output text)"
roles="$(aws iam list-roles --query "Roles[?starts_with(RoleName, '${CLUSTER_NAME}-')].RoleName" --output text)"
buckets="$(aws s3api list-buckets --query "Buckets[?starts_with(Name, '${CLUSTER_NAME}-')].Name" --output text)"
logs="$(aws logs describe-log-groups --region "$AWS_REGION" --log-group-name-prefix "/aws/containerinsights/${CLUSTER_NAME}/" \
  --query 'logGroups[].logGroupName' --output text)"
for what in "EBS volumes:${vols}" "load balancers:${lbs}" "ECR repositories:${repos}" "IAM roles:${roles}" \
    "S3 buckets:${buckets}" "Container Insights log groups:${logs}"; do
  if [[ -n "${what#*:}" ]]; then echo "  LEFT  ${what}"; left=1; else echo "  none  ${what%%:*}"; fi
done
(( left == 0 )) || exit 1
rm -f env.sh
echo "everything is gone"
