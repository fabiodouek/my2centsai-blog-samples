#!/usr/bin/env bash
# Build the part 4 workload image (image/Dockerfile) and push it to ECR as
# <registry>/<prefix>/egress-tools:1. The base python image has no curl, git, Node or AWS CLI, and
# the scripts compare how those clients treat the egress proxy's CA.
#
#   ./00_image.sh            build for linux/amd64 and push (skips the build if the tag is already in ECR)
#   ./00_image.sh --force    build and push even if the tag exists
#
# The ECR login lives in a throwaway Docker config deleted on exit, like ../opensandbox-eks/03_images.sh:
# nothing lands in your keychain. The repository is created by the ECR creation template on the first push.
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

TAG="${ECR_REGISTRY}/${ECR_PREFIX}/egress-tools:1"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
real_config="${DOCKER_CONFIG:-$HOME/.docker}"
if [[ -d "$real_config/cli-plugins" ]]; then ln -s "$real_config/cli-plugins" "$WORK/cli-plugins"; fi
token="$(aws ecr get-login-password --region "$AWS_REGION")" || { echo "could not get an ECR login token (aws sts get-caller-identity?)" >&2; exit 1; }
printf '{"auths":{"%s":{"auth":"%s"}}}\n' "$ECR_REGISTRY" "$(printf 'AWS:%s' "$token" | base64 | tr -d '\n')" >"$WORK/config.json"
chmod 600 "$WORK/config.json"
unset token
export DOCKER_CONFIG="$WORK"

if [[ "$FORCE" == false ]] && docker buildx imagetools inspect "$TAG" >/dev/null 2>&1; then
  echo "already in ECR: ${TAG#*/} (use --force to rebuild)"
  exit 0
fi
docker buildx build --platform linux/amd64 --provenance=false --push -t "$TAG" image
docker buildx imagetools inspect "$TAG" | sed -n '1,4p'
echo "pushed ${TAG#*/}"
