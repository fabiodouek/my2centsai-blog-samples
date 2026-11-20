#!/usr/bin/env bash
# Prove the instance has no way in: no inbound rule, nothing answers on its public address,
# and Systems Manager (which only needs outbound HTTPS) still reaches it.
. "$(dirname "$0")/common.sh"

echo "== inbound rules on the security group"
aws ec2 describe-security-groups --group-ids "$SG_ID" \
  --query 'SecurityGroups[0].{GroupName:GroupName,InboundRules:length(IpPermissions),OutboundRules:length(IpPermissionsEgress)}' --output table

IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo; echo "== from this machine to the instance's public address (shown as x.x.x.x)"
for port in 22 443 8080; do
  if nc -z -w 5 "$IP" "$port" 2>/dev/null; then r="OPEN"; else r="no answer (timed out)"; fi
  echo "x.x.x.x:$port  $r"
done
echo "curl -m 5 http://x.x.x.x/  ->  $(curl -s -m 5 -o /dev/null -w '%{http_code}' "http://$IP/" 2>/dev/null | sed 's/^000$/no answer (timed out)/')"

echo; echo "== Systems Manager still reaches it, because the agent dials out"
aws ssm describe-instance-information --filters "Key=InstanceIds,Values=$INSTANCE_ID" \
  --query 'InstanceInformationList[0].{Ping:PingStatus,Platform:PlatformName,Agent:AgentVersion}' --output table
