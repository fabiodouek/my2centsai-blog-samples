#!/usr/bin/env bash
# Test 6: credentials the agent never holds. Three providers: GitHub and Anthropic with real
# credentials read from your environment, and a dummy token for the echo server, which shows what
# actually arrives at the destination. Nothing here prints a credential: only placeholders, HTTP
# codes and the dummy value. Your repository's name is masked as <GITHUB_REPO>.
#
# Needs in the environment: GITHUB_TOKEN (fine-grained, contents read/write on one repository),
# GITHUB_REPO (owner/name of that repository), ANTHROPIC_API_KEY. The push goes to a new branch
# openshell-demo-<timestamp>, which the script deletes again.
source "$(dirname "$0")/lib.sh"
: "${GITHUB_TOKEN:?set GITHUB_TOKEN}" "${GITHUB_REPO:?set GITHUB_REPO (owner/name)}" "${ANTHROPIC_API_KEY:?set ANTHROPIC_API_KEY}"
NAME="creds"
BRANCH="openshell-demo-$(date +%s)"
mask() { sed -e "s|${GITHUB_REPO}|<GITHUB_REPO>|g" -e 's|^remote: .*pull/new.*|remote: (pull-request link)|'; }
cleanup() {
  os sandbox delete "$NAME" >/dev/null 2>&1
  for p in e-github e-anthropic e-echo; do os provider delete "$p" >/dev/null 2>&1; done
  gh api -X DELETE "repos/${GITHUB_REPO}/git/refs/heads/${BRANCH}" >/dev/null 2>&1 || true   # if the in-sandbox delete didn't run
}
trap cleanup EXIT
cleanup

say "openshell profile import -f profiles/{github,anthropic,echo-demo}.yaml"
for p in github anthropic echo-demo; do import_profile "$p"; done
os provider list-profiles 2>/dev/null | nocolor | sed 's/^/  /'

say "openshell provider create --type github|anthropic|echo-demo --credential <KEY>   (values read from the environment)"
os provider create --name e-github --type github --credential GITHUB_TOKEN 2>&1 | nocolor | sed 's/^/  /'
os provider create --name e-anthropic --type anthropic --credential ANTHROPIC_API_KEY 2>&1 | nocolor | sed 's/^/  /'
ECHO_TOKEN=demo-token-1 os provider create --name e-echo --type echo-demo --credential ECHO_TOKEN 2>&1 | nocolor | sed 's/^/  /'

say "openshell sandbox create --name ${NAME} --provider e-github --provider e-anthropic --provider e-echo --policy github-push.yaml"
mkdir -p "${HERE}/logs"
sed "s|\${GITHUB_REPO}|${GITHUB_REPO}|g" "${HERE}/policies/github-push.yaml.tmpl" >"${HERE}/logs/github-push.yaml"
make_sandbox "$NAME" --provider e-github --provider e-anthropic --provider e-echo --policy "${HERE}/logs/github-push.yaml" || exit 1

say "what the sandbox holds"
in_sb "$NAME" sh -c 'for k in GITHUB_TOKEN ANTHROPIC_API_KEY ECHO_TOKEN; do printf "  %-18s %s\n" "$k" "$(printenv $k)"; done'

say "what the echo server receives (a dummy token)"
in_sb "$NAME" sh -c 'curl -sS -m 8 -H "Authorization: Bearer $ECHO_TOKEN" http://echo.echo.svc.cluster.local/whoami | jq -r "\"  sent: Bearer \" + (env.ECHO_TOKEN|.[0:40]) + \"...\", \"  got:  \" + .headers.Authorization"'

say "GitHub: gh and git are in the provider profile"
in_sb "$NAME" sh -c 'printf "  gh api user:          %s\n" "$(gh api user --jq .type 2>&1 | tail -n 1)"
  printf "  git ls-remote:        %s refs\n" "$(git ls-remote "https://x-access-token:${GITHUB_TOKEN}@github.com/$0.git" 2>/dev/null | wc -l)"
  printf "  curl api.github.com:  %s\n" "$(curl -sS -m 8 -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/user 2>&1 | tail -n 1)"
  printf "  python3 (own rule):   %s\n" "$(python3 -c "import os, urllib.request as u; print(u.urlopen(u.Request(\"https://api.github.com/user\", headers={\"Authorization\": \"Bearer \" + os.environ[\"GITHUB_TOKEN\"]}), timeout=8).status)" 2>&1 | tail -n 1)"' "$GITHUB_REPO" 2>&1 | mask

say "git push to a new branch, then delete it   (allowed for this one repository by the sandbox policy)"
in_sb "$NAME" sh -c 'set -e; cd /sandbox; rm -rf repo
  git clone -q --depth 1 "https://x-access-token:${GITHUB_TOKEN}@github.com/$0.git" repo; cd repo
  git checkout -q -b "$1"; date > openshell-demo.txt; git add openshell-demo.txt
  git -c user.name=openshell-demo -c user.email=demo@example.invalid commit -qm "OpenShell credential demo"
  git push -q origin "$1" 2>&1 | grep -v "^remote:" | sed "s/^/  /" || true
  echo "  branch on GitHub after push:   $(git ls-remote origin "$1" | wc -l)"
  git push -q origin --delete "$1"
  echo "  branch on GitHub after delete: $(git ls-remote origin "$1" | wc -l)"' "$GITHUB_REPO" "$BRANCH" 2>&1 | mask

say "a placeholder sent where it isn't bound, and a spoofed Host header"
in_sb "$NAME" sh -c 'printf "  GitHub placeholder to the echo server: "; curl -sS -m 8 -H "Authorization: Bearer $GITHUB_TOKEN" http://echo.echo.svc.cluster.local/steal; echo
  printf "  Host: api.github.com to the echo server: "; curl -sS -m 8 -H "Host: api.github.com" -H "Authorization: Bearer $ECHO_TOKEN" http://echo.echo.svc.cluster.local/steal; echo'

say "Anthropic: a Messages API call with the placeholder key"
in_sb "$NAME" sh -c 'curl -sS -m 60 https://api.anthropic.com/v1/messages -H "x-api-key: $ANTHROPIC_API_KEY" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d "{\"model\":\"claude-haiku-5-5\",\"max_tokens\":30,\"messages\":[{\"role\":\"user\",\"content\":\"Reply with exactly: hello from an OpenShell sandbox\"}]}" \
  | jq -r "\"  model:  \" + .model, \"  reply:  \" + .content[0].text, \"  tokens: \(.usage.input_tokens) in, \(.usage.output_tokens) out\""'

say "openshell logs ${NAME}   (credential decisions)"
ocsf "$NAME" | grep -E 'credential|authority|HTTP:POST.*(messages|receive-pack)' | cut -c1-140 | mask | sed 's/^/  /' | tail -n 4
