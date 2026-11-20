#!/usr/bin/env bash
# The AWS CLI version of the EC2 console's Launch instance form (the post launches from the console;
# this is the same request). Ubuntu 24.04 arm64, t4g.small, 30 GB gp3, no key pair, the no-ingress group.
. "$(dirname "$0")/common.sh"
[ -n "${SG_ID:-}" ] || { echo "run 01_prereqs.sh first"; exit 1; }

AMI=$(aws ssm get-parameter --name "$AMI_PARAM" --query Parameter.Value --output text)
ID=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
      --security-group-ids "$SG_ID" --iam-instance-profile "Name=$PROFILE" \
      --metadata-options HttpTokens=required,HttpPutResponseHopLimit=2,HttpEndpoint=enabled \
      --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=30,VolumeType=gp3,DeleteOnTermination=true}' \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=$TAG_KEY,Value=$TAG_VAL}]" \
                           "ResourceType=volume,Tags=[{Key=$TAG_KEY,Value=$TAG_VAL}]" \
      --query 'Instances[0].InstanceId' --output text)
save_state INSTANCE_ID "$ID"
echo "launched a $INSTANCE_TYPE"
"$HERE/03_wait_ssm.sh"
