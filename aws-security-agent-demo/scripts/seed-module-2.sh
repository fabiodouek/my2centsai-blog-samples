#!/usr/bin/env bash
#
# seed-module-2.sh — populate the Module 2 MySQL database after `terraform apply`.
#
# The ECS task definition pulls a prebuilt public image
# (public.ecr.aws/p3q0v3y2/aws-goat-m2:latest) which does not auto-seed RDS.
# This script ships the repo-local `dump.sql` to the running container and
# loads it via `docker exec ... mysql` over SSM Run Command.
#
# Requires: aws CLI, base64. No Docker needed on the operator's machine.
# Run from anywhere; paths are resolved relative to the script's location.

set -euo pipefail

# Resource identifiers — set by modules/module-2/main.tf and resources/ecs/task_definition.json.
# All overridable via env var for forks that rename resources.
REGION="${AWS_REGION:-us-east-1}"
CLUSTER_NAME="${CLUSTER_NAME:-ecs-lab-cluster}"       # main.tf:400
DB_IDENTIFIER="${DB_IDENTIFIER:-aws-goat-db}"         # main.tf:154
CONTAINER_LABEL="${CONTAINER_LABEL:-aws-goat-m2}"     # resources/ecs/task_definition.json:26
SECRET_ID="${SECRET_ID:-RDS_CREDS}"                   # main.tf:488

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DUMP_FILE="${DUMP_FILE:-$SCRIPT_DIR/../modules/module-2/src/src/dump.sql}"

[ -f "$DUMP_FILE" ] || { echo "ERROR: dump.sql not found at $DUMP_FILE" >&2; exit 1; }
command -v aws >/dev/null 2>&1 || { echo "ERROR: aws CLI not found in PATH" >&2; exit 1; }
command -v base64 >/dev/null 2>&1 || { echo "ERROR: base64 not found in PATH" >&2; exit 1; }

echo "==> Locating ECS container instance (cluster '$CLUSTER_NAME')..."
CI_ARN=$(aws ecs list-container-instances \
  --region "$REGION" \
  --cluster "$CLUSTER_NAME" \
  --query 'containerInstanceArns[0]' \
  --output text 2>/dev/null || true)
if [ -z "$CI_ARN" ] || [ "$CI_ARN" = "None" ]; then
  echo "ERROR: no container instances registered to cluster '$CLUSTER_NAME'." >&2
  echo "       Has 'terraform apply' finished for module-2?" >&2
  exit 1
fi
INSTANCE_ID=$(aws ecs describe-container-instances \
  --region "$REGION" \
  --cluster "$CLUSTER_NAME" \
  --container-instances "$CI_ARN" \
  --query 'containerInstances[0].ec2InstanceId' \
  --output text 2>/dev/null || true)
if [ -z "$INSTANCE_ID" ] || [ "$INSTANCE_ID" = "None" ]; then
  echo "ERROR: could not resolve EC2 instance for container instance $CI_ARN." >&2
  exit 1
fi
echo "    Instance: $INSTANCE_ID"

echo "==> Fetching DB credentials from Secrets Manager ('$SECRET_ID')..."
CREDS_JSON=$(aws secretsmanager get-secret-value \
  --region "$REGION" \
  --secret-id "$SECRET_ID" \
  --query SecretString --output text 2>/dev/null || true)
if [ -z "$CREDS_JSON" ]; then
  echo "ERROR: could not read secret '$SECRET_ID'." >&2
  exit 1
fi
DB_USER=$(printf '%s' "$CREDS_JSON" | sed -n 's/.*"username":[[:space:]]*"\([^"]*\)".*/\1/p')
DB_PASS=$(printf '%s' "$CREDS_JSON" | sed -n 's/.*"password":[[:space:]]*"\([^"]*\)".*/\1/p')
if [ -z "$DB_USER" ] || [ -z "$DB_PASS" ]; then
  echo "ERROR: could not parse username/password from secret '$SECRET_ID'." >&2
  exit 1
fi
echo "    User:     $DB_USER"

echo "==> Locating RDS endpoint ('$DB_IDENTIFIER')..."
RDS_HOST=$(aws rds describe-db-instances \
  --region "$REGION" \
  --db-instance-identifier "$DB_IDENTIFIER" \
  --query 'DBInstances[0].Endpoint.Address' \
  --output text 2>/dev/null || true)
if [ -z "$RDS_HOST" ] || [ "$RDS_HOST" = "None" ]; then
  echo "ERROR: RDS '$DB_IDENTIFIER' not found." >&2
  exit 1
fi
echo "    RDS:      $RDS_HOST"

echo "==> Building dump payload (prepend DROP, append COMMIT, base64-encode)..."
DUMP_B64=$({
  printf 'DROP DATABASE IF EXISTS appdb;\n'
  cat "$DUMP_FILE"
  printf '\nCOMMIT;\n'
} | base64 | tr -d '\n')
echo "    Payload: ${#DUMP_B64} bytes (base64)"

PARAMS_FILE="$(mktemp -t seed-module-2.XXXXXX.json)"
trap 'rm -f "$PARAMS_FILE"' EXIT

cat > "$PARAMS_FILE" <<JSON
{
  "commands": [
    "set -e",
    "CID=\$(docker ps -q --filter label=com.amazonaws.ecs.container-name=${CONTAINER_LABEL} | head -1)",
    "if [ -z \"\$CID\" ]; then echo 'ERROR: no ${CONTAINER_LABEL} container running on this host' >&2; exit 1; fi",
    "echo '${DUMP_B64}' | base64 -d > /tmp/awsgoat-dump.sql",
    "docker cp /tmp/awsgoat-dump.sql \$CID:/tmp/dump.sql",
    "docker exec \$CID sh -c 'mysql -h ${RDS_HOST} -u${DB_USER} -p${DB_PASS} < /tmp/dump.sql'",
    "rm -f /tmp/awsgoat-dump.sql",
    "echo seed-complete"
  ]
}
JSON

echo "==> Sending SSM command..."
CMD_ID=$(aws ssm send-command \
  --region "$REGION" \
  --instance-ids "$INSTANCE_ID" \
  --document-name "AWS-RunShellScript" \
  --comment "awsgoat-m2 DB seed" \
  --parameters "file://$PARAMS_FILE" \
  --query 'Command.CommandId' --output text)
echo "    CommandId: $CMD_ID"

echo -n "==> Waiting for SSM command"
STATUS="Pending"
for _ in $(seq 1 90); do
  sleep 2
  STATUS=$(aws ssm get-command-invocation --region "$REGION" \
    --instance-id "$INSTANCE_ID" --command-id "$CMD_ID" \
    --query 'Status' --output text 2>/dev/null || echo "Pending")
  case "$STATUS" in
    Success|Failed|Cancelled|TimedOut) break ;;
  esac
  echo -n "."
done
echo " -> $STATUS"

if [ "$STATUS" != "Success" ]; then
  echo "--- stdout ---"
  aws ssm get-command-invocation --region "$REGION" \
    --instance-id "$INSTANCE_ID" --command-id "$CMD_ID" \
    --query 'StandardOutputContent' --output text || true
  echo "--- stderr ---"
  aws ssm get-command-invocation --region "$REGION" \
    --instance-id "$INSTANCE_ID" --command-id "$CMD_ID" \
    --query 'StandardErrorContent' --output text || true
  exit 1
fi

echo "==> Module 2 DB seeded. Log in via the app to confirm."
