#!/usr/bin/env bash
# Shared settings for the eclipse-enclave-aws scripts. Source it, do not run it.
set -euo pipefail

export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION"
export AWS_PAGER=""

NAME="enclave-eval"                  # name prefix for everything created
TAG_KEY="Project"; TAG_VAL="eclipse-enclave-aws"   # every resource carries this tag
INSTANCE_TYPE="t4g.small"            # Graviton2, 2 vCPU, 2 GiB
AMI_PARAM="/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id"
ROLE="$NAME-role"; PROFILE="$NAME-profile"; SG_NAME="$NAME-no-ingress"
MODEL="${MODEL:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE="$HERE/.state"                 # INSTANCE_ID=... written by the launch step (git-ignored)
# shellcheck disable=SC1090
[ -f "$STATE" ] && . "$STATE"

save_state() { grep -v "^$1=" "$STATE" 2>/dev/null > "$STATE.tmp" || true; echo "$1=$2" >> "$STATE.tmp"; mv "$STATE.tmp" "$STATE"; }

# Run a script on the instance through SSM Run Command (no inbound port, no SSH key)
# and print its output.  usage: ssm_run <local script> [as-user] [script arguments]
ssm_run() {
  local script="$1" user="${2:-root}" args="${3:-}" id out status
  local body; body="$(base64 < "$script" | tr -d '\n')"
  if [ "$user" = root ]; then runner="bash"; else runner="sudo -iu $user bash"; fi
  id=$(aws ssm send-command --instance-ids "$INSTANCE_ID" --document-name AWS-RunShellScript \
        --comment "$(basename "$script")" --timeout-seconds 1800 \
        --parameters "commands=[\"echo $body | base64 -d > /tmp/job.sh; chmod 755 /tmp/job.sh; $runner /tmp/job.sh $args 2>&1\"]" \
        --query Command.CommandId --output text)
  while :; do
    status=$(aws ssm get-command-invocation --command-id "$id" --instance-id "$INSTANCE_ID" --query Status --output text 2>/dev/null || echo Pending)
    case "$status" in Pending|InProgress|Delayed) sleep 3;; *) break;; esac
  done
  aws ssm get-command-invocation --command-id "$id" --instance-id "$INSTANCE_ID" --query StandardOutputContent --output text
  [ "$status" = Success ] || { echo "[ssm] $(basename "$script") finished with status $status" >&2; return 1; }
}
