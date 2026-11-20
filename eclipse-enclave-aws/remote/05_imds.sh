#!/usr/bin/env bash
# On the instance, as the agent user: can code in a container get the instance role's credentials from IMDS?
# usage: 05_imds.sh [bare|all]   (bare = only the plain `docker run` case)
set -uo pipefail
cd ~/project
MODE="${1:-all}"

cat > ~/project/imds.sh <<'PROBE'
T=$(curl -s -m 4 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)
if [ -z "$T" ] || [ "${T:0:6}" = "Domain" ]; then echo "no token (${T:-timed out})"; exit 0; fi
R=$(curl -s -m 4 -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/iam/security-credentials/)
C=$(curl -s -m 4 -H "X-aws-ec2-metadata-token: $T" http://169.254.169.254/latest/meta-data/iam/security-credentials/$R)
echo "GOT CREDENTIALS for role $R: $(echo "$C" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(len(d["AccessKeyId"]),"char key id,",len(d["Token"]),"char session token")')"
PROBE

echo "-- A. plain docker run, no Enclave"
docker run --rm -v ~/project:/p:ro --entrypoint bash enclave-claude:latest /p/imds.sh 2>&1 | tail -2
[ "$MODE" = bare ] && exit 0

echo "-- B. Enclave, restricted mode (the default)"
script -qec "enclave shell -- -c 'bash /home/agent/project/imds.sh'" /dev/null 2>&1 | grep -E 'GOT|no token'
echo "-- C. Enclave with --allow-all-network"
script -qec "enclave shell --allow-all-network -- -c 'bash /home/agent/project/imds.sh'" /dev/null 2>&1 | grep -E 'GOT|no token'
