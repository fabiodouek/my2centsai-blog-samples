#!/usr/bin/env bash
# Part 5, step 1: the AWS side. Two IAM roles, an EKS Pod Identity association and a
# DynamoDB table, from terraform/ (its own state, separate from ../opensandbox-eks/terraform:
# nothing in the cluster stack changes).
#
#   osb-broker     the broker's role, through Pod Identity for opensandbox-broker/osb-broker
#   osb-sandbox    assumed by the broker once per sandbox (test bucket, test table, one
#                  Bedrock model; an explicit Deny on every credential-vending action)
#   osb-cred-test  the DynamoDB table for the non-S3 tests
#
#   ./01_iam.sh            terraform apply, check the result, write broker.env
#   ./01_iam.sh destroy    terraform destroy with the same variables, remove broker.env
#                          (delete the broker first: kubectl delete namespace opensandbox-broker)
#
# broker.env (gitignored) holds the Terraform outputs as KEY=value lines, plus SANDBOX_NAMESPACE
# (the server's [kubernetes] namespace, from ../opensandbox-eks/values/server.yaml); 02_broker.sh
# turns it into the broker's ConfigMap, and the test scripts read it.
#
# The account ID is shown as <account> in everything this script prints.
#
# Safe to run again: a second run reports "No changes." and writes the same broker.env.
#
# Needs ../opensandbox-eks/01_cluster.sh first (env.sh, the cluster, the Pod Identity agent),
# and an existing S3 bucket in the cluster's Region, named by AWS_TEST_BUCKET in the secrets
# file (OSB_SECRETS_ENV, default ~/.config/opensandbox-eks/secrets.env). secrets_env.py parses that file;
# nothing sources it, and this script never prints the bucket name.
set -euo pipefail
cd "$(dirname "$0")"

ACTION="${1:-apply}"
if [[ $# -gt 1 || ( "$ACTION" != apply && "$ACTION" != destroy ) ]]; then
  echo "usage: ./01_iam.sh [apply|destroy]" >&2
  exit 2
fi

for tool in terraform aws jq python3; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
[[ -f ../opensandbox-eks/env.sh ]] || { echo "run ../opensandbox-eks/01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ../opensandbox-eks/env.sh
: "${CLUSTER_NAME:?../opensandbox-eks/env.sh does not set CLUSTER_NAME: run ../opensandbox-eks/01_cluster.sh again}"
: "${AWS_REGION:?../opensandbox-eks/env.sh does not set AWS_REGION: run ../opensandbox-eks/01_cluster.sh again}"
: "${ECR_REGISTRY:?../opensandbox-eks/env.sh does not set ECR_REGISTRY: run ../opensandbox-eks/01_cluster.sh again}"

LOGS="logs"   # full Terraform output (gitignored)
mkdir -p "$LOGS"
# The namespace the server creates sandbox pods in: [kubernetes] namespace in the server's config.
SERVER_VALUES="../opensandbox-eks/values/server.yaml"
SANDBOX_NAMESPACE="$(sed -n '/^[[:space:]]*\[kubernetes\]/,/^[[:space:]]*\[/ s/^[[:space:]]*namespace[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$SERVER_VALUES" 2>/dev/null || true)"
if [[ -z "$SANDBOX_NAMESPACE" ]]; then
  echo "could not read the [kubernetes] namespace from ${SERVER_VALUES}" >&2
  exit 1
fi
# The account ID, to mask it: it is the first label of the ECR registry host.
ACCOUNT_ID="${ECR_REGISTRY%%.*}"
mask() { if [[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then sed "s/${ACCOUNT_ID}/<account>/g"; else cat; fi; }

# --- output helpers (same as ../opensandbox-eks/01_cluster.sh) -------------------------
phase_start=0
duration() { local s=$1; if (( s >= 60 )); then printf '%dm %02ds' $((s / 60)) $((s % 60)); else printf '%ds' "$s"; fi; }
phase() { phase_start=$(date +%s); printf '\n\033[1m== %s\033[0m\n' "$1"; }
phase_done() { printf '   took %s\n' "$(duration $(( $(date +%s) - phase_start )))"; }
failures=0
ok()   { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }

# --- 1. The test bucket ------------------------------------------------------------------
phase "test bucket"
# Terraform reads it from the environment (TF_VAR_test_bucket); nothing below prints it.
TF_VAR_test_bucket="$(python3 -I secrets_env.py AWS_TEST_BUCKET)" || exit 1
export TF_VAR_test_bucket
if [[ "$ACTION" == destroy ]]; then
  ok "AWS_TEST_BUCKET from the secrets file (destroy leaves the bucket alone; name not shown)"
# us-east-1 answers with a null LocationConstraint ("None" in text output).
elif ! bucket_region="$(aws s3api get-bucket-location --bucket "$TF_VAR_test_bucket" \
    --query LocationConstraint --output text 2>/dev/null)"; then
  echo "AWS_TEST_BUCKET: no such bucket in this account, or no access to it (aws sts get-caller-identity)" >&2
  exit 1
else
  [[ "$bucket_region" == "None" ]] && bucket_region="us-east-1"
  if [[ "$bucket_region" != "$AWS_REGION" ]]; then
    echo "AWS_TEST_BUCKET is in ${bucket_region}, the cluster in ${AWS_REGION}: use a bucket in ${AWS_REGION}" >&2
    exit 1
  fi
  ok "AWS_TEST_BUCKET from the secrets file: an existing bucket in ${AWS_REGION} (name not shown)"
fi
phase_done

# --- 2. Terraform --------------------------------------------------------------------------
phase "terraform ${ACTION}"
echo "cluster ${CLUSTER_NAME}, Region ${AWS_REGION} (full output: ${LOGS}/terraform-${ACTION}.log)"
if ! terraform -chdir=terraform init -input=false -no-color >"$LOGS/terraform-init.log" 2>&1; then
  tail -n 20 "$LOGS/terraform-init.log" | mask >&2
  exit 1
fi
# The terminal shows the plan summary, each resource as it completes and any error.
set +e
terraform -chdir=terraform "$ACTION" -input=false -auto-approve -no-color \
  -var "region=${AWS_REGION}" \
  -var "cluster_name=${CLUSTER_NAME}" 2>&1 \
  | tee "$LOGS/terraform-${ACTION}.log" \
  | awk -v account="${ACCOUNT_ID:-none}" '/^(Plan:|No changes\.|Apply complete!|Destroy complete!)|(Creation|Modifications|Destruction) complete after|(Error|Warning): / {
           sub(/ \[id=[^]]*\]$/, ""); gsub(account, "<account>"); print; fflush() }'
tf_rc=${PIPESTATUS[0]}
set -e
if (( tf_rc != 0 )); then
  echo "terraform ${ACTION} failed (exit ${tf_rc}); the end of ${LOGS}/terraform-${ACTION}.log:" >&2
  tail -n 30 "$LOGS/terraform-${ACTION}.log" | mask >&2
  exit 1
fi
phase_done

if [[ "$ACTION" == destroy ]]; then
  rm -f broker.env
  echo "removed broker.env"
  exit 0
fi

# --- 3. broker.env -------------------------------------------------------------------------
phase "broker.env"
# Plain KEY=value lines (no quotes): kubectl create configmap --from-env-file reads them as they are.
terraform -chdir=terraform output -json | jq -r '
  "# Written by 01_iam.sh from the Terraform outputs. Run ./01_iam.sh again instead of editing it.",
  ({ SANDBOX_ROLE_ARN: .sandbox_role_arn.value, BROKER_ROLE_ARN: .broker_role_arn.value,
     DDB_TABLE: .ddb_table.value, BROKER_NS: .broker_ns.value, BROKER_SA: .broker_sa.value,
     AWS_REGION: .region.value, AWS_TEST_BUCKET: .test_bucket.value,
     BEDROCK_MODEL_ID: .bedrock_model_id.value }
   | to_entries[] | "\(.key)=\(.value)")' >broker.env.tmp
echo "SANDBOX_NAMESPACE=${SANDBOX_NAMESPACE}" >>broker.env.tmp
mv broker.env.tmp broker.env
# Everything but the bucket name, with the account ID masked.
grep -v -e '^#' -e '^AWS_TEST_BUCKET=' broker.env | mask | sed 's/^/  /'
echo "  AWS_TEST_BUCKET=(from the secrets file, not shown)"
phase_done

# --- 4. Checks -------------------------------------------------------------------------------
phase "checks"
env_value() { sed -n "s/^$1=//p" broker.env; }
for role in osb-broker osb-sandbox; do
  if aws iam get-role --role-name "$role" --query Role.RoleName --output text >/dev/null 2>&1; then
    ok "IAM role ${role} exists"
  else
    fail "IAM role ${role} not found"
  fi
done
broker_assoc="$(aws eks list-pod-identity-associations --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" \
  --namespace "$(env_value BROKER_NS)" --service-account "$(env_value BROKER_SA)" \
  --query 'associations[].associationId' --output text)"
if [[ -n "$broker_assoc" && "$broker_assoc" != "None" ]]; then
  ok "Pod Identity association for $(env_value BROKER_NS)/$(env_value BROKER_SA)"
else
  fail "no Pod Identity association for $(env_value BROKER_NS)/$(env_value BROKER_SA)"
fi
# Pod Identity puts its credentials into every container of an associated pod, so the
# namespace the sandboxes run in must have no association at all.
sandbox_assoc="$(aws eks list-pod-identity-associations --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" \
  --namespace "$SANDBOX_NAMESPACE" --query 'associations[].serviceAccount' --output text)"
if [[ -z "$sandbox_assoc" || "$sandbox_assoc" == "None" ]]; then
  ok "no Pod Identity association in namespace ${SANDBOX_NAMESPACE} (the sandboxes')"
else
  fail "namespace ${SANDBOX_NAMESPACE} has Pod Identity associations (${sandbox_assoc}): sandbox pods would get AWS credentials"
fi
table_status="$(aws dynamodb describe-table --region "$AWS_REGION" --table-name "$(env_value DDB_TABLE)" \
  --query Table.TableStatus --output text 2>/dev/null || true)"
if [[ "$table_status" == ACTIVE ]]; then
  ok "DynamoDB table $(env_value DDB_TABLE) is ACTIVE"
else
  fail "DynamoDB table $(env_value DDB_TABLE) is '${table_status:-missing}', not ACTIVE"
fi
phase_done

if (( failures > 0 )); then
  echo "${failures} check(s) failed" >&2
  exit 1
fi
echo
echo "next: ./02_broker.sh"
