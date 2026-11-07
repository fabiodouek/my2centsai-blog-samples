#!/usr/bin/env bash
# Remove everything this directory created, then check that nothing was left behind.
# Run it once, at the end of the series. It asks you to type the cluster name first;
# `./99_teardown.sh --yes` skips the question.
#
# The order matters. Kubernetes controllers create AWS resources the Terraform does not know about
# (a load balancer and its security group, EBS volumes, network interfaces), and only those
# controllers can delete them, so they have to be running while their objects are deleted.
# Steps (the phase numbers in the output):
#   0. the node group is brought back to 2 nodes if it is at 0 (./08_scale.sh nodes-only)
#   1. all sandboxes
#   2. the gateway's NLB Service, and wait for the NLB to disappear
#   3. the console, then the Helm releases: gateway, server, controller, base, kata-deploy, and
#      last the AWS Load Balancer Controller
#   4. the server's PVC, and wait for its EBS volume to disappear
#   5. the namespaces, the 7 CRDs Helm keeps, and the gp3-encrypted StorageClass
#   6. every ECR repository under opensandbox-eks/ (with --force: the images go too)
#   7. terraform destroy (cluster, node group, VPC, IAM roles, ECR settings)
#   8. a check for leftovers: network interfaces and security groups in the VPC, load balancers,
#      EBS volumes of this cluster, ECR repositories. The script exits 1 if it finds any.
#   9. a check that the registry's ECR scanning configuration is back at the default (basic, no rules)
#
# Safe to run again after a failure: each step skips what is already gone. When the cluster no
# longer exists, steps 1 to 6 are skipped.
#
# MY_IP is the address that terraform apply used for the EKS API allow-list; terraform destroy needs
# the same -var values. It defaults to your current public IP (checkip.amazonaws.com).
set -Eeuo pipefail
cd "$(dirname "$0")"

ASSUME_YES=0
case "${1:-}" in
  --yes|-y) ASSUME_YES=1 ;;
  "") ;;
  *) echo "usage: $0 [--yes]" >&2; exit 2 ;;
esac

for tool in terraform aws kubectl helm jq curl; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
[[ -f env.sh ]] || { echo "env.sh not found: run ./01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
: "${KUBE_CONTEXT:?env.sh has no KUBE_CONTEXT: run ./01_cluster.sh again}"
: "${CLUSTER_NAME:?env.sh has no CLUSTER_NAME: run ./01_cluster.sh again}"
: "${AWS_REGION:?env.sh has no AWS_REGION: run ./01_cluster.sh again}"
: "${ECR_PREFIX:?env.sh has no ECR_PREFIX: run ./01_cluster.sh again}"
: "${ECR_REGISTRY:?env.sh has no ECR_REGISTRY: run ./01_cluster.sh again}"
: "${NODEGROUP_NAME:?env.sh has no NODEGROUP_NAME: run ./01_cluster.sh again}"
: "${VPC_ID:?env.sh has no VPC_ID: run ./01_cluster.sh again}"
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
helm() { command helm --kube-context "$KUBE_CONTEXT" "$@"; }
aws() { command aws --region "$AWS_REGION" "$@"; }

SYSTEM_NS="opensandbox-system"
SANDBOX_NS="opensandbox"
DATAPLANE_NS="opensandbox-dataplane"
GATEWAY_SERVICE="opensandbox-gateway-nlb"
PVC="opensandbox-server-data"
STORAGE_CLASS="gp3-encrypted"
MANAGE_ECR_SCAN_ON_PUSH="${MANAGE_ECR_SCAN_ON_PUSH:-true}"
LOGS="logs"
mkdir -p "$LOGS"
trap 'exit 130' INT TERM
# Any command that fails under set -e ends the script here with a hint (every step skips what is already gone).
# Only the main shell prints it: set -E also runs the trap inside $(...), where a failure can be the expected
# answer (the "is the NLB gone yet?" probes), and the script goes on. A real failure still reaches the main shell.
trap 'if (( BASH_SUBSHELL == 0 )); then echo "teardown stopped at line ${LINENO} (the command above failed). Fix that, then run ./99_teardown.sh again: finished steps are skipped." >&2; fi' ERR

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
  timings+="$(printf '  %-40s %9s' "$phase_name" "$(duration "$took")")"$'\n'
  printf '   took %s\n' "$(duration "$took")"
}
failures=0
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }

indent() { sed 's/^/        /'; }

# Waits up to $1 seconds for the command in the rest of the arguments to succeed.
wait_for() {
  local limit="$1" waited=0
  shift
  until "$@"; do
    if (( waited >= limit )); then return 1; fi
    sleep 10
    waited=$((waited + 10))
  done
}

# --- what is there -------------------------------------------------------------
MY_IP="${MY_IP:-$(curl -fsS -m 10 https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)}"
if [[ ! "$MY_IP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
  echo "could not work out your public IPv4 address (got '${MY_IP}'): set MY_IP and run again" >&2
  exit 1
fi
cluster_exists=0
if describe_out="$(aws eks describe-cluster --name "$CLUSTER_NAME" 2>&1)"; then
  cluster_exists=1
elif [[ "$describe_out" != *ResourceNotFoundException* ]]; then
  echo "could not ask AWS whether cluster ${CLUSTER_NAME} exists: ${describe_out//$'\n'/ }" >&2
  exit 1
fi

# The caller must be the account that holds the cluster: another profile would find nothing and report success.
# The account is read from env.sh (the ECR registry host starts with it), so this holds on a re-run after the
# cluster is gone, too. Nothing of the account number is printed.
caller="$(aws sts get-caller-identity --query Account --output text)"
env_account="${ECR_REGISTRY%%.*}"
if [[ ! "$env_account" =~ ^[0-9]{12}$ ]]; then
  echo "env.sh has no usable ECR_REGISTRY to take the account from: run ./01_cluster.sh again" >&2
  exit 1
fi
if [[ "$caller" != "$env_account" ]]; then
  echo "account mismatch: the current AWS profile is not the account env.sh was written for (use the profile that created the cluster, AWS_PROFILE)" >&2
  exit 1
fi
if (( cluster_exists )); then
  cluster_account="$(jq -r '.cluster.arn | split(":")[4]' <<<"$describe_out")"
  if [[ "$cluster_account" != "$caller" ]]; then
    echo "account mismatch: the cluster ${CLUSTER_NAME} is not in the current profile's account" >&2
    exit 1
  fi
fi
echo "caller account matches env.sh and the cluster"

echo "This deletes, in ${AWS_REGION}: the EKS cluster ${CLUSTER_NAME}, its VPC and node group, the load balancer, the EBS volume,"
echo "every ECR repository named ${ECR_PREFIX}/*, and the registry's scan-on-push rule."
if (( ! cluster_exists )); then echo "The cluster ${CLUSTER_NAME} does not exist (any more): the Kubernetes steps are skipped."; fi
if (( ! ASSUME_YES )); then
  read -r -p "Type the cluster name (${CLUSTER_NAME}) to go on: " answer
  [[ "$answer" == "$CLUSTER_NAME" ]] || { echo "not confirmed: nothing deleted"; exit 1; }
fi

# ===============================================================================
if (( cluster_exists )); then
  phase "nodes"
  desired="$(aws eks describe-nodegroup --cluster-name "$CLUSTER_NAME" --nodegroup-name "$NODEGROUP_NAME" \
    --query nodegroup.scalingConfig.desiredSize --output text 2>/dev/null || echo none)"
  if [[ "$desired" == "0" ]]; then
    echo "  the node group is scaled to 0, and the controllers that delete the load balancer and the volume need nodes: scaling up"
    if ! ./08_scale.sh nodes-only; then
      echo "could not bring the nodes back. Scale the node group to 2 by hand (aws eks update-nodegroup-config, or ./08_scale.sh nodes-only after fixing the error above), then run ./99_teardown.sh again" >&2
      exit 1
    fi
  else
    ok "node group desired size ${desired}"
  fi
  kubectl get --raw /readyz >/dev/null 2>&1 || { echo "cannot reach context '${KUBE_CONTEXT}'. Is your IP still ${MY_IP}? Run ./01_cluster.sh to refresh the allow-list, or set MY_IP" >&2; exit 1; }
  phase_done

  phase "1. delete all sandboxes"
  # The BatchSandbox objects are the sandboxes; the controller deletes their pods. Done with kubectl so that no
  # port-forward is needed (98_delete_sandboxes.py does the same through the server API).
  if kubectl get crd batchsandboxes.sandbox.opensandbox.io >/dev/null 2>&1; then
    count="$(kubectl -n "$SANDBOX_NS" get batchsandboxes.sandbox.opensandbox.io --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    kubectl -n "$SANDBOX_NS" delete batchsandboxes.sandbox.opensandbox.io --all --wait --timeout=5m 2>&1 | sed 's/^/  /' || true
    left_now="$(kubectl -n "$SANDBOX_NS" get batchsandboxes.sandbox.opensandbox.io --no-headers 2>/dev/null | wc -l | tr -d ' ')"
    echo "  ${count} sandbox(es) found, deletion requested; ${left_now} left"
  else
    echo "  no BatchSandbox CRD: no sandboxes"
  fi
  no_sandbox_pods() { [[ -z "$(kubectl -n "$SANDBOX_NS" get pods -o name 2>/dev/null)" ]]; }
  if wait_for 180 no_sandbox_pods; then ok "no pods in ${SANDBOX_NS}"; else warn "pods still in ${SANDBOX_NS}; the namespace delete below removes them"; fi
  phase_done

  phase "2. the gateway's NLB"
  nlb_host="$(kubectl -n "$SYSTEM_NS" get svc "$GATEWAY_SERVICE" -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  nlb_arn=""
  if [[ -n "$nlb_host" ]]; then
    nlb_arn="$(aws elbv2 describe-load-balancers --query "LoadBalancers[?DNSName=='${nlb_host}'].LoadBalancerArn | [0]" --output text 2>/dev/null || true)"
    [[ "$nlb_arn" != None ]] || nlb_arn=""
  fi
  kubectl -n "$SYSTEM_NS" delete svc "$GATEWAY_SERVICE" --ignore-not-found --wait --timeout=5m 2>&1 | sed 's/^/  /'
  if [[ -n "$nlb_arn" ]]; then
    # Gone only when AWS says so: any other error (expired credentials, throttling) means keep waiting.
    nlb_gone() { local e; e="$(aws elbv2 describe-load-balancers --load-balancer-arns "$nlb_arn" 2>&1 >/dev/null)" && return 1; [[ "$e" == *LoadBalancerNotFound* ]]; }
    if wait_for 300 nlb_gone; then ok "the NLB is gone"; else fail "the NLB still exists 5 minutes after its Service was deleted"; fi
  else
    ok "no NLB for ${GATEWAY_SERVICE}"
  fi
  phase_done

  phase "3. uninstall the console and the Helm releases"
  # The console has no Helm release: delete what 06_console.sh applied.
  kubectl -n "$SYSTEM_NS" delete deployment/opensandbox-console-web deployment/opensandbox-console-bff \
    svc/opensandbox-console-web svc/opensandbox-console-bff secret/opensandbox-console-bff secret/opensandbox-tenants \
    serviceaccount/opensandbox-console-bff --ignore-not-found 2>&1 | sed 's/^/  /'
  kubectl delete clusterrolebinding/opensandbox-console-bff-readonly clusterrole/opensandbox-console-bff-readonly --ignore-not-found 2>&1 | sed 's/^/  /'
  uninstall() {  # uninstall <release> <namespace>
    if helm -n "$2" status "$1" >/dev/null 2>&1; then
      if helm -n "$2" uninstall "$1" --wait --timeout 10m >"$LOGS/helm-uninstall-$1.log" 2>&1; then
        ok "uninstalled $1"
      else
        tail -n 5 "$LOGS/helm-uninstall-$1.log" | sed 's/^/        /'
        fail "helm uninstall $1 failed (log: $LOGS/helm-uninstall-$1.log)"
      fi
    else
      ok "$1 not installed"
    fi
  }
  uninstall opensandbox-ingress-gateway "$SYSTEM_NS"
  uninstall opensandbox-server "$SYSTEM_NS"
  uninstall opensandbox-controller "$SYSTEM_NS"
  uninstall opensandbox-base "$SYSTEM_NS"
  uninstall kata-deploy kube-system
  # Last: it deletes the NLB and target groups of any Service of type LoadBalancer that is left.
  uninstall aws-load-balancer-controller kube-system
  phase_done

  phase "4. the PVC and its EBS volume"
  pv="$(kubectl -n "$SYSTEM_NS" get pvc "$PVC" -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)"
  vol=""
  if [[ -n "$pv" ]]; then vol="$(kubectl get pv "$pv" -o jsonpath='{.spec.csi.volumeHandle}' 2>/dev/null || true)"; fi
  kubectl -n "$SYSTEM_NS" delete pvc --all --ignore-not-found --wait --timeout=5m 2>&1 | sed 's/^/  /'
  if [[ -n "$vol" ]]; then
    vol_gone() { local e; e="$(aws ec2 describe-volumes --volume-ids "$vol" 2>&1 >/dev/null)" && return 1; [[ "$e" == *InvalidVolume.NotFound* ]]; }
    if wait_for 300 vol_gone; then ok "EBS volume ${vol} is gone"; else fail "EBS volume ${vol} still exists 5 minutes after its PVC was deleted"; fi
  else
    ok "no PVC volume to wait for"
  fi
  phase_done

  phase "5. namespaces, CRDs, StorageClass"
  kubectl delete ns "$SANDBOX_NS" "$DATAPLANE_NS" "$SYSTEM_NS" --ignore-not-found --wait --timeout=5m 2>&1 | sed 's/^/  /' || warn "a namespace is still terminating"
  # Helm never deletes the CRDs it installed from a chart's crds/ directory.
  crds="$(kubectl get crd -o name 2>/dev/null | { grep -E '\.(sandbox\.opensandbox\.io|sandbox\.fast\.io)$' || true; })"
  if [[ -n "$crds" ]]; then
    # shellcheck disable=SC2086
    kubectl delete $crds --wait --timeout=3m 2>&1 | sed 's/^/  /' || warn "a CRD did not delete"
  fi
  left_crds="$(kubectl get crd -o name 2>/dev/null | { grep -cE '\.(sandbox\.opensandbox\.io|sandbox\.fast\.io)$' || true; })"
  if [[ "$left_crds" == 0 ]]; then ok "no OpenSandbox CRDs left"; else fail "${left_crds} OpenSandbox CRDs left"; fi
  kubectl delete storageclass "$STORAGE_CLASS" --ignore-not-found 2>&1 | sed 's/^/  /'
  phase_done
fi

# ===============================================================================
phase "6. ECR repositories under ${ECR_PREFIX}/"
repos="$(aws ecr describe-repositories --query "repositories[?starts_with(repositoryName, '${ECR_PREFIX}/')].repositoryName" --output text)"
count=0
for repo in $repos; do
  aws ecr delete-repository --repository-name "$repo" --force >/dev/null
  echo "  deleted ${repo}"
  count=$((count + 1))
done
ok "${count} repositories deleted (delete-repository returned success for each)"
phase_done

# ===============================================================================
phase "7. terraform destroy"
echo "  (full output: ${LOGS}/terraform-destroy.log)"
if ! terraform -chdir=terraform init -input=false -no-color >"$LOGS/terraform-init.log" 2>&1; then
  tail -n 20 "$LOGS/terraform-init.log" >&2
  exit 1
fi
# The ERR trap is cleared here: on bash 3.2 a trap that runs after a failed pipeline overwrites PIPESTATUS.
trap - ERR
set +e
terraform -chdir=terraform destroy -input=false -auto-approve -no-color \
  -var "region=${AWS_REGION}" \
  -var "public_access_cidr=${MY_IP}/32" \
  -var "manage_ecr_scan_on_push=${MANAGE_ECR_SCAN_ON_PUSH}" 2>&1 \
  | tee "$LOGS/terraform-destroy.log" \
  | awk '/^(Plan:|Destroy complete!)|(Destruction) complete after|Still [a-z]+\.\.\. \[[0-9]+m0?0s elapsed\]|(Error|Warning): / {
           sub(/ \[id=[^]]*\]$/, ""); print; fflush() }'
destroy_rc=${PIPESTATUS[0]}
set -e
trap 'if (( BASH_SUBSHELL == 0 )); then echo "teardown stopped at line ${LINENO} (the command above failed). Fix that, then run ./99_teardown.sh again: finished steps are skipped." >&2; fi' ERR
if (( destroy_rc != 0 )); then
  echo "terraform destroy failed (exit ${destroy_rc}); the end of ${LOGS}/terraform-destroy.log:" >&2
  tail -n 30 "$LOGS/terraform-destroy.log" >&2
  echo "Fix the error and run this script again; the steps above skip what is already gone." >&2
  exit 1
fi
phase_done

# ===============================================================================
phase "8. leftovers"
# A failed query is a failed check, never an empty answer. Service-created network interfaces (EKS control
# plane, VPC CNI, NAT) carry no tag of ours, so they are found by VPC id; a deleted VPC returns nothing.
check_none() {  # check_none <what> <text output of the query, one line per leftover> (the query's own failure is reported by the caller)
  local what="$1" out="$2"
  if [[ -z "$out" ]]; then ok "no ${what}"; else fail "${what} left:"; printf '%s\n' "$out" | indent; fi
}
query() {  # query <what> <aws arguments...>: sets $found; a failed query is a failed check
  local what="$1"
  shift
  local err_file
  err_file="$(mktemp)"
  if ! found="$(aws "$@" --output text 2>"$err_file")"; then   # stdout is the answer; stderr (warnings) is only for errors
    fail "could not check ${what}: $(tr '\n' ' ' <"$err_file")"
    found=""
    rm -f "$err_file"
    return 1
  fi
  rm -f "$err_file"
}
if query "network interfaces" ec2 describe-network-interfaces --filters "Name=vpc-id,Values=${VPC_ID}" --query 'NetworkInterfaces[].[NetworkInterfaceId,Description]'; then check_none "network interfaces in ${VPC_ID}" "$found"; fi
if query "security groups" ec2 describe-security-groups --filters "Name=vpc-id,Values=${VPC_ID}" --query 'SecurityGroups[].[GroupId,GroupName]'; then check_none "security groups in ${VPC_ID}" "$found"; fi
if query "load balancers" elbv2 describe-load-balancers --query "LoadBalancers[?VpcId=='${VPC_ID}'].[LoadBalancerName,Type]"; then check_none "load balancers in ${VPC_ID}" "$found"; fi
# Only this cluster's volumes: the account may hold volumes of other clusters.
if query "EBS volumes" ec2 describe-volumes --filters "Name=tag-key,Values=kubernetes.io/cluster/${CLUSTER_NAME}" --query 'Volumes[].[VolumeId,State]'; then check_none "EBS volumes tagged kubernetes.io/cluster/${CLUSTER_NAME}" "$found"; fi
if query "ECR repositories" ecr describe-repositories --query "repositories[?starts_with(repositoryName, '${ECR_PREFIX}/')].repositoryName"; then check_none "ECR repositories under ${ECR_PREFIX}/" "$found"; fi
if describe_out="$(aws eks describe-cluster --name "$CLUSTER_NAME" 2>&1)"; then
  fail "EKS cluster ${CLUSTER_NAME} still exists"
elif [[ "$describe_out" == *ResourceNotFoundException* ]]; then
  ok "EKS cluster ${CLUSTER_NAME} is gone"
else
  fail "could not check the EKS cluster: ${describe_out//$'\n'/ }"
fi
phase_done

phase "9. ECR scanning configuration"
scan="$(aws ecr get-registry-scanning-configuration --query scanningConfiguration --output json)"
if jq -e '.scanType == "BASIC" and (.rules | length) == 0' <<<"$scan" >/dev/null; then
  ok "the registry is back at the default: basic scanning, no rules"
elif [[ "$MANAGE_ECR_SCAN_ON_PUSH" == false ]]; then
  warn "the registry has a scanning configuration of its own (this series did not manage it): $(jq -c . <<<"$scan")"
else
  fail "the registry's scanning configuration is not the default: $(jq -c . <<<"$scan")"
fi
phase_done

# The kubeconfig entries are now dead; remove them. The cluster and user entries are named by ARN.
cluster_entry="$(command kubectl config view -o jsonpath="{.contexts[?(@.name=='${KUBE_CONTEXT}')].context.cluster}" 2>/dev/null || true)"
user_entry="$(command kubectl config view -o jsonpath="{.contexts[?(@.name=='${KUBE_CONTEXT}')].context.user}" 2>/dev/null || true)"
if [[ -n "$cluster_entry" ]]; then
  command kubectl config delete-context "$KUBE_CONTEXT" >/dev/null 2>&1 || true
  command kubectl config delete-cluster "$cluster_entry" >/dev/null 2>&1 || true
  [[ -z "$user_entry" ]] || command kubectl config unset "users.${user_entry}" >/dev/null 2>&1 || true
  echo "removed kubeconfig context ${KUBE_CONTEXT}"
fi

printf '\n\033[1mTimings\033[0m\n%s' "$timings"
printf '  %-40s %9s\n' "total" "$(duration $(( $(date +%s) - script_start )))"
if (( failures > 0 )); then printf '\n%d check(s) failed (see FAIL above): something is still running and may cost money\n' "$failures"; exit 1; fi
printf '\nteardown complete: nothing of %s is left in %s\n' "$CLUSTER_NAME" "$AWS_REGION"
