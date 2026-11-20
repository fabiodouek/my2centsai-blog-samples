#!/usr/bin/env bash
# After a launch (console or CLI): record the instance id and wait until Systems Manager can reach it.
# usage: 03_wait_ssm.sh [instance-id]   (without an argument, finds the running instance by its Name tag)
. "$(dirname "$0")/common.sh"
if [ -n "${1:-}" ]; then INSTANCE_ID="$1"; else
  INSTANCE_ID=$(aws ec2 describe-instances --filters "Name=tag:Name,Values=$NAME" Name=instance-state-name,Values=pending,running \
                --query 'Reservations[0].Instances[0].InstanceId' --output text)
fi
save_state INSTANCE_ID "$INSTANCE_ID"
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID"
echo "instance running; waiting for the SSM agent"
for _ in $(seq 1 60); do
  s=$(aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$INSTANCE_ID" --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)
  [ "$s" = Online ] && { echo "SSM: Online"; exit 0; }; sleep 5
done
echo "SSM agent did not come online" >&2; exit 1
