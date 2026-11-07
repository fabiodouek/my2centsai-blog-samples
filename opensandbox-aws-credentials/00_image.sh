#!/usr/bin/env bash
# Build the part 5 workload image (image/Dockerfile) and push it to ECR as
# <registry>/<prefix>/aws-tools:1. Part 4's egress-tools:1 image has the AWS CLI v2 but not boto3, and
# the sandbox cannot pip install anything: its network policy allows AWS hosts only.
#
#   ./00_image.sh            build for linux/amd64 and push (skips the build if the tag is already in ECR)
#   ./00_image.sh --force    build and push even if the tag exists
#
# Needs part 4's image first (../opensandbox-egress/00_image.sh). The ECR login lives in a throwaway
# Docker config deleted on exit, like ../opensandbox-egress/00_image.sh: nothing lands in your keychain.
set -euo pipefail
cd "$(dirname "$0")"

FORCE=false
case "${1:-}" in
  "") ;;
  --force) FORCE=true ;;
  *) echo "usage: ./00_image.sh [--force]" >&2; exit 2 ;;
esac
(( $# <= 1 )) || { echo "usage: ./00_image.sh [--force]" >&2; exit 2; }

[[ -f ../opensandbox-eks/env.sh ]] || { echo "run ../opensandbox-eks/01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ../opensandbox-eks/env.sh
: "${ECR_REGISTRY:?env.sh does not set ECR_REGISTRY}" "${ECR_PREFIX:?env.sh does not set ECR_PREFIX}" "${AWS_REGION:?env.sh does not set AWS_REGION}"
for tool in aws docker; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed" >&2; exit 1; }
done
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is not available" >&2; exit 1; }

BASE="${ECR_REGISTRY}/${ECR_PREFIX}/egress-tools:1"
TAG="${ECR_REGISTRY}/${ECR_PREFIX}/aws-tools:1"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
real_config="${DOCKER_CONFIG:-$HOME/.docker}"
if [[ -d "$real_config/cli-plugins" ]]; then ln -s "$real_config/cli-plugins" "$WORK/cli-plugins"; fi
token="$(aws ecr get-login-password --region "$AWS_REGION")" || { echo "could not get an ECR login token (aws sts get-caller-identity?)" >&2; exit 1; }
printf '{"auths":{"%s":{"auth":"%s"}}}\n' "$ECR_REGISTRY" "$(printf 'AWS:%s' "$token" | base64 | tr -d '\n')" >"$WORK/config.json"
chmod 600 "$WORK/config.json"
unset token
export DOCKER_CONFIG="$WORK"

docker buildx imagetools inspect "$BASE" >/dev/null 2>&1 || { echo "${BASE#*/} is not in ECR: run ../opensandbox-egress/00_image.sh first" >&2; exit 1; }
if [[ "$FORCE" == false ]] && docker buildx imagetools inspect "$TAG" >/dev/null 2>&1; then
  echo "already in ECR: ${TAG#*/} (use --force to rebuild)"
  exit 0
fi
docker buildx build --platform linux/amd64 --provenance=false --push --build-arg "BASE=${BASE}" -t "$TAG" image
docker buildx imagetools inspect "$TAG" | sed -n '1,4p'
echo "pushed ${TAG#*/}"
