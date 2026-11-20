#!/usr/bin/env bash
# On the instance, as the agent user: two agents at once, each in its own git worktree, on a 2 GiB host.
set -uo pipefail
cd ~/project
MODEL="${MODEL:-us.anthropic.claude-haiku-4-5-20251001-v1:0}"
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
ROLE=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/iam/security-credentials/)
CREDS=$(curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/iam/security-credentials/$ROLE)
export AWS_ACCESS_KEY_ID=$(echo "$CREDS" | jq -r .AccessKeyId) AWS_SECRET_ACCESS_KEY=$(echo "$CREDS" | jq -r .SecretAccessKey) AWS_SESSION_TOKEN=$(echo "$CREDS" | jq -r .Token)
export CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION=us-east-1 ANTHROPIC_MODEL="$MODEL" ANTHROPIC_SMALL_FAST_MODEL="$MODEL"
PASS=CLAUDE_CODE_USE_BEDROCK,AWS_REGION,ANTHROPIC_MODEL,ANTHROPIC_SMALL_FAST_MODEL,AWS_ACCESS_KEY_ID,AWS_SECRET_ACCESS_KEY,AWS_SESSION_TOKEN

docker ps -aq | xargs -r docker rm -f >/dev/null 2>&1
for n in a b; do git worktree remove --force ../wt-$n 2>/dev/null; git branch -D agent/$n >/dev/null 2>&1; git worktree add -q ../wt-$n -b agent/$n; done
echo "== worktrees"; git worktree list

( while sleep 2; do echo "$(date +%s) $(free -m | awk '/Mem/{print $3}') $(free -m | awk '/Swap/{print $3}') $(docker ps --format '{{.Names}}' | grep enclave-claude | grep -vc gateway)"; done > /tmp/par.log ) &
SAMPLER=$!
echo "== start two background sessions"
for n in a b; do
  ( cd ../wt-$n && script -qec "enclave --tool claude --background --name agent-$n --allow-domain bedrock-runtime.us-east-1.amazonaws.com --pass-env $PASS -- -p 'Create primes.py with a function primes_up_to(n), and test_$n.py that asserts primes_up_to(30) is [2,3,5,7,11,13,17,19,23,29]. Run the test with python3. Reply done when it passes.'" /dev/null >/tmp/bg-$n.log 2>&1 )
done
sleep 6
echo "== enclave ps"; enclave ps 2>&1 | cut -c1-160
echo "== memory while both run"; free -m | sed -n 1,3p
echo "== docker stats"; docker stats --no-stream --format '{{.Name}}  cpu {{.CPUPerc}}  mem {{.MemUsage}}' 2>&1 | cut -c1-120

echo "== wait for both to finish"
for i in $(seq 1 60); do enclave ps 2>&1 | grep -q running || break; sleep 3; done
echo "-- after they exit, their gateways are still up until you stop the sessions:"; enclave ps --all 2>&1 | cut -c1-120; docker ps --format '{{.Names}}' | grep -c gateway | sed 's/^/gateway containers still running: /'
enclave stop >/dev/null 2>&1; echo "after enclave stop: $(docker ps -q | wc -l) containers running"
kill $SAMPLER 2>/dev/null
echo "most agent containers running at once: $(sort -k4 -n /tmp/par.log | tail -1 | awk '{print $4}')"
echo "peak RAM used (MB): $(sort -k2 -n /tmp/par.log | tail -1 | awk '{print $2}')   peak swap used (MB): $(sort -k3 -n /tmp/par.log | tail -1 | awk '{print $3}')   (host total $(free -m | awk '/Mem/{print $2}') MB)"
for n in a b; do echo "== worktree wt-$n"; ls ../wt-$n | tr '\n' ' '; echo; (cd ../wt-$n && python3 test_$n.py && echo "test_$n.py passes"); done
