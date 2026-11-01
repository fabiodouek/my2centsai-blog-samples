#!/usr/bin/env bash
# Test 5 (CloudTrail): how the calls look in CloudTrail event history (management events, free,
# ~90 days, eventually consistent). The session name is per provider refresh state, not per
# sandbox: two sandboxes on the shared p5-aws provider share one session name, so CloudTrail
# cannot tell them apart. A second provider with its own session_name gives a third sandbox its
# own attribution. Event history is queried from the host with the default (admin) profile.
source "$(dirname "$0")/lib.sh"
PERSB="p5-aws-persb"
SHARED_SESSION="p5-shared"   # must match 01_setup.sh's session_name for p5-aws
PERSB_SESSION="p5-persb"
cleanup() {
  for n in p5-ct-a p5-ct-b p5-ct-c; do os sandbox delete "$n" >/dev/null 2>&1 & done; wait
  os provider delete "$PERSB" >/dev/null 2>&1
}
trap cleanup EXIT
[[ -n "${BUCKET:-}" ]] || { echo "run ./01_setup.sh first (env.sh has no BUCKET)" >&2; exit 1; }

say "a second provider ${PERSB} with its own session_name (${PERSB_SESSION})"
if ! os provider get "$PERSB" >/dev/null 2>&1; then
  os provider create --name "$PERSB" --type aws --runtime-credentials >/dev/null
fi
os provider refresh configure "$PERSB" --credential-key AWS_ACCESS_KEY_ID --strategy aws-sts-assume-role \
  --material role_arn="$SANDBOX_ROLE_ARN" --material session_name="$PERSB_SESSION" --material aws_region="$AWS_REGION" 2>&1 | nocolor | mask >/dev/null
os provider refresh rotate "$PERSB" --credential-key AWS_ACCESS_KEY_ID >/dev/null 2>&1
ok "configured"

say "two sandboxes on the shared provider, one on the per-sandbox provider"
signing_sandbox p5-ct-a >/dev/null || exit 1
signing_sandbox p5-ct-b >/dev/null || exit 1
# Same policy, but bound to the per-sandbox provider.
make_sandbox p5-ct-c --provider "$PERSB" --policy "$(policy_file "$PERSB")" \
  --env AWS_DEFAULT_REGION="$AWS_REGION" --env AWS_REGION="$AWS_REGION" --env BUCKET="$BUCKET" >/dev/null || exit 1
ok "p5-ct-a, p5-ct-b (shared ${SHARED_SESSION}); p5-ct-c (${PERSB_SESSION})"

say "one signed call from each (GetCallerIdentity is a management event)"
for n in p5-ct-a p5-ct-b p5-ct-c; do
  arn="$(in_sb "$n" bash -c 'aws sts get-caller-identity --query Arn --output text' 2>&1 | nocolor)"
  echo "  ${n}: $(mask <<<"$arn")"
done

say "CloudTrail event history by session name (lags a few minutes; polling up to ~6 min)"
lookup() {  # username -> count of recent GetCallerIdentity events (default profile, host)
  aws cloudtrail lookup-events --max-results 20 \
    --lookup-attributes AttributeKey=Username,AttributeValue="$1" \
    --query "length(Events)" --output text 2>/dev/null
}
for _ in $(seq 1 18); do
  a="$(lookup "$SHARED_SESSION")"; c="$(lookup "$PERSB_SESSION")"
  if [[ "${a:-0}" -ge 1 && "${c:-0}" -ge 1 ]]; then break; fi
  sleep 20
done
echo "  Username=${SHARED_SESSION} (both shared sandboxes land here): ${a:-0} event(s)"
echo "  Username=${PERSB_SESSION} (only p5-ct-c):                    ${c:-0} event(s)"
echo "  The shared session name can't tell p5-ct-a from p5-ct-b."

say "who minted those sessions: sts:AssumeRole into the sandbox role"
aws cloudtrail lookup-events --max-results 50 --lookup-attributes AttributeKey=ResourceName,AttributeValue="$SANDBOX_ROLE_ARN" \
  --query 'Events[].CloudTrailEvent' --output text 2>/dev/null | tr '\t' '\n' | python3 -c '
import json, sys
seen = set()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    e = json.loads(line)
    rp = e.get("requestParameters") or {}
    if "osh-eks-sandbox" not in str(rp.get("roleArn", "")):
        continue
    ui = e["userIdentity"]
    caller = ui.get("sessionContext", {}).get("sessionIssuer", {}).get("userName", "?")
    session = ui.get("arn", "").split("/")[-1]
    key = (caller, session[:24], rp.get("roleSessionName"))
    if key in seen:
        continue
    seen.add(key)
    name, dur, ntags = rp.get("roleSessionName"), rp.get("durationSeconds"), len(rp.get("tags") or [])
    print(f"  caller {caller} (session {session[:24]}...) -> session name {name}, {dur} s, tags: {ntags}")
' | mask | head -n 4
echo "  S3 object reads and writes are data events: they need a trail or event data store, not shown"
echo "  in the free event history."
