#!/usr/bin/env bash
# Tear the stack down. Run 13_cleanup.py first: the egress connector can't be deleted while MicroVMs use it, and
# the image isn't managed by Terraform. Then this:
#
#   1. stops if anything but iron-proxy runs in the ECS cluster (part 3's API does when part 3 is deployed on this
#      stack; destroy part 3 first);
#   2. switches off the DNS Firewall association's mutation protection (terraform destroy can't remove the
#      association while it's on);
#   3. runs terraform destroy;
#   4. with control_plane=true, force-deletes the credentials part 3 added under user_secret_prefix. Terraform
#      doesn't manage them, nothing can read them once the roles are gone, and a deleted secret's name stays
#      taken for its 7-30 day recovery window.
#
#   scripts/destroy.sh                  # asks for confirmation, like terraform destroy
#   scripts/destroy.sh -auto-approve    # extra arguments go to terraform destroy
set -euo pipefail
cd "$(dirname "$0")/../infra"

region=$(terraform output -raw region 2>/dev/null || echo "${AWS_REGION:-us-east-1}")
assoc=$(terraform output -raw firewall_association_id 2>/dev/null || true)
cluster=$(terraform output -raw ecs_cluster 2>/dev/null || true)
service=$(terraform output -raw ecs_service 2>/dev/null || true)
user_prefix=$(terraform output -raw user_secret_prefix 2>/dev/null || true)
[[ $user_prefix == null ]] && user_prefix=""

if [[ -n "$cluster" ]]; then
  others=$(aws ecs list-services --region "$region" --cluster "$cluster" --query 'serviceArns' --output text 2>/dev/null \
    | tr '\t' '\n' | sed 's|.*/||' | grep -vx "$service" | grep -v '^None$' || true)
  if [[ -n "$others" ]]; then
    echo "Other services still run in the $cluster cluster: $(echo "$others" | tr '\n' ' ')" >&2
    echo "Destroy part 3 first (its scripts/destroy.sh), then run this again." >&2
    exit 1
  fi
fi

if [[ -n "$assoc" ]]; then
  protection=$(aws route53resolver get-firewall-rule-group-association --region "$region" \
    --firewall-rule-group-association-id "$assoc" \
    --query FirewallRuleGroupAssociation.MutationProtection --output text 2>/dev/null || echo GONE)
  if [[ "$protection" == "ENABLED" ]]; then
    echo "Switching off mutation protection on the DNS Firewall association (the alarm will log it)"
    aws route53resolver update-firewall-rule-group-association --region "$region" \
      --firewall-rule-group-association-id "$assoc" --mutation-protection DISABLED >/dev/null
    for _ in $(seq 1 90); do
      read -r status prot < <(aws route53resolver get-firewall-rule-group-association --region "$region" \
        --firewall-rule-group-association-id "$assoc" \
        --query 'FirewallRuleGroupAssociation.[Status,MutationProtection]' --output text)
      [[ "$status" == "COMPLETE" && "$prot" == "DISABLED" ]] && break
      sleep 2
    done
    echo "Mutation protection: $prot ($status)"
  else
    echo "Mutation protection: $protection"
  fi
fi

if ! terraform destroy "$@"; then
  echo "terraform destroy didn't finish. If the association still exists, its mutation protection is now off:" >&2
  echo "run 'terraform -chdir=infra apply' to switch it back on." >&2
  exit 1
fi

if [[ -n "$user_prefix" ]]; then
  for s in $(aws secretsmanager list-secrets --region "$region" --include-planned-deletion \
               --filters "Key=name,Values=$user_prefix" --query 'SecretList[].Name' --output text); do
    # A secret already scheduled for deletion has to be restored before it can be force-deleted
    aws secretsmanager restore-secret --region "$region" --secret-id "$s" >/dev/null 2>&1 || true
    aws secretsmanager delete-secret --region "$region" --secret-id "$s" --force-delete-without-recovery >/dev/null
    echo "Deleted part 3's credential secret $s"
  done
fi
