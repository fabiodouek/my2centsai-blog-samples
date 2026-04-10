#!/usr/bin/env bash
# cleanup.sh
#
# Deletes the CloudFormation stack and all its resources.
#
# Usage: ./cleanup.sh [stack-name] [region]

set -euo pipefail

STACK_NAME="${1:-devops-agent-demo}"
REGION="${2:-us-east-1}"

echo "Deleting stack: $STACK_NAME in $REGION"
echo ""

aws cloudformation delete-stack \
  --stack-name "$STACK_NAME" \
  --region "$REGION"

echo "Waiting for stack deletion..."
aws cloudformation wait stack-delete-complete \
  --stack-name "$STACK_NAME" \
  --region "$REGION"

echo "Stack deleted."
