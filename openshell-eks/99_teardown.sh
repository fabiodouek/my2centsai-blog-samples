#!/usr/bin/env bash
# Remove everything this directory created, in an order that leaves nothing billing:
#
#   1. every sandbox (their workspace volumes are EBS)
#   2. the gateway NLB (06_remote.sh --delete waits until AWS has deleted it)
#   3. the Helm releases: openshell, kata-deploy, the AWS Load Balancer Controller
#   4. Agent Sandbox (its CRD takes any remaining Sandbox objects with it)
#   5. what the chart leaves behind on uninstall: its TLS, JWT and key-encryption Secrets, the
#      gateway data PVC and any workspace PVC; then the namespace; then a wait for the EBS
#      volumes to go
#   6. every openshell-eks/* ECR repository
#   7. terraform destroy
#   8. a check for leftover EBS volumes, load balancers and ECR repositories; exit 1 if any
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

say "local cleanup"
if [[ -f .port-forward.pid ]]; then kill "$(cat .port-forward.pid)" 2>/dev/null || true; rm -f .port-forward.pid; fi
openshell gateway remove osh-eks >/dev/null 2>&1 || true
openshell gateway remove osh-eks-nlb >/dev/null 2>&1 || true
rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateways/osh-eks" "${XDG_CONFIG_HOME:-$HOME/.config}/openshell/gateways/osh-eks-nlb"
kubectl config delete-context "$KUBE_CONTEXT" >/dev/null 2>&1 || true

say "8. leftovers"
left=0
vols="$(aws ec2 describe-volumes --region "$AWS_REGION" --filters "Name=tag:project,Values=openshell-eks" --query 'Volumes[].VolumeId' --output text)"
lbs="$(aws elbv2 describe-load-balancers --region "$AWS_REGION" --query "LoadBalancers[?starts_with(LoadBalancerName, 'k8s-openshel')].LoadBalancerName" --output text)"
repos="$(aws ecr describe-repositories --region "$AWS_REGION" --query "repositories[?starts_with(repositoryName, '${ECR_PREFIX}/')].repositoryName" --output text)"
for what in "EBS volumes:${vols}" "load balancers:${lbs}" "ECR repositories:${repos}"; do
  if [[ -n "${what#*:}" ]]; then echo "  LEFT  ${what}"; left=1; else echo "  none  ${what%%:*}"; fi
done
(( left == 0 )) || exit 1
rm -f env.sh
echo "everything is gone"
