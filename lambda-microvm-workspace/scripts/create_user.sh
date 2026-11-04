#!/usr/bin/env bash
# Create (or reset) a confirmed workspace user with a permanent password. No email is sent.
#   scripts/create_user.sh you@example.com 'Passw0rd!long'
#   scripts/create_user.sh --admin you@example.com      # also add to the admins group (may change the proxy policy)
# Omit the password to be prompted for it (keeps it out of shell history).
set -euo pipefail

ADMIN=""
if [[ "${1:-}" == "--admin" ]]; then ADMIN=1; shift; fi
[[ $# -ge 1 ]] || { echo "usage: $0 [--admin] <email> [password]" >&2; exit 1; }
EMAIL="$1"
PASSWORD="${2:-}"
if [[ -z "$PASSWORD" ]]; then
  read -r -s -p "Password for $EMAIL: " PASSWORD
  echo
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export AWS_REGION="${AWS_REGION:-us-east-1}" AWS_PAGER=""
POOL="$(terraform -chdir="$ROOT/infra" output -raw user_pool_id)"

if aws cognito-idp admin-get-user --user-pool-id "$POOL" --username "$EMAIL" >/dev/null 2>&1; then
  echo "user exists; resetting the password"
else
  aws cognito-idp admin-create-user --user-pool-id "$POOL" --username "$EMAIL" \
    --user-attributes Name=email,Value="$EMAIL" Name=email_verified,Value=true \
    --message-action SUPPRESS >/dev/null
  echo "created $EMAIL"
fi

aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username "$EMAIL" \
  --password "$PASSWORD" --permanent
if [[ -n "$ADMIN" ]]; then
  # The group shows up in the ID token's cognito:groups claim at the next sign-in
  aws cognito-idp admin-add-user-to-group --user-pool-id "$POOL" --username "$EMAIL" \
    --group-name "$(terraform -chdir="$ROOT/infra" output -raw admin_group)"
  echo "added to the admins group"
fi
echo "password set (permanent); sign in at $(terraform -chdir="$ROOT/infra" output -raw url)"
