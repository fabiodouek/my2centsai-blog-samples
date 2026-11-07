#!/usr/bin/env bash
# Capture the AWS console pages for the part 5 video: the IAM role osb-sandbox and the CloudTrail event
# history entry that shows the sandbox's session name. Account ID, account alias and IAM user name are masked
# on every screenshot and every frame.
#
#   W=<video work dir> ./console/capture.sh [SESSION_NAME]
#
# SESSION_NAME defaults to the sandbox ID that ./05_cloudtrail.py wrote to logs/last_session.txt.
# W is the video work directory (it must hold .venv with playwright, per the deep-dive-video skill); outputs:
#   $W/shots/iam-role.png, $W/shots/cloudtrail-events.png, $W/web/iam-cloudtrail.mp4
#
# The sign-in is a READ-ONLY federation token from the `default` AWS profile
# (sts get-federation-token --policy-arns ReadOnlyAccess, 1 hour). It lives in a mode-700 temp directory that
# is deleted on exit. Nothing is written to the repository, and no token is printed.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${W:?set W to the video work directory (see the deep-dive-video skill)}"
SK="${SK:-$HOME/Code/blog-open-sandbox/.claude/skills/deep-dive-video/scripts}"
PY="$W/.venv/bin/python"
[[ -x "$PY" ]] || { echo "$W/.venv/bin/python not found: set up the tooling venv first (deep-dive-video skill)" >&2; exit 1; }
[[ -f "$SK/browse.py" ]] || { echo "$SK/browse.py not found" >&2; exit 1; }
SESSION="${1:-}"
if [[ -z "$SESSION" && -f logs/last_session.txt ]]; then SESSION="$(head -1 logs/last_session.txt)"; fi
[[ -n "$SESSION" ]] || { echo "no session name: run ./05_cloudtrail.py first, or pass the sandbox ID" >&2; exit 1; }
[[ "$SESSION" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "the session name has unexpected characters" >&2; exit 1; }

TMP="$(mktemp -d)"
chmod 700 "$TMP"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$W/shots" "$W/web"

identity="$(aws sts get-caller-identity --profile default --output json)"
ACCOUNT="$(jq -r .Account <<<"$identity")"
OSB_CAPTURE_CALLER_NAME="$(jq -r '.Arn | split("/") | last' <<<"$identity")"
OSB_CAPTURE_ALIAS="$(aws iam list-account-aliases --profile default --query 'AccountAliases[0]' --output text 2>/dev/null || true)"
if [[ "$OSB_CAPTURE_ALIAS" == "None" ]]; then OSB_CAPTURE_ALIAS=""; fi
export OSB_CAPTURE_CALLER_NAME OSB_CAPTURE_ALIAS

aws sts get-federation-token --profile default --name osb-console-capture \
  --policy-arns arn=arn:aws:iam::aws:policy/ReadOnlyAccess --duration-seconds 3600 --output json >"$TMP/fed.json"
chmod 600 "$TMP/fed.json"

echo "screenshots (masked) ..."
"$PY" console/console_login.py "$TMP/fed.json" "$TMP" "$W/shots" "$SESSION" || echo "console_login.py: see the lines above (a screenshot may still be usable)" >&2
rm -f "$TMP/fed.json"

# The clip: the same two pages, with the cursor. browse.py masks text in every document it loads.
DASHED="$(sed -E 's/^([0-9]{4})([0-9]{4})([0-9]{4})$/\1-\2-\3/' <<<"$ACCOUNT")"
"$PY" - "$TMP/spec.json" "$ACCOUNT" "$DASHED" "$OSB_CAPTURE_ALIAS" "$OSB_CAPTURE_CALLER_NAME" "$TMP/state.json" "$SESSION" <<'PYEOF'
import json, re, sys
out, account, dashed, alias, caller, state = sys.argv[1:7]
# the top bar shows "<account name> (<id>)": mask the name by its position before the ID (listed first, so it
# runs before the ID itself is replaced)
mask = [r"[^()]+?(?= \((?:%s|%s)\))" % (re.escape(account), re.escape(dashed))] + [re.escape(x) for x in (account, dashed, alias, caller) if x] + [r"ASIA[0-9A-Z]{4,}"]
session = sys.argv[7]
spec = {
    "url": "https://us-east-1.console.aws.amazon.com/iam/home?region=us-east-1#/roles/details/osb-sandbox?section=permissions",
    "state": state, "settle": 8, "viewport": [1280, 720], "scale": 2,
    "hide": ["#awsccc-cb-c", "#awsccc-sb-ux-c"],
    "mask_text": mask,
    "actions": [
        {"mark": "role", "wait": 4},
        {"scroll": 400, "duration": 1.5, "wait": 3},
        {"goto": "https://us-east-1.console.aws.amazon.com/cloudtrail/home?region=us-east-1#/events", "wait": 8, "mark": "events"},
        {"click": "button:has-text(\"Read-only\")", "wait": 1},
        {"click": "[role=option]:has-text(\"User name\")", "wait": 1},
        {"type": session + "\n", "into": "input[placeholder=\"Enter a user name\"]", "wait": 6},
        {"wait": 3},
        {"mark": "end"},
    ],
}
json.dump(spec, open(out, "w"))
PYEOF
echo "clip ..."
"$PY" "$SK/browse.py" "$TMP/spec.json" "$W/web/iam-cloudtrail.mp4" --state "$TMP/state.json" --no-save-state
echo "done: $W/shots/iam-role.png $W/shots/cloudtrail-events.png $W/web/iam-cloudtrail.mp4"
echo "look at every frame and screenshot before use (the masking is a DOM rewrite; the page can still show the IDs somewhere it does not reach)"
