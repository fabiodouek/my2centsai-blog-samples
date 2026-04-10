#!/usr/bin/env bash
# deploy.sh
#
# Deploys the serverless API stack using CloudFormation.
#
# Usage: ./deploy.sh [stack-name] [region]

set -euo pipefail

STACK_NAME="${1:-devops-agent-demo}"
REGION="${2:-us-east-1}"

echo "Deploying stack: $STACK_NAME in $REGION"
echo ""

aws cloudformation deploy \
  --template-file "$(dirname "$0")/../cloudformation/stack.yaml" \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ProjectName="$STACK_NAME"

echo ""
echo "Stack deployed. Outputs:"
aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query 'Stacks[0].Outputs[*].{Key:OutputKey,Value:OutputValue}' \
  --output table
