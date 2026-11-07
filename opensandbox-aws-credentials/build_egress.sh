#!/usr/bin/env bash
# Part 5, step 3: build the egress sidecar image with the SigV4 addon, push it to ECR and switch
# the server to it.
#
#  1. checks: the base image opensandbox-eks/egress:release-1.2.0 in your ECR (the mirror
#     ../opensandbox-eks/03_images.sh made; Docker Hub is never used), the addon's unit tests,
#     and ../opensandbox-lifecycle/server_config.py
#  2. builds egress/ for linux/amd64: FROM that base, pinned by the digest just checked, the addon
#     copied to /var/egress/custom/sigv4_resign.py, OPENSANDBOX_EGRESS_MITMPROXY_SCRIPT pointing at
#     it, and a build step in which the image's own python3 and mitmproxy load the addon. The build
#     context is the addon alone (egress/.dockerignore). Pushes it as
#     opensandbox-eks/egress-sigv4:release-1.2.0 and :release-1.2.0-<content hash>, the hash of
#     egress/Dockerfile, egress/sigv4_resign.py and the base image's digest. Consumers should use
#     the digest this script prints: the :release-1.2.0 tag moves only when a build happens
#  3. switches the server: ../opensandbox-lifecycle/server_config.py --egress-image <image>@<digest>.
#     That script keeps the runtime, the egress mode and the gateway, and checks /health,
#     /version and the running config.toml afterwards. The image is pinned by digest, so a node
#     never runs an older build it cached under the same tag.
#
# The egress image is server-wide: from then on, every sandbox with an egress sidecar runs the
# addon. Traffic without the vault's AWS carrier headers passes through it untouched.
#
# Safe to run again: when the content tag is already in ECR nothing is built, and when the
# server already runs that digest it is not upgraded. An AWS or cluster error other than "that
# image isn't there" stops the script, before anything is built or switched. No output, error
# messages included, shows the account ID, an ARN (with its IAM user or role) or the EKS endpoint. The server uses the Recreate strategy:
# a switch replaces its pod, so restart any `kubectl port-forward` to it (127.0.0.1:18080).
#
#   ./build_egress.sh                                                  build, push, switch
#   python3 ../opensandbox-lifecycle/server_config.py --egress-image base   back to the base image
#
# Needs ../opensandbox-eks/01_cluster.sh to 04_install.sh (env.sh, the cluster, the server, the
# mirrored images) and ../opensandbox-lifecycle (server_config.py). Tools: aws, docker with
# buildx, jq, python3 (3.11+) and uv.
set -euo pipefail
cd "$(dirname "$0")"

for tool in aws docker jq python3 uv; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is not available (see README.md)" >&2; exit 1; }
[[ -f ../opensandbox-eks/env.sh ]] || { echo "run ../opensandbox-eks/01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ../opensandbox-eks/env.sh
: "${ECR_REGISTRY:?../opensandbox-eks/env.sh does not set ECR_REGISTRY: run ../opensandbox-eks/01_cluster.sh again}"
: "${ECR_PREFIX:?../opensandbox-eks/env.sh does not set ECR_PREFIX: run ../opensandbox-eks/01_cluster.sh again}"
: "${AWS_REGION:?../opensandbox-eks/env.sh does not set AWS_REGION: run ../opensandbox-eks/01_cluster.sh again}"
SERVER_CONFIG="../opensandbox-lifecycle/server_config.py"
[[ -f "$SERVER_CONFIG" ]] || { echo "$SERVER_CONFIG not found: it comes with ../opensandbox-lifecycle (part 3)" >&2; exit 1; }

REPO="${ECR_PREFIX}/egress-sigv4"
BASE_REPO="${ECR_PREFIX}/egress"
TAG="release-1.2.0"
LOGS="logs"   # full build and test output (gitignored)
# Everything this script prints, AWS and cluster error messages included, goes through mask:
# the account ID (the first label of the ECR registry host) becomes <account>; any ARN, which
# carries an account ID and an IAM user or role name, becomes <arn>; the EKS API endpoint
# becomes <eks-endpoint>, an ELB/NLB host name <nlb>.
ACCOUNT_ID="${ECR_REGISTRY%%.*}"
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || ACCOUNT_ID="__no_account_id__"
mask() {
  sed -E \
    -e "s/arn:aws[a-zA-Z-]*:[^[:space:]\"',)]+/<arn>/g" \
    -e "s/${ACCOUNT_ID}/<account>/g" \
    -e 's/[0-9A-Fa-f]{32}\.[a-z0-9]+\.[a-z0-9-]+\.eks\.amazonaws\.com/<eks-endpoint>/g' \
    -e 's/[a-z0-9-]+\.elb\.[a-z0-9-]+\.amazonaws\.com/<nlb>/g'
}
mkdir -p "$LOGS"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- output helpers (same as ../opensandbox-eks/01_cluster.sh) -------------------------
phase_start=0
duration() { local s=$1; if (( s >= 60 )); then printf '%dm %02ds' $((s / 60)) $((s % 60)); else printf '%ds' "$s"; fi; }
phase() { phase_start=$(date +%s); printf '\n\033[1m== %s\033[0m\n' "$1"; }
phase_done() { printf '   took %s\n' "$(duration $(( $(date +%s) - phase_start )))"; }
sha() { if command -v sha256sum >/dev/null; then sha256sum | cut -c1-12; else shasum -a 256 | cut -c1-12; fi; }
ok()   { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*" >&2; exit 1; }
# The last lines of a captured error output, masked, on stderr.
show_error() { tail -n "${2:-5}" "$1" | mask | sed 's/^/        /' >&2; }

# The digest of an image tag in this account's ECR; nothing when the image or the repository
# isn't there. Any other error (expired credentials, access denied, throttling) stops the
# script: read as "not there", it would rebuild the same content under a new digest and
# restart the server for nothing. Use as `x="$(ecr_digest repo tag)" || exit 1`.
ecr_digest() {
  local err
  err="$(mktemp "$WORK/ecr.XXXXXX")"
  if aws ecr describe-images --region "$AWS_REGION" --repository-name "$1" --image-ids "imageTag=$2" \
      --query 'imageDetails[0].imageDigest' --output text 2>"$err"; then
    return 0
  fi
  if grep -qE 'ImageNotFoundException|RepositoryNotFoundException' "$err"; then
    return 0
  fi
  show_error "$err"
  fail "could not look up ${1}:${2} in ECR (an AWS error, not a missing image): nothing built or switched"
}

# server_config.py with its stderr captured and shown masked when it fails.
server_config() {
  local err
  err="$(mktemp "$WORK/server.XXXXXX")"
  if python3 "$SERVER_CONFIG" "$@" 2>"$err"; then
    return 0
  fi
  show_error "$err" 15
  fail "${SERVER_CONFIG} $1 failed (above): is the cluster reachable? (kubectl --context ${KUBE_CONTEXT:-osb-eks} get nodes)"
}

# --- 1. What the build needs -------------------------------------------------------------------
phase "preconditions"
base_digest="$(ecr_digest "$BASE_REPO" "$TAG")" || exit 1
[[ "$base_digest" == sha256:* ]] || fail "${BASE_REPO}:${TAG} is not in your ECR: run ../opensandbox-eks/03_images.sh"
ok "base image ${BASE_REPO}:${TAG} in ECR (${base_digest:0:19}...)"
if uv run --quiet --project egress pytest egress/tests/test_sigv4_resign.py -q -p no:cacheprovider \
    >"$LOGS/build-egress-tests.log" 2>&1; then
  ok "addon unit tests: $(tail -n 1 "$LOGS/build-egress-tests.log" | tr -d '=' | sed 's/^ *//; s/ *$//')"
else
  show_error "$LOGS/build-egress-tests.log" 20
  fail "the addon's unit tests fail (full output: ${LOGS}/build-egress-tests.log): not building"
fi
now="$(server_config --show)" || exit 1
ok "server now: ${now}"
phase_done

# The image's inputs: the Dockerfile, the addon (the whole build context, egress/.dockerignore)
# and the base image, by digest. The same inputs give the same tag, so a re-run finds it.
CONTENT_TAG="${TAG}-$( { cat egress/Dockerfile egress/sigv4_resign.py; printf '%s' "$base_digest"; } | sha)"

# --- 2. Build and push -------------------------------------------------------------------------
phase "build and push ${REPO}:${CONTENT_TAG}"
digest="$(ecr_digest "$REPO" "$CONTENT_TAG")" || exit 1
if [[ "$digest" == sha256:* ]]; then
  ok "already in ECR (same Dockerfile, addon and base): ${REPO}:${CONTENT_TAG}, not rebuilt"
else
  # The ECR login lives in a throwaway Docker config, deleted on exit (as in 02_broker.sh):
  # nothing lands in your keychain, and the token is never printed. That config has no Docker
  # contexts, so the build is pointed at the daemon of your current context.
  if [[ -z "${DOCKER_HOST:-}" ]]; then
    DOCKER_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
    if [[ -n "$DOCKER_HOST" ]]; then export DOCKER_HOST; fi
  fi
  if ! token="$(aws ecr get-login-password --region "$AWS_REGION" 2>"$WORK/ecr-login.err")"; then
    show_error "$WORK/ecr-login.err"
    fail "could not get an ECR login token: check your AWS credentials (aws sts get-caller-identity)"
  fi
  real_config="${DOCKER_CONFIG:-$HOME/.docker}"
  if [[ -d "$real_config/cli-plugins" ]]; then ln -s "$real_config/cli-plugins" "$WORK/cli-plugins"; fi
  printf '{"auths":{"%s":{"auth":"%s"}}}\n' "$ECR_REGISTRY" "$(printf 'AWS:%s' "$token" | base64 | tr -d '\n')" >"$WORK/config.json"
  chmod 600 "$WORK/config.json"
  unset token
  export DOCKER_CONFIG="$WORK"
  echo "  FROM ${BASE_REPO}@${base_digest:0:19}... (${TAG}), linux/amd64 (full output: ${LOGS}/build-egress.log)"
  if ! docker buildx build --platform linux/amd64 \
      --build-arg "BASE=${ECR_REGISTRY}/${BASE_REPO}@${base_digest}" \
      --tag "${ECR_REGISTRY}/${REPO}:${TAG}" \
      --tag "${ECR_REGISTRY}/${REPO}:${CONTENT_TAG}" \
      --metadata-file "$WORK/build.json" \
      --push egress/ >"$LOGS/build-egress.log" 2>&1; then
    show_error "$LOGS/build-egress.log" 30
    fail "docker buildx build failed"
  fi
  digest="$(jq -r '."containerimage.digest" // empty' "$WORK/build.json" 2>/dev/null || true)"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "the build did not report an image digest (see ${LOGS}/build-egress.log)"
  ok "build-time check: the image's python3 loaded sigv4_resign.py with all four hooks"
  ok "pushed ${REPO}:${TAG} and :${CONTENT_TAG}"
fi
ok "digest ${digest}"
phase_done

# --- 3. Switch the server ----------------------------------------------------------------------
phase "switch the server's [egress] image"
IMAGE="${ECR_REGISTRY}/${REPO}@${digest}"
state="$(server_config --show --json)" || exit 1
current="$(jq -r '.egress_image' <<<"$state")"
if [[ "$current" == "${REPO}@${digest}" ]]; then
  ok "the server already runs ${REPO}@${digest:0:19}...: not upgraded"
else
  # server_config.py checks the image is in ECR before the upgrade; its output, errors
  # included, is masked like everything else here.
  python3 "$SERVER_CONFIG" --egress-image "$IMAGE" 2>&1 | mask
fi
phase_done

echo
echo "egress image: <registry>/${REPO}@${digest}"
echo "new sandboxes with an egress sidecar now run the SigV4 addon; restart any port-forward to the server"
