#!/usr/bin/env bash
# Getting started from the Google Cloud side, with the gcloud CLI:
#   ./00_setup_gcloud.sh            enable the API, create a restricted key, save it to .env
#   ./00_setup_gcloud.sh --delete   delete that key again
# Needs: gcloud, signed in, and GOOGLE_CLOUD_PROJECT set to your project ID.
# The key value is written to .env (git-ignored) and never printed.
set -euo pipefail
: "${GOOGLE_CLOUD_PROJECT:?Set GOOGLE_CLOUD_PROJECT to your project ID}"
P="$GOOGLE_CLOUD_PROJECT"
NAME="decisions-api-demo"
step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

key_id() {
  gcloud services api-keys list --project "$P" --filter="displayName=$NAME" --format='value(uid)' | head -1
}

if [[ "${1:-}" == "--delete" ]]; then
  step "Delete the API key \"$NAME\""
  ID="$(key_id)"
  if [[ -z "$ID" ]]; then echo "no key with that name"; exit 0; fi
  echo "plain delete:"
  ERR="$(gcloud services api-keys delete "$ID" --project "$P" --quiet 2>&1 | grep -o -E "FAILED_PRECONDITION: .*detected" || true)"
  if [[ -z "$ERR" ]]; then
    echo "  deleted; keys named $NAME left: $(gcloud services api-keys list --project "$P" --filter="displayName=$NAME" --format='value(uid)' | wc -l | tr -d ' ')"
    rm -f .env; exit 0
  fi
  echo "  $ERR"
  echo "delete with --no-check-existing-usage:"
  # A key that had traffic in the last 7 days is protected: a plain delete fails with
  # APIKEYS_DELETE_INCOMPATIBLE_WITH_USAGE. This key was made for the demo, so skip the check.
  gcloud services api-keys delete "$ID" --project "$P" --no-check-existing-usage --quiet >/dev/null 2>&1
  rm -f .env
  echo "  deleted; keys named $NAME left: $(gcloud services api-keys list --project "$P" --filter="displayName=$NAME" --format='value(uid)' | wc -l | tr -d ' ')"
  exit 0
fi

step "1. Enable the Gemini API (Generative Language API) and the API Keys API"
gcloud services enable generativelanguage.googleapis.com apikeys.googleapis.com --project "$P" --quiet >/dev/null 2>&1
gcloud services list --enabled --project "$P" \
  --filter='config.name:(generativelanguage.googleapis.com apikeys.googleapis.com)' --format='table[no-heading](config.name,state)'

step "2. Create an API key that can only call the Gemini API"
gcloud services api-keys create --project "$P" --display-name "$NAME" \
  --api-target=service=generativelanguage.googleapis.com >/dev/null 2>&1
ID="$(key_id)"
gcloud services api-keys describe "$ID" --project "$P" \
  --format='table[no-heading](displayName,restrictions.apiTargets[].service.list())'

step "3. Save the key to .env (not printed)"
umask 077
printf 'GEMINI_API_KEY=%s\n' "$(gcloud services api-keys get-key-string "$ID" --project "$P" --format='value(keyString)')" > .env
echo "wrote .env ($(wc -c < .env | tr -d ' ') bytes, mode $(stat -f '%Lp' .env 2>/dev/null || stat -c '%a' .env))"

step "4. First request with the new key"
set -a; . ./.env; set +a
for i in 1 2 3 4 5 6; do
  OUT="$(./01_first_call.sh)"
  if [[ "$OUT" == *'"answers"'* ]]; then echo "$OUT"; exit 0; fi
  echo "key not active yet, retrying in 10 s"; sleep 10
done
echo "$OUT"; exit 1
