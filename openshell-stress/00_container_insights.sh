#!/usr/bin/env bash
# Container Insights (the Amazon CloudWatch Observability add-on, metrics only) for the stress
# tests, through part 2's Terraform (var.container_insights). It bills per metric observation,
# so turn it off after the runs.
#
#   ./00_container_insights.sh on    plan, show the summary, apply that plan
#   ./00_container_insights.sh off   the same with container_insights=false (removes the agent)
#
# It passes the same region and API-endpoint CIDR (your current public IP) as
# ../openshell-eks/01_cluster.sh, so nothing else in the plan should change: read the plan
# summary before it applies. The add-on runs a cloudwatch-agent DaemonSet, which takes one Pod
# slot on each node (part of what 01_density.py measures against).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL="${HERE}/../openshell-eks"
case "${1:-}" in on) want=true ;; off) want=false ;; *) echo "usage: $0 on|off" >&2; exit 2 ;; esac
# shellcheck source=/dev/null
source "${INSTALL}/env.sh"
MY_IP="${MY_IP:-$(curl -fsS -m 10 https://checkip.amazonaws.com || true)}"
[[ "$MY_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "could not work out your public IP: set MY_IP" >&2; exit 1; }
mkdir -p "${HERE}/logs"
plan="${HERE}/logs/container-insights.tfplan"

terraform -chdir="${INSTALL}/terraform" plan -input=false -no-color -out="$plan" \
  -var "region=${AWS_REGION}" -var "public_access_cidr=${MY_IP}/32" -var "container_insights=${want}" \
  >"${HERE}/logs/container-insights-plan.log"
grep -E '^(Plan:|No changes)|^  # ' "${HERE}/logs/container-insights-plan.log" || true
read -r -p "apply this plan? [y/N] " ok
[[ "$ok" == y ]] || { echo "not applied"; rm -f "$plan"; exit 0; }
terraform -chdir="${INSTALL}/terraform" apply -input=false -no-color "$plan" | grep -E '^(Apply complete|Error)' || true
rm -f "$plan"
kubectl --context "$KUBE_CONTEXT" -n amazon-cloudwatch get pods 2>/dev/null || true
