#!/usr/bin/env bash
# On the instance, as the agent user: what does the agent actually see?
#  1. a declared secret (a dummy ANTHROPIC_API_KEY in Enclave's secrets file): placeholder in the container?
#  2. an AWS credential passed with --pass-env: placeholder or the real value?
set -uo pipefail
cd ~/project
mkdir -p ~/.local/state/enclave/secrets/global
echo 'ANTHROPIC_API_KEY=sk-ant-dummy-not-a-real-key-0123456789' > ~/.local/state/enclave/secrets/global/claude.env
chmod 600 ~/.local/state/enclave/secrets/global/claude.env

cat > ~/project/secrets_probe.sh <<'PROBE'
echo "ANTHROPIC_API_KEY inside the sandbox: ${ANTHROPIC_API_KEY:0:22}... (${#ANTHROPIC_API_KEY} characters)"
echo -n "call to api.anthropic.com with that value: "
curl -s -m 8 https://api.anthropic.com/v1/models -H "x-api-key: $ANTHROPIC_API_KEY" -H 'anthropic-version: 2023-06-01' | head -c 160; echo
if [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  echo "AWS_SECRET_ACCESS_KEY inside the sandbox: ${#AWS_SECRET_ACCESS_KEY} characters, sha256 $(echo -n "$AWS_SECRET_ACCESS_KEY" | sha256sum | cut -c1-12)"
fi
PROBE

# an AWS key from the instance role, exported on the host the way the Bedrock run does
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
ROLE=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/iam/security-credentials/)
CREDS=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/iam/security-credentials/$ROLE)
export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | jq -r .AccessKeyId) AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | jq -r .SecretAccessKey) AWS_SESSION_TOKEN=$(echo "$CREDS" | jq -r .Token)
echo "on the host:                              AWS_SECRET_ACCESS_KEY ${#AWS_SECRET_ACCESS_KEY} characters, sha256 $(echo -n "$AWS_SECRET_ACCESS_KEY" | sha256sum | cut -c1-12)"
echo
script -qec "enclave shell --pass-env AWS_ACCESS_KEY_ID,AWS_SECRET_ACCESS_KEY,AWS_SESSION_TOKEN -- -c 'bash /home/agent/project/secrets_probe.sh'" /dev/null 2>&1 | grep -E 'inside the sandbox|call to'
rm -f ~/.local/state/enclave/secrets/global/claude.env   # the dummy key was only for this probe
