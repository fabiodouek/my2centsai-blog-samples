#!/usr/bin/env bash
set -euo pipefail

# Create an AWS Agent Registry with JWT (Cognito) authorization and manual approval.
#
# Usage:
#   ./scripts/create-registry.sh <registry-name> <cognito-user-pool-id> [region]
#
# The Cognito User Pool ID is required for JWT auth. Create one in the Cognito
# console or with: aws cognito-idp create-user-pool --pool-name my-registry-pool
#
# To use IAM auth instead, pass "IAM" as the user-pool-id:
#   ./scripts/create-registry.sh <registry-name> IAM [region]

REGISTRY_NAME="${1:?Usage: $0 <registry-name> <cognito-user-pool-id|IAM> [region]}"
USER_POOL_ID="${2:?Usage: $0 <registry-name> <cognito-user-pool-id|IAM> [region]}"
REGION="${3:-us-east-1}"

if [ "${USER_POOL_ID}" = "IAM" ]; then
  AUTH_TYPE="AWS_IAM"
  AUTH_LABEL="AWS IAM"
  AUTH_ARGS="--authorizer-type AWS_IAM"
else
  AUTH_TYPE="CUSTOM_JWT"
  AUTH_LABEL="JWT (Cognito: ${USER_POOL_ID})"
  DISCOVERY_URL="https://cognito-idp.${REGION}.amazonaws.com/${USER_POOL_ID}/.well-known/openid-configuration"
  JWT_CONFIG=$(jq -n \
    --arg url "${DISCOVERY_URL}" \
    '{"customJWTAuthorizer": {"discoveryUrl": $url}}')
  AUTH_ARGS="--authorizer-type CUSTOM_JWT --authorizer-configuration ${JWT_CONFIG}"
fi

echo "============================================"
echo "  Creating Agent Registry"
echo "============================================"
echo ""
echo "Registry name: ${REGISTRY_NAME}"
echo "Region:        ${REGION}"
echo "Auth type:     ${AUTH_LABEL}"
echo "Auto-approve:  false"
echo ""

echo "[1/2] Creating registry..."
RESPONSE=$(aws bedrock-agentcore-control create-registry \
  --name "${REGISTRY_NAME}" \
  --description "Demo registry for AWS Agent Registry evaluation" \
  ${AUTH_ARGS} \
  --approval-configuration autoApproval=false \
  --region "${REGION}" \
  --output json)

REGISTRY_ARN=$(echo "${RESPONSE}" | jq -r '.registryArn')
REGISTRY_ID=$(echo "${REGISTRY_ARN}" | awk -F'/' '{print $NF}')

echo "  Registry ARN: ${REGISTRY_ARN}"
echo "  Registry ID:  ${REGISTRY_ID}"
echo ""

echo "[2/2] Waiting for registry to become READY..."
while true; do
  STATUS=$(aws bedrock-agentcore-control get-registry \
    --registry-id "${REGISTRY_ID}" \
    --region "${REGION}" \
    --output json | jq -r '.status')

  if [ "${STATUS}" = "READY" ]; then
    echo "  Status: ${STATUS}"
    break
  elif [ "${STATUS}" = "CREATE_FAILED" ]; then
    echo "  ERROR: Registry creation failed."
    exit 1
  fi

  echo "  Status: ${STATUS} (waiting...)"
  sleep 2
done

echo ""
echo "============================================"
echo "  Registry created successfully!"
echo "============================================"
echo ""
echo "Registry ID: ${REGISTRY_ID}"
echo ""
echo "Next step: register resources with:"
echo "  ./scripts/register-resources.sh ${REGISTRY_ID} ${REGION}"
echo ""
