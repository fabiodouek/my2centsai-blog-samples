#!/usr/bin/env bash
# On the instance, as the agent user: Claude Code on Amazon Bedrock inside an Enclave session.
#  1. with Enclave's default allowlist (it has no AWS domains)
#  2. after adding the Bedrock runtime endpoint with --allow-domain, then a real headless task
set -uo pipefail
cd ~/project
MODEL="${MODEL:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"

# The role's temporary credentials, read from IMDS on the host (a sandbox cannot reach IMDS, see 05_imds.sh).
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
ROLE=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/iam/security-credentials/)
CREDS=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/iam/security-credentials/$ROLE)
export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | jq -r .AccessKeyId) AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | jq -r .SecretAccessKey) AWS_SESSION_TOKEN=$(echo "$CREDS" | jq -r .Token)
export CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION=us-east-1 ANTHROPIC_MODEL="$MODEL" ANTHROPIC_SMALL_FAST_MODEL="$MODEL"
ask() {  # run one headless Claude Code prompt in a session; print only the agent's reply
  script -qec "enclave --tool claude --allow-domain bedrock-runtime.us-east-1.amazonaws.com --pass-env $PASS -- -p '$1'" /dev/null 2>&1 \
    | sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g' | tr -d '\r' | awk '/^-{20,}/{n++; next} n>=2 && NF'
}
PASS=CLAUDE_CODE_USE_BEDROCK,AWS_REGION,ANTHROPIC_MODEL,ANTHROPIC_SMALL_FAST_MODEL,AWS_ACCESS_KEY_ID,AWS_SECRET_ACCESS_KEY,AWS_SESSION_TOKEN

echo "== 1. default allowlist: ask Claude to say hello (gives up after 40 s)"
timeout 40 script -qec "enclave --tool claude --pass-env $PASS -- -p 'Reply with one word: hello'" /dev/null 2>&1 | grep -E 'API error|API Error' | head -2 | cut -c1-120
docker ps -q --filter name=enclave-claude | xargs -r docker rm -f >/dev/null 2>&1
echo "-- what the gateway denied:"
enclave network log --verdict deny --since 1m --summary 2>&1 | head -6

echo
echo "== 2. allow the Bedrock runtime endpoint with --allow-domain"
START=$(date +%s); echo "enclave --tool claude --allow-domain bedrock-runtime.us-east-1.amazonaws.com ... -- -p 'Reply with one word: hello'"
echo "reply: $(ask 'Reply with one word: hello')   ($(( $(date +%s) - START )) s)"

echo
rm -f ~/project/fib.py
echo "== 3. a real headless task: the agent writes and runs code in the project"
ask 'Write fib.py that prints the first 10 Fibonacci numbers, run it with python3, and reply with the numbers only.' | tr '\n' ' '; echo
echo "-- on the host, the file the agent wrote:"; ls -l ~/project/fib.py | awk '{print $5" bytes", $9}'; python3 ~/project/fib.py | tr '\n' ' '; echo
echo "-- gateway summary for the Bedrock host:"
enclave network log --since 5m --summary 2>&1 | grep -E 'DOMAIN|bedrock-runtime'
