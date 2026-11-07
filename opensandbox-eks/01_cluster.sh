#!/usr/bin/env bash
# Create the EKS cluster for this series (or bring an existing one back in line with the
# Terraform), install the AWS Load Balancer Controller, write env.sh, then check what
# OpenSandbox needs from the cluster: Kubernetes 1.37, two Ready m7i.2xlarge nodes, /dev/kvm
# on each node (nested virtualization, for Kata), IMDSv2 with a hop limit of 1, the EKS Pod
# Identity agent and the load balancer controller.
#
# Safe to run again: a second run changes nothing and repeats the checks. Every check that
# fails is reported, and the script then exits 1.
#
# This script writes ./env.sh (gitignored) from the Terraform outputs; every later script
# sources it.
#
# ECR scan on push can only be set for the whole registry: this configures the registry's
# scanning (basic, scan on push for opensandbox-eks/*), and a teardown resets it to the
# default (basic, no rules). If the registry has a configuration of its own, the script stops
# first: ALLOW_ECR_SCAN_OVERWRITE=1 replaces it. MANAGE_ECR_SCAN_ON_PUSH=false removes the rule
# if this Terraform already manages it, and otherwise leaves the registry alone.
#
# MY_IP sets the one address allowed to reach the EKS API (default: your current public IP,
# from checkip.amazonaws.com). If your IP changes, run this again.
set -euo pipefail
cd "$(dirname "$0")"

REGION="us-east-1"  # passed to Terraform as var.region; env.sh exports it as AWS_REGION
# AWS Load Balancer Controller chart (app v3.6.2). A bump means two more changes, and
# nothing checks that they match:
#  - the IAM policy: terraform/lbc-iam-policy-v<version>.json is the controller's
#    docs/install/iam_policy.json at that tag (the file name is also in terraform/main.tf);
#  - the CRDs: Helm installs a chart's crds/ on first install only and never upgrades
#    them, so apply the new release's CRDs with kubectl first (see its release notes).
LBC_CHART_VERSION="3.6.2"
LBC_RELEASE="aws-load-balancer-controller"
NODE_TYPE="m7i.2xlarge"
DEBUG_IMAGE="public.ecr.aws/amazonlinux/amazonlinux:2023"
LOGS="logs"   # full Terraform and Helm output (gitignored)
MANAGE_ECR_SCAN_ON_PUSH="${MANAGE_ECR_SCAN_ON_PUSH:-true}"

for tool in terraform aws kubectl helm jq curl; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
if [[ "$MANAGE_ECR_SCAN_ON_PUSH" != true && "$MANAGE_ECR_SCAN_ON_PUSH" != false ]]; then
  echo "MANAGE_ECR_SCAN_ON_PUSH must be true or false (got '${MANAGE_ECR_SCAN_ON_PUSH}')" >&2
  exit 1
fi
mkdir -p "$LOGS"

# --- output and timing helpers -------------------------------------------------
script_start=$(date +%s)
timings=""
phase_name=""
phase_start=0
duration() { local s=$1; if (( s >= 60 )); then printf '%dm %02ds' $((s / 60)) $((s % 60)); else printf '%ds' "$s"; fi; }
phase() {
  phase_name="$1"
  phase_start=$(date +%s)
  printf '\n\033[1m== %s\033[0m\n' "$1"
}
phase_done() {
  local took=$(( $(date +%s) - phase_start ))
  timings+="$(printf '  %-30s %9s' "$phase_name" "$(duration "$took")")"$'\n'
  printf '   took %s\n' "$(duration "$took")"
}
failures=0
ok()   { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }

# --- 1. Terraform ---------------------------------------------------------------
MY_IP="${MY_IP:-$(curl -fsS -m 10 https://checkip.amazonaws.com || true)}"
if [[ ! "$MY_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "could not work out your public IP (got '${MY_IP}'): set MY_IP and run again" >&2
  exit 1
fi

phase "terraform apply"
echo "EKS API endpoint open to ${MY_IP}/32 only (full output: ${LOGS}/terraform-apply.log)"
if ! terraform -chdir=terraform init -input=false -no-color >"$LOGS/terraform-init.log" 2>&1; then
  tail -n 20 "$LOGS/terraform-init.log" >&2
  exit 1
fi

# The registry's scanning configuration exists before this Terraform does. Only take it
# over if it is still the default, or if this Terraform already manages it.
# (The state list is captured before grep reads it: piping it into `grep -q` under pipefail
# fails whenever grep exits early and Terraform dies of SIGPIPE.)
tf_state="$(terraform -chdir=terraform state list 2>/dev/null || true)"
scan_in_state=false
if grep -q '^aws_ecr_registry_scanning_configuration\.scan_on_push' <<<"$tf_state"; then scan_in_state=true; fi
if [[ "$MANAGE_ECR_SCAN_ON_PUSH" == false ]]; then
  if [[ "$scan_in_state" == true ]]; then
    echo "ECR scanning: no longer managed (MANAGE_ECR_SCAN_ON_PUSH=false); this apply removes the"
    echo "scan-on-push rule this Terraform created, and the registry returns to basic, no rules"
  else
    echo "ECR scanning: not managed (MANAGE_ECR_SCAN_ON_PUSH=false); the registry is left as it is"
  fi
elif [[ "$scan_in_state" == true ]]; then
  echo "ECR scanning: already managed by this Terraform"
else
  scan_config="$(aws ecr get-registry-scanning-configuration --region "$REGION" --query scanningConfiguration --output json)"
  if jq -e '.scanType == "BASIC" and (.rules | length) == 0' <<<"$scan_config" >/dev/null; then
    echo "ECR scanning: registry at the default (basic, no rules); adding scan on push for opensandbox-eks/*"
  elif [[ "${ALLOW_ECR_SCAN_OVERWRITE:-0}" == 1 ]]; then
    # Printed so it can be put back by hand after a teardown.
    echo "ECR scanning: replacing the registry's own configuration (ALLOW_ECR_SCAN_OVERWRITE=1), which was:"
    jq . <<<"$scan_config"
  else
    {
      echo "The ECR registry in ${REGION} has a scanning configuration of its own:"
      jq . <<<"$scan_config"
      echo "Terraform would replace it for the whole registry, and a teardown would then reset it"
      echo "to basic scanning with no rules. Run again with ALLOW_ECR_SCAN_OVERWRITE=1 to replace"
      echo "it, or with MANAGE_ECR_SCAN_ON_PUSH=false to leave it alone (no scan on push)."
    } >&2
    exit 1
  fi
fi

# The terminal shows the plan summary, each resource as it completes, a line a minute for
# the slow ones, and any error; the log keeps everything.
set +e
terraform -chdir=terraform apply -input=false -auto-approve -no-color \
  -var "region=${REGION}" \
  -var "public_access_cidr=${MY_IP}/32" \
  -var "manage_ecr_scan_on_push=${MANAGE_ECR_SCAN_ON_PUSH}" 2>&1 \
  | tee "$LOGS/terraform-apply.log" \
  | awk '/^(Plan:|No changes\.|Apply complete!)|(Creation|Modifications|Destruction) complete after|Still [a-z]+\.\.\. \[[0-9]+m0?0s elapsed\]|(Error|Warning): / {
           sub(/ \[id=[^]]*\]$/, ""); print; fflush() }'
apply_rc=${PIPESTATUS[0]}
set -e
if (( apply_rc != 0 )); then
  echo "terraform apply failed (exit ${apply_rc}); the end of ${LOGS}/terraform-apply.log:" >&2
  tail -n 30 "$LOGS/terraform-apply.log" >&2
  exit 1
fi
phase_done

# --- 2. env.sh ------------------------------------------------------------------
phase "env.sh"
terraform -chdir=terraform output -json | jq -r '
  "# Written by 01_cluster.sh from the Terraform outputs. Every later script sources it;",
  "# run ./01_cluster.sh again to refresh it instead of editing it.",
  ({ CLUSTER_NAME: .cluster_name.value, AWS_REGION: .region.value,
     ECR_REGISTRY: .ecr_registry.value, ECR_PREFIX: .ecr_prefix.value,
     NODEGROUP_NAME: .nodegroup_name.value, VPC_ID: .vpc_id.value,
     LBC_ROLE_ARN: .lbc_role_arn.value, KUBE_CONTEXT: .cluster_name.value }
   | to_entries[] | "export \(.key)=\(.value | @sh)")' >env.sh.tmp
mv env.sh.tmp env.sh
grep '^export' env.sh | sed 's/^export /  /'
# shellcheck source=/dev/null
source ./env.sh
phase_done

# --- 3. kubeconfig --------------------------------------------------------------
phase "kubeconfig"
aws eks update-kubeconfig --region "$AWS_REGION" --name "$CLUSTER_NAME" --alias "$KUBE_CONTEXT" >/dev/null
# From here on every kubectl and helm call names this context, so switching contexts in
# another terminal can't point the script at a different cluster.
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
helm() { command helm --kube-context "$KUBE_CONTEXT" "$@"; }
echo "kubectl context: ${KUBE_CONTEXT}"
# Terraform ignores the node group's desired size, so a group scaled to 0 stays at 0.
desired="$(aws eks describe-nodegroup --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" \
  --nodegroup-name "$NODEGROUP_NAME" --query nodegroup.scalingConfig.desiredSize --output text)"
if [[ "$desired" == "0" ]]; then
  echo "node group ${NODEGROUP_NAME} is scaled to 0: run ./08_scale.sh up, then this script again" >&2
  exit 1
fi
phase_done

# --- 4. AWS Load Balancer Controller --------------------------------------------
phase "AWS Load Balancer Controller"
# Its IAM role comes from EKS Pod Identity (created by Terraform). region and vpcId are set
# because the controller would otherwise read them from instance metadata, which the
# hop limit of 1 keeps out of reach of pods. keepTLSSecret reuses the webhook certificate
# on upgrade instead of minting a new one, so an upgrade with the same values is a no-op.
prev_rev="$(helm -n kube-system list --filter "^${LBC_RELEASE}\$" -o json | jq -r '.[0].revision // empty')"
if ! helm upgrade --install "$LBC_RELEASE" aws-load-balancer-controller \
    --repo https://aws.github.io/eks-charts --version "$LBC_CHART_VERSION" \
    --namespace kube-system \
    --set clusterName="$CLUSTER_NAME" \
    --set region="$AWS_REGION" \
    --set vpcId="$VPC_ID" \
    --set serviceAccount.name=aws-load-balancer-controller \
    --set keepTLSSecret=true \
    --set defaultTags.project=opensandbox-eks \
    --wait --timeout 5m >"$LOGS/helm-lbc.log" 2>&1; then
  tail -n 20 "$LOGS/helm-lbc.log" >&2
  exit 1
fi
rev="$(helm -n kube-system list --filter "^${LBC_RELEASE}\$" -o json | jq -r '.[0].revision')"
if [[ -z "$prev_rev" ]]; then
  echo "chart ${LBC_CHART_VERSION} installed (revision ${rev})"
elif diff <(helm -n kube-system get manifest "$LBC_RELEASE" --revision "$prev_rev") \
          <(helm -n kube-system get manifest "$LBC_RELEASE" --revision "$rev") >/dev/null; then
  echo "chart ${LBC_CHART_VERSION} upgraded to revision ${rev}: manifests identical to revision ${prev_rev}"
else
  echo "chart ${LBC_CHART_VERSION} upgraded to revision ${rev}: manifests changed since revision ${prev_rev}"
fi
phase_done

# --- 5. Checks ------------------------------------------------------------------
phase "checks"

server="$(kubectl version -o json | jq -r '.serverVersion.gitVersion')"
if [[ "$server" == v1.37.* ]]; then ok "Kubernetes ${server}"; else fail "Kubernetes ${server} (expected v1.37.x)"; fi

kubectl wait --for=condition=Ready nodes --all --timeout=5m >/dev/null || true
kubectl get nodes -o custom-columns='NODE:.metadata.name,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type,KUBELET:.status.nodeInfo.kubeletVersion,KERNEL:.status.nodeInfo.kernelVersion,RUNTIME:.status.nodeInfo.containerRuntimeVersion'
nodes_json="$(kubectl get nodes -o json)"
node_count="$(jq '.items | length' <<<"$nodes_json")"
ready_count="$(jq --arg t "$NODE_TYPE" '[.items[]
  | select(.metadata.labels["node.kubernetes.io/instance-type"] == $t)
  | select(any(.status.conditions[]; .type == "Ready" and .status == "True"))] | length' <<<"$nodes_json")"
if [[ "$node_count" == 2 && "$ready_count" == 2 ]]; then
  ok "2 nodes Ready, both ${NODE_TYPE}"
else
  fail "${ready_count} of ${node_count} nodes Ready as ${NODE_TYPE} (expected 2 of 2)"
fi
nodes="$(jq -r '.items[].metadata.name' <<<"$nodes_json")"

# /dev/kvm on every node, from a debug pod that mounts the node's root filesystem at /host.
# The pod's shell runs as "$KVM_MARKER" ($0), which is how the script finds its own debug
# pods and leaves any other node-debugger pod alone. The command always exits 0, and the
# output decides. Pods left by an interrupted run are deleted first.
KVM_MARKER="osb-kvm-check"
kvm_pods() {  # this script's debug pods in the default namespace, on node $1 (or on any node)
  kubectl get pods -n default -o json \
    | jq -r --arg node "${1:-}" --arg marker "$KVM_MARKER" '.items[]
        | select(.metadata.name | startswith("node-debugger-"))
        | select(any((.spec.containers[0].command // []) + (.spec.containers[0].args // []) | .[]; . == $marker))
        | select($node == "" or .spec.nodeName == $node) | .metadata.name'
}
for pod in $(kvm_pods); do kubectl delete pod -n default "$pod" --ignore-not-found >/dev/null; done
debug_failed=" "   # nodes whose debug pod could not be created (already reported)
for node in $nodes; do
  # kubectl names the pod node-debugger-<node>-<suffix>, and EKS node names contain dots, so
  # the API server warns that the name is not a DNS label. That warning is dropped; any other
  # message is shown. "vmx" counts the CPUs whose flags include VT-x (the kernel also prints a
  # separate "vmx flags" line per CPU, hence the match on the flags line only).
  # shellcheck disable=SC2016  # $(...) runs inside the debug container
  if ! err="$(kubectl debug -n default "node/$node" --quiet --profile=general --image="$DEBUG_IMAGE" -- \
      sh -c 'ls -l /host/dev/kvm 2>&1; echo "vmx: $(grep "^flags" /proc/cpuinfo | grep -cw vmx)"' "$KVM_MARKER" \
      2>&1 >/dev/null)"; then
    fail "${node}: kubectl debug failed: ${err//$'\n'/ }"
    debug_failed+="${node} "
    continue
  fi
  if [[ -n "$err" ]]; then grep -v "a DNS label is recommended" <<<"$err" || true; fi
done
for node in $nodes; do
  if [[ "$debug_failed" == *" ${node} "* ]]; then continue; fi
  pod="$(kvm_pods "$node")"   # captured whole, then cut (a pipe into head can SIGPIPE under pipefail)
  pod="${pod%%$'\n'*}"
  if [[ -z "$pod" ]]; then fail "${node}: no debug pod was created"; continue; fi
  if kubectl wait -n default --for=jsonpath='{.status.phase}'=Succeeded "pod/$pod" --timeout=3m >/dev/null 2>&1; then
    out="$(kubectl logs -n default "$pod")"
  else
    # Usually the image pull: public.ecr.aws is pulled anonymously, through the one NAT address.
    why="$(kubectl get pod -n default "$pod" -o jsonpath='{.status.phase} {.status.containerStatuses[0].state.waiting.reason} {.status.containerStatuses[0].state.waiting.message}' 2>/dev/null || true)"
    if [[ -z "${why// /}" ]]; then why="no status reported"; fi
    out="(the debug pod did not finish within 3 minutes: ${why})"
  fi
  kubectl delete pod -n default "$pod" --ignore-not-found --wait=false >/dev/null
  kvm="$(grep -m 1 '/host/dev/kvm' <<<"$out" || true)"
  vmx="$(sed -n 's/^vmx: //p' <<<"$out")"
  if [[ "$kvm" == c* ]]; then
    ok "${node}: ${kvm} (vmx on ${vmx} vCPUs)"
  else
    fail "${node}: /dev/kvm is not a character device: ${out//$'\n'/ }"
  fi
done

# Instance settings, from EC2 itself, for the instances behind the nodes.
instance_ids="$(jq -r '.items[].spec.providerID | split("/") | last' <<<"$nodes_json")"
# shellcheck disable=SC2086  # one instance ID per word
instances="$(aws ec2 describe-instances --region "$AWS_REGION" --instance-ids $instance_ids \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,MetadataOptions.HttpTokens,MetadataOptions.HttpPutResponseHopLimit,CpuOptions.NestedVirtualization]' \
  --output text)"
while read -r id type tokens hops nested; do
  detail="${id} (${type}): HttpTokens ${tokens}, HttpPutResponseHopLimit ${hops}, NestedVirtualization ${nested}"
  if [[ "$tokens" == required && "$hops" == 1 && "$nested" == enabled ]]; then ok "$detail"; else fail "$detail"; fi
done <<<"$instances"

kubectl -n kube-system rollout status daemonset/eks-pod-identity-agent --timeout=2m >/dev/null || true
pia="$(kubectl -n kube-system get daemonset eks-pod-identity-agent -o json \
  | jq -r '"\(.status.numberReady)/\(.status.desiredNumberScheduled)"')"
if [[ "$pia" == "2/2" ]]; then ok "eks-pod-identity-agent DaemonSet ${pia} ready"; else fail "eks-pod-identity-agent DaemonSet ${pia} ready (expected 2/2)"; fi

kubectl -n kube-system rollout status "deployment/${LBC_RELEASE}" --timeout=3m >/dev/null || true
read -r lbc_ready lbc_want < <(kubectl -n kube-system get deployment "$LBC_RELEASE" -o json \
  | jq -r '"\(.status.readyReplicas // 0) \(.spec.replicas)"')
# Pod Identity injects this variable into pods whose service account has an association.
lbc_creds="$(kubectl -n kube-system get pods -l app.kubernetes.io/name=aws-load-balancer-controller -o json \
  | jq '[.items[] | select(any(.spec.containers[0].env[]?; .name == "AWS_CONTAINER_CREDENTIALS_FULL_URI"))] | length')"
if (( lbc_want > 0 && lbc_ready == lbc_want && lbc_creds == lbc_want )); then
  ok "${LBC_RELEASE} ${lbc_ready}/${lbc_want} ready, credentials from EKS Pod Identity"
else
  fail "${LBC_RELEASE} ${lbc_ready}/${lbc_want} ready, ${lbc_creds} pods with Pod Identity credentials"
  # Pods created before the Pod Identity association existed never get credentials, and a
  # re-run doesn't recreate them.
  if (( lbc_creds < lbc_want )); then
    echo "        pods without credentials started before their Pod Identity association; restart them:"
    echo "        kubectl --context ${KUBE_CONTEXT} -n kube-system rollout restart deployment/${LBC_RELEASE}"
  fi
fi

default_sc="$(kubectl get storageclass -o json | jq -r '[.items[]
  | select(.metadata.annotations["storageclass.kubernetes.io/is-default-class"] == "true")
  | "\(.metadata.name) (\(.provisioner), type \(.parameters.type // "not set: the driver default"))"] | join(", ")')"
if [[ -n "$default_sc" ]]; then ok "default StorageClass ${default_sc}"; else fail "no default StorageClass"; fi
phase_done

# --- Summary --------------------------------------------------------------------
printf '\n\033[1m== Timings\033[0m\n%s' "$timings"
printf '  %-30s %9s\n' "total" "$(duration $(( $(date +%s) - script_start )))"
if (( failures > 0 )); then
  echo "${failures} check(s) failed" >&2
  exit 1
fi
echo "cluster ${CLUSTER_NAME} is ready; later scripts read ./env.sh"
