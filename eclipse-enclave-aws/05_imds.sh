#!/usr/bin/env bash
# Can a container get the instance role's credentials from the metadata service (IMDS)?
# Three cases at hop limit 2 (what the console launch form set), then a plain container at hop limit 1.
. "$(dirname "$0")/common.sh"
hop() { aws ec2 modify-instance-metadata-options --instance-id "$INSTANCE_ID" --http-put-response-hop-limit "$1" \
          --query 'InstanceId' --output text >/dev/null; sleep 5; echo "   (instance metadata hop limit is now $1)"; }

echo "### hop limit 2"
"$HERE/run_remote.sh" remote/05_imds.sh agent
echo
echo "### plain container, hop limit 1"
hop 1
"$HERE/run_remote.sh" remote/05_imds.sh agent bare 2>&1 | sed -n 1,2p
hop 2
