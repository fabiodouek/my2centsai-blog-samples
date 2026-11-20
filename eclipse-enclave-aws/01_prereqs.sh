#!/usr/bin/env bash
# The two things the instance needs before launch, with the AWS CLI:
#   1. a security group in the default VPC with NO inbound rules
#   2. an instance role (SSM + Bedrock invoke) and its instance profile
. "$(dirname "$0")/common.sh"
TAGS="Key=$TAG_KEY,Value=$TAG_VAL"

VPC=$(aws ec2 describe-vpcs --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)
echo "default VPC found"

SG=$(aws ec2 create-security-group --group-name "$SG_NAME" --vpc-id "$VPC" \
      --description "Enclave eval: no inbound rules" \
      --tag-specifications "ResourceType=security-group,Tags=[{$TAGS}]" --query GroupId --output text)
save_state SG_ID "$SG"
# create-security-group adds only the default egress rule; there is no ingress rule to remove.
echo "security group created, inbound rules: $(aws ec2 describe-security-groups --group-ids "$SG" --query 'length(SecurityGroups[0].IpPermissions)' --output text)"

aws iam create-role --role-name "$ROLE" --tags "$TAGS" --assume-role-policy-document '{
  "Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
aws iam attach-role-policy --role-name "$ROLE" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam put-role-policy --role-name "$ROLE" --policy-name bedrock-invoke --policy-document '{
  "Version":"2012-10-17","Statement":[{"Effect":"Allow",
    "Action":["bedrock:InvokeModel","bedrock:InvokeModelWithResponseStream"],
    "Resource":["arn:aws:bedrock:*::foundation-model/anthropic.claude-haiku-4-5-*",
                "arn:aws:bedrock:*:*:inference-profile/*.anthropic.claude-haiku-4-5-*"]}]}'
aws iam create-instance-profile --instance-profile-name "$PROFILE" --tags "$TAGS" >/dev/null
aws iam add-role-to-instance-profile --instance-profile-name "$PROFILE" --role-name "$ROLE"
echo "role and instance profile created: $PROFILE"
sleep 10   # IAM is eventually consistent; the launch needs the profile to be visible
