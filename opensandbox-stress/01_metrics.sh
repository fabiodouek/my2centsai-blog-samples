#!/usr/bin/env bash
# Turn Container Insights (the Amazon CloudWatch Observability EKS add-on, metrics only) on for the
# stress test, or off again afterwards. Part 2's Terraform has it behind one variable,
# container_insights (default false), so this is a `terraform plan` of ../opensandbox-eks/terraform
# with that variable flipped, a check of the plan, and an apply of exactly the checked plan.
#
#   ./01_metrics.sh               on: plan, check, apply, then check the agent and note the start
#   ./01_metrics.sh off           off (99_cleanup.py runs this), and note the end
#   ./01_metrics.sh --plan-only   plan and check, apply nothing (works with off too)
#   ./01_metrics.sh --dry-run     print the steps; run no terraform and touch nothing
#
# The other -var values are the ones the cluster was built with, read from the Terraform state, not
# worked out again: the region (and env.sh's AWS_REGION must agree), the EKS endpoint's allow-list
# /32 (part 2's 01_cluster.sh passed your IP at the time; today's IP may differ, and a different
# value would change who can reach the cluster), and whether the ECR scan-on-push rule is managed.
# None of them is printed.
#
# The plan is refused unless every change in it is the add-on's own: the add-on, its IAM role, the
# role's policy attachment and its Pod Identity association (creates when turning on, deletes when
# turning off; tfplan.py says exactly which addresses). A plan that would touch anything else (the
# node group after a new AMI, the endpoint allow-list, an add-on version) stops the script before
# the apply. The apply runs the saved plan file, so it does what was checked and nothing more.
#
# After "on": the add-on is ACTIVE, the cloudwatch-agent DaemonSet in amazon-cloudwatch is Ready on
# every node, and its pods have EKS Pod Identity credentials. results/metrics-window.json gets the
# start time, which 08_cloudwatch.py reads. After "off": the add-on and the DaemonSet are gone.
#
# Container Insights bills per metric observation while it is on; run "off" (or 99_cleanup.py)
# when the tests are done. Full Terraform output goes to logs/ (gitignored).
set -euo pipefail
cd "$(dirname "$0")"

EKS="../opensandbox-eks"
TF="${EKS}/terraform"
ADDON="amazon-cloudwatch-observability"
AGENT_NS="amazon-cloudwatch"
AGENT_DS="cloudwatch-agent"
WINDOW="results/metrics-window.json"
LOGS="logs"

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; }
WANT="on"
MODE="apply"
for arg in "$@"; do
  case "$arg" in
    on|off) WANT="$arg" ;;
    --plan-only) MODE="plan" ;;
    --dry-run) MODE="dry" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "usage: ./01_metrics.sh [on|off] [--plan-only|--dry-run]" >&2; exit 2 ;;
  esac
done
CI_VALUE="true"; ACTION="create"
if [[ "$WANT" == off ]]; then CI_VALUE="false"; ACTION="delete"; fi

if [[ "$MODE" == dry ]]; then
  cat <<EOF
01_metrics.sh ${WANT} (dry run: no terraform, no kubectl, no AWS call)
  checks first:
    - terraform, jq, kubectl, aws and python3 are installed; ../opensandbox-eks/env.sh exists
    - the Terraform state has the cluster, one /32 on the EKS endpoint, and a region equal to env.sh's
  steps:
     1. terraform show -json: read region, public_access_cidr and manage_ecr_scan_on_push (not printed)
     2. terraform plan -var container_insights=${CI_VALUE} (and the three values above), saved to a temp file
     3. tfplan.py check ${WANT}: every planned change must be a ${ACTION} of the add-on, its IAM role,
        the role's policy attachment or its Pod Identity association; otherwise stop here
     4. terraform apply of the saved plan (skipped when the plan has nothing to do)
     5. $( [[ "$WANT" == on ]] && echo "aws eks describe-addon: ACTIVE; DaemonSet ${AGENT_NS}/${AGENT_DS} Ready on every node, with Pod Identity credentials" || echo "aws eks describe-addon: gone; DaemonSet ${AGENT_NS}/${AGENT_DS} gone" )
     6. ${WINDOW}: $( [[ "$WANT" == on ]] && echo "the start of the metrics window (kept if it is already on)" || echo "the end of the metrics window" )
  changes to the cluster: the ${ADDON} add-on $( [[ "$WANT" == on ]] && echo "installed" || echo "removed" ) (with its IAM role and Pod Identity association)
  estimated wall clock: about $( [[ "$WANT" == on ]] && echo 4 || echo 3 ) min
EOF
  exit 0
fi

for tool in terraform jq kubectl aws python3; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
[[ -f "${EKS}/env.sh" ]] || { echo "run ../opensandbox-eks/01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source "${EKS}/env.sh"
: "${KUBE_CONTEXT:?env.sh has no KUBE_CONTEXT}" "${AWS_REGION:?env.sh has no AWS_REGION}"
: "${CLUSTER_NAME:?env.sh has no CLUSTER_NAME}" "${ECR_REGISTRY:?env.sh has no ECR_REGISTRY}"
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
ACCOUNT_ID="${ECR_REGISTRY%%.*}"
# Terraform and AWS errors can name an ARN, the account, the EKS endpoint, a load balancer, the
# endpoint's /32 or a node's IP: mask them all.
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || ACCOUNT_ID="__no_account_id__"
mask() {
  sed -E \
    -e "s/arn:aws[a-zA-Z-]*:[^[:space:]\"',)]+/<arn>/g" \
    -e "s/${ACCOUNT_ID}/<account>/g" \
    -e 's/[0-9A-Fa-f]{32}\.[a-z0-9]+\.[a-z0-9-]+\.eks\.amazonaws\.com/<eks-endpoint>/g' \
    -e 's/[a-z0-9-]+\.elb\.[a-z0-9-]+\.amazonaws\.com/<nlb>/g' \
    -e 's/ip-[0-9]+-[0-9]+-[0-9]+-[0-9]+/<node>/g' \
    -e 's/([0-9]{1,3}\.){3}[0-9]{1,3}/<ip>/g'
}
mkdir -p "$LOGS" results
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
trap 'exit 130' INT TERM

script_start=$(date +%s)
phase_start=0
duration() { local s=$1; if (( s >= 60 )); then printf '%dm %02ds' $((s / 60)) $((s % 60)); else printf '%ds' "$s"; fi; }
phase() { phase_start=$(date +%s); printf '\n\033[1m== %s\033[0m\n' "$1"; }
phase_done() { printf '   took %s\n' "$(duration $(( $(date +%s) - phase_start )))"; }
failures=0
ok()   { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }

# --- 1. what the cluster was built with ----------------------------------------------------------
phase "the -var values of the cluster, from the Terraform state"
if ! terraform -chdir="$TF" init -input=false -no-color >"$LOGS/terraform-init.log" 2>&1; then
  tail -n 20 "$LOGS/terraform-init.log" | mask >&2
  exit 1
fi
terraform -chdir="$TF" show -json >"$WORK/state.json"
if ! python3 tfplan.py vars <"$WORK/state.json" >"$WORK/vars"; then exit 1; fi
region="" cidr="" scan="" ci_now=""
while IFS='=' read -r key value; do
  case "$key" in
    region) region="$value" ;;
    public_access_cidr) cidr="$value" ;;
    manage_ecr_scan_on_push) scan="$value" ;;
    container_insights) ci_now="$value" ;;
  esac
done <"$WORK/vars"
if [[ "$region" != "$AWS_REGION" ]]; then
  echo "the Terraform state's region is not env.sh's AWS_REGION: run ../opensandbox-eks/01_cluster.sh again" >&2
  exit 1
fi
ok "region ${region} (state and env.sh agree)"
ok "EKS endpoint allow-list: the one /32 in the state (not printed; not your current IP)"
ok "manage_ecr_scan_on_push=${scan}"
echo "  Container Insights now: $( [[ "$ci_now" == true ]] && echo on || echo off ); wanted: ${WANT}"
phase_done

# --- 2. plan and check -----------------------------------------------------------------------------
phase "terraform plan -var container_insights=${CI_VALUE}"
set +e
# The /32 goes in through the environment (TF_VAR_...), not the command line, so `ps` does not show it.
TF_VAR_public_access_cidr="$cidr" terraform -chdir="$TF" plan -input=false -no-color -lock-timeout=120s \
  -out="$WORK/metrics.tfplan" \
  -var "region=${region}" \
  -var "manage_ecr_scan_on_push=${scan}" \
  -var "container_insights=${CI_VALUE}" >"$LOGS/terraform-plan-metrics-${WANT}.log" 2>&1
plan_rc=$?
set -e
unset cidr
if (( plan_rc != 0 )); then
  echo "terraform plan failed (exit ${plan_rc}); the end of ${LOGS}/terraform-plan-metrics-${WANT}.log:" >&2
  tail -n 30 "$LOGS/terraform-plan-metrics-${WANT}.log" | mask >&2
  exit 1
fi
grep -E '^(Plan:|No changes\.)' "$LOGS/terraform-plan-metrics-${WANT}.log" | sed 's/^/  /' || true
terraform -chdir="$TF" show -json "$WORK/metrics.tfplan" >"$WORK/plan.json"
set +e
python3 tfplan.py check "$WANT" <"$WORK/plan.json"
check_rc=$?
set -e
phase_done
case "$check_rc" in
  0) ;;
  3) echo "nothing to apply" ;;
  *) echo "the plan changes more than the CloudWatch add-on: not applied (full plan: ${LOGS}/terraform-plan-metrics-${WANT}.log)" >&2
     exit 1 ;;
esac
if [[ "$MODE" == plan ]]; then
  echo "--plan-only: nothing applied"
  exit 0
fi

# --- 3. apply the checked plan -------------------------------------------------------------------
if (( check_rc == 0 )); then
  phase "terraform apply (the saved plan, nothing else)"
  set +e
  terraform -chdir="$TF" apply -input=false -no-color -auto-approve -lock-timeout=120s "$WORK/metrics.tfplan" 2>&1 \
    | tee "$LOGS/terraform-apply-metrics-${WANT}.log" \
    | awk '/^(Apply complete!)|(Creation|Destruction) complete after|Still [a-z]+\.\.\. \[[0-9]+m0?0s elapsed\]|(Error|Warning): / {
             sub(/ \[id=[^]]*\]$/, ""); print; fflush() }' | mask
  apply_rc=${PIPESTATUS[0]}
  set -e
  if (( apply_rc != 0 )); then
    echo "terraform apply failed (exit ${apply_rc}); the end of ${LOGS}/terraform-apply-metrics-${WANT}.log:" >&2
    tail -n 30 "$LOGS/terraform-apply-metrics-${WANT}.log" | mask >&2
    exit 1
  fi
  phase_done
fi

# --- 4. checks -------------------------------------------------------------------------------------
phase "checks"
addon_status="$(aws eks describe-addon --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" --addon-name "$ADDON" \
  --query addon.status --output text 2>"$WORK/addon.err" || true)"
if [[ -z "$addon_status" ]] && grep -q ResourceNotFoundException "$WORK/addon.err"; then addon_status="none"; fi
nodes="$(kubectl get nodes -o name | wc -l | tr -d ' ')"
if [[ "$WANT" == on ]]; then
  if [[ "$addon_status" == ACTIVE ]]; then ok "add-on ${ADDON} ACTIVE"; else fail "add-on ${ADDON} is ${addon_status:-unknown} (expected ACTIVE)"; fi
  # The operator creates the agent's DaemonSet a little after the add-on turns ACTIVE.
  for _ in $(seq 1 30); do
    kubectl -n "$AGENT_NS" get daemonset "$AGENT_DS" >/dev/null 2>&1 && break
    sleep 5
  done
  kubectl -n "$AGENT_NS" rollout status "daemonset/${AGENT_DS}" --timeout=5m >/dev/null 2>&1 || true
  ready="$(kubectl -n "$AGENT_NS" get daemonset "$AGENT_DS" -o json 2>/dev/null \
    | jq -r '"\(.status.numberReady // 0)/\(.status.desiredNumberScheduled // 0)"' || echo "0/0")"
  if [[ "$ready" == "${nodes}/${nodes}" ]]; then ok "DaemonSet ${AGENT_NS}/${AGENT_DS} ${ready} Ready"; else fail "DaemonSet ${AGENT_NS}/${AGENT_DS} ${ready} Ready (expected ${nodes}/${nodes})"; fi
  creds="$(kubectl -n "$AGENT_NS" get pods -o json 2>/dev/null | jq --arg ds "$AGENT_DS" '[.items[]
    | select(any(.metadata.ownerReferences[]?; .kind == "DaemonSet" and .name == $ds))
    | select(any(.spec.containers[].env[]?; .name == "AWS_CONTAINER_CREDENTIALS_FULL_URI"))] | length' || echo 0)"
  if [[ "$creds" == "$nodes" ]]; then ok "${creds} agent pod(s) with EKS Pod Identity credentials"; else fail "${creds} of ${nodes} agent pods have Pod Identity credentials"; fi
  if [[ -f "$WINDOW" && "$ci_now" == true ]] && jq -e '.start and (.end == null)' "$WINDOW" >/dev/null 2>&1; then
    ok "metrics window kept: started $(jq -r .start "$WINDOW")"
  else
    jq -n --arg start "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --arg cluster "$CLUSTER_NAME" \
      '{start: $start, end: null, cluster: $cluster}' >"$WINDOW"
    ok "metrics window starts $(jq -r .start "$WINDOW") (${WINDOW})"
  fi
else
  if [[ "$addon_status" == none ]]; then ok "add-on ${ADDON} removed"; else fail "add-on ${ADDON} is ${addon_status:-unknown} (expected removed)"; fi
  gone=0
  for _ in $(seq 1 36); do
    if ! kubectl -n "$AGENT_NS" get daemonset "$AGENT_DS" >/dev/null 2>&1; then gone=1; break; fi
    sleep 5
  done
  if (( gone )); then ok "DaemonSet ${AGENT_NS}/${AGENT_DS} gone"; else fail "DaemonSet ${AGENT_NS}/${AGENT_DS} still exists 3 minutes after the add-on was removed"; fi
  if kubectl get namespace "$AGENT_NS" >/dev/null 2>&1; then
    echo "  note  the namespace ${AGENT_NS} is still there (the add-on leaves it); it holds no agent"
  fi
  if [[ -f "$WINDOW" ]]; then
    jq --arg end "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '.end = (.end // $end)' "$WINDOW" >"$WORK/window" && mv "$WORK/window" "$WINDOW"
    ok "metrics window: $(jq -r '"\(.start) to \(.end)"' "$WINDOW")"
  fi
fi
phase_done

printf '\n  %-20s %9s\n' total "$(duration $(( $(date +%s) - script_start )))"
if (( failures > 0 )); then echo "${failures} check(s) failed" >&2; exit 1; fi
echo "Container Insights is ${WANT}"
