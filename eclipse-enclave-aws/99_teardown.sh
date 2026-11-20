#!/usr/bin/env bash
# Delete everything this example created, then check that nothing is left.
. "$(dirname "$0")/common.sh"
set +e

echo "== terminate the instance"
if [ -n "${INSTANCE_ID:-}" ]; then
  aws ec2 terminate-instances --instance-ids "$INSTANCE_ID" --query 'TerminatingInstances[0].CurrentState.Name' --output text
  aws ec2 wait instance-terminated --instance-ids "$INSTANCE_ID" && echo terminated
fi

echo "== security group, instance profile, role"
for i in 1 2 3 4 5 6; do aws ec2 delete-security-group --group-id "${SG_ID:-none}" 2>/dev/null && { echo "security group deleted"; break; }; sleep 10; done
aws iam remove-role-from-instance-profile --instance-profile-name "$PROFILE" --role-name "$ROLE" 2>/dev/null
aws iam delete-instance-profile --instance-profile-name "$PROFILE" 2>/dev/null && echo "instance profile deleted"
aws iam detach-role-policy --role-name "$ROLE" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore 2>/dev/null
aws iam delete-role-policy --role-name "$ROLE" --policy-name bedrock-invoke 2>/dev/null
aws iam delete-role --role-name "$ROLE" 2>/dev/null && echo "role deleted"

echo; echo "== what is left (each line should say none)"
echo "instances not terminated: $(aws ec2 describe-instances --filters "Name=tag:Name,Values=$NAME" --query 'length(Reservations[].Instances[?State.Name!=`terminated`][])' --output text)"
echo "volumes with the instance's id:  $(aws ec2 describe-volumes --filters "Name=attachment.instance-id,Values=${INSTANCE_ID:-none}" --query 'length(Volumes)' --output text)"
echo "security groups named $SG_NAME:  $(aws ec2 describe-security-groups --filters "Name=group-name,Values=$SG_NAME" --query 'length(SecurityGroups)' --output text)"
echo "roles named $ROLE:  $(aws iam list-roles --query "length(Roles[?RoleName=='$ROLE'])" --output text)"
echo "instance profiles named $PROFILE:  $(aws iam list-instance-profiles --query "length(InstanceProfiles[?InstanceProfileName=='$PROFILE'])" --output text)"
rm -f "$STATE"
