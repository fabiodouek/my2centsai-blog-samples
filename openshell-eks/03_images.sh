#!/usr/bin/env bash
# Copy the container images this series runs into your ECR registry, so the cluster pulls
# from ECR and nothing is fetched from the internet when a sandbox starts.
#
# images.txt lists the images, one per line: the source and its path under
# <registry>/openshell-eks/. Each one is copied with its whole multi-architecture index. The
# repositories don't exist beforehand: the ECR repository creation template that
# 01_cluster.sh sets up creates each one on its first push, with a lifecycle rule.
#
# The workload image (workload/Dockerfile: the pinned Ubuntu base plus curl, git, gh, jq,
# Python, boto3 and the AWS CLI) is built here for linux/amd64 and pushed as
# openshell-eks/workload:<WORKLOAD_TAG>.
#
# Then every ECR index digest is compared with the source's. An image that is missing or
# different is listed by name and the script exits 1, so the problem shows up here and not
# later as ImagePullBackOff and a sandbox that times out with no hint why.
#
#   ./03_images.sh                 copy what is missing or different, then verify
#   ./03_images.sh --verify-only   copy nothing; only compare the digests
#
# Safe to run again: an image whose ECR digest already equals the source's isn't copied, so a
# second run changes nothing. A failed copy leaves what finished in place; run it again.
#
# Docker login: the ECR login lives in a throwaway Docker config that is deleted on exit.
# Nothing lands in your keychain, and your own Docker logins are not used, so the pulls from
# ghcr.io, registry.k8s.io and nvcr.io are anonymous. When a registry answers 429 the script
# waits and tries again (15 s, then double, 5 attempts, 15 minutes of waiting in all) and says
# so. If a rate limit still blocks an image, run the script again later: it copies only what
# is missing.
set -euo pipefail
cd "$(dirname "$0")"

VERIFY_ONLY=false
if (( $# > 1 )); then
  echo "usage: ./03_images.sh [--verify-only]" >&2
  exit 2
fi
case "${1:-}" in
  "") ;;
  --verify-only) VERIFY_ONLY=true ;;
  *) echo "usage: ./03_images.sh [--verify-only]" >&2; exit 2 ;;
esac

for tool in aws docker jq; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is not available (see README.md)" >&2; exit 1; }
if [[ ! -f env.sh ]]; then
  echo "env.sh not found: run ./01_cluster.sh first" >&2
  exit 1
fi
# shellcheck source=/dev/null
source ./env.sh
: "${ECR_REGISTRY:?env.sh does not set ECR_REGISTRY: run ./01_cluster.sh again}"
: "${ECR_PREFIX:?env.sh does not set ECR_PREFIX: run ./01_cluster.sh again}"
: "${AWS_REGION:?env.sh does not set AWS_REGION: run ./01_cluster.sh again}"

LOGS="logs"   # full output of every registry command (gitignored)
WORKLOAD_TAG="0.1.6"
WORKLOAD_DIGEST_FILE="$LOGS/workload-${WORKLOAD_TAG}.digest"   # written by the build, read by --verify-only
LOG="$LOGS/03-images.log"
MAX_ATTEMPTS=5          # per registry command
MAX_TOTAL_WAIT=900      # seconds spent waiting to retry, across the whole run
mkdir -p "$LOGS"
: >"$LOG"

# --- output and timing helpers (same as 01_cluster.sh) ---------------------------
script_start=$(date +%s)
timings=""
phase_name=""
phase_start=0
duration() { local s=$1; if (( s >= 60 )); then printf '%dm %02ds' $((s / 60)) $((s % 60)); else printf '%ds' "$s"; fi; }
phase() {
  phase_name="$1"
  phase_start=$(date +%s)
  printf '\n\033[1m== %s\033[0m\n' "$1"
}
phase_done() {
  local took=$(( $(date +%s) - phase_start ))
  timings+="$(printf '  %-30s %9s' "$phase_name" "$(duration "$took")")"$'\n'
  printf '   took %s\n' "$(duration "$took")"
}
ok()   { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; }

# --- the image list ----------------------------------------------------------------
SRC_REFS=()     # source image (by tag or by digest)
DST_REFS=()     # full ECR reference
NAMES=()        # <prefix>/<path>:<tag>, how this script talks about an image
SRC_DIGESTS=()  # filled in as the sources are read
PROBLEMS=()     # why an image could not be copied (empty when nothing went wrong)
name_width=0
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"
  read -r src path extra <<<"$line" || true
  [[ -n "${src:-}" ]] || continue
  if [[ -z "${path:-}" || -n "${extra:-}" || "${path##*/}" != *:* || "$path" == *@* ]]; then
    echo "images.txt: '${line}' must be '<source> <path:tag>'" >&2
    exit 1
  fi
  SRC_REFS+=("$src")
  DST_REFS+=("${ECR_REGISTRY}/${ECR_PREFIX}/${path}")
  NAMES+=("${ECR_PREFIX}/${path}")
  SRC_DIGESTS+=("")
  PROBLEMS+=("")
  if (( ${#ECR_PREFIX} + 1 + ${#path} > name_width )); then name_width=$(( ${#ECR_PREFIX} + 1 + ${#path} )); fi
done < images.txt
WORKLOAD_NAME="${ECR_PREFIX}/workload:${WORKLOAD_TAG}"
WORKLOAD_REF="${ECR_REGISTRY}/${WORKLOAD_NAME}"
if (( ${#WORKLOAD_NAME} > name_width )); then name_width=${#WORKLOAD_NAME}; fi
count=${#SRC_REFS[@]}
if (( count == 0 )); then
  echo "images.txt lists no images" >&2
  exit 1
fi

# --- registry helpers ------------------------------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
RETRY_OUT=""    # stdout of the last command run by retry
RETRY_ERR=""    # its stderr
rate_limited=0  # how many times a registry answered 429
waited=0        # seconds spent waiting for a retry

# retry <command...>: run a registry command; try again after a wait when it fails for a
# reason that can pass (a rate limit, a network error). A missing image or a refused login
# fails at once.
retry() {
  local attempt=1 wait=15 why
  while :; do
    if RETRY_OUT="$("$@" 2>"$WORK/stderr")"; then
      RETRY_ERR="$(<"$WORK/stderr")"
      return 0
    fi
    RETRY_ERR="$(<"$WORK/stderr")"
    printf '$ %s\n%s\n' "$*" "$RETRY_ERR" >>"$LOG"
    if grep -qiE '429|toomanyrequests|too many requests|rate limit' <<<"$RETRY_ERR"; then
      why="rate limited (HTTP 429)"
      rate_limited=$((rate_limited + 1))
    elif grep -qiE 'not found|manifest unknown|name unknown|unauthorized|denied|invalid reference' <<<"$RETRY_ERR"; then
      return 1
    else
      why="failed: $(err_line "$RETRY_ERR")"
    fi
    (( attempt < MAX_ATTEMPTS && waited + wait <= MAX_TOTAL_WAIT )) || return 1
    printf '        %s; waiting %ds before attempt %d of %d\n' "$why" "$wait" $((attempt + 1)) "$MAX_ATTEMPTS"
    sleep "$wait"
    waited=$((waited + wait))
    wait=$((wait * 2))
    attempt=$((attempt + 1))
  done
}

# err_line <text>: the error line of a command's stderr, shortened to fit a terminal line.
err_line() {
  local l host
  if grep -qiE '429|toomanyrequests|too many requests' <<<"$1"; then
    host="$(grep -oE 'https?://[^/ ]+' <<<"$1" | head -n1 || true)"
    printf 'rate limited (HTTP 429)%s' "${host:+ by ${host#*://}}"
    return
  fi
  l="$(grep -m1 '^ERROR' <<<"$1" || true)"
  [[ -n "$l" ]] || l="$(head -n1 <<<"$1")"
  l="${l#ERROR: }"
  printf '%.150s' "$l"
}

# digest_of <ref>: sets DIGEST to the digest of the manifest or index the reference points to.
# Returns 0 when it found one, 2 when the image doesn't exist (DIGEST is empty), and 1 for any
# other error (the reason is in RETRY_ERR).
DIGEST=""
digest_of() {
  DIGEST=""
  if retry docker buildx imagetools inspect "$1" --format '{{json .Manifest}}'; then
    DIGEST="$(jq -r '.digest // empty' <<<"$RETRY_OUT")"
    [[ -n "$DIGEST" ]] && return 0
    RETRY_ERR="no digest in the manifest returned for $1"
    return 1
  fi
  if grep -qiE 'not found|manifest unknown|name unknown' <<<"$RETRY_ERR"; then return 2; fi
  return 1
}

# --- 1. ECR login --------------------------------------------------------------------
phase "ECR login"
if ! token="$(aws ecr get-login-password --region "$AWS_REGION")"; then
  echo "could not get an ECR login token: check your AWS credentials (aws sts get-caller-identity)" >&2
  exit 1
fi
# The Docker CLI looks for CLI plugins (buildx) in its config directory, so link the real one.
real_config="${DOCKER_CONFIG:-$HOME/.docker}"
if [[ -d "$real_config/cli-plugins" ]]; then ln -s "$real_config/cli-plugins" "$WORK/cli-plugins"; fi
# The same file format docker login writes when no credential helper is set. A Docker config
# with no logins in it would fall back to the keychain helper, which is what to avoid here.
printf '{"auths":{"%s":{"auth":"%s"}}}\n' "$ECR_REGISTRY" "$(printf 'AWS:%s' "$token" | base64 | tr -d '\n')" >"$WORK/config.json"
chmod 600 "$WORK/config.json"
unset token
export DOCKER_CONFIG="$WORK"
ok "logged in to ${ECR_REGISTRY} (token valid for 12 hours, kept in a throwaway Docker config)"
phase_done

# --- 2. Copy ----------------------------------------------------------------------------
copied=0
present=0
if [[ "$VERIFY_ONLY" == false ]]; then
  phase "copy ${count} images to ECR"
  for (( i = 0; i < count; i++ )); do
    src="${SRC_REFS[i]}"; dst="${DST_REFS[i]}"; name="${NAMES[i]}"
    label="$(printf '%-*s' "$name_width" "$name")"
    started=$(date +%s)

    rc=0; digest_of "$src" || rc=$?
    if (( rc != 0 )); then
      if (( rc == 2 )); then PROBLEMS[i]="source does not exist"; else PROBLEMS[i]="cannot read source: $(err_line "$RETRY_ERR")"; fi
      printf '  skip  %s  %s\n' "$label" "${PROBLEMS[i]}"
      continue
    fi
    SRC_DIGESTS[i]="$DIGEST"

    rc=0; digest_of "$dst" || rc=$?
    if (( rc == 1 )); then
      PROBLEMS[i]="cannot read ECR: $(err_line "$RETRY_ERR")"
      printf '  skip  %s  %s\n' "$label" "${PROBLEMS[i]}"
      continue
    fi
    if (( rc == 0 )) && [[ "$DIGEST" == "${SRC_DIGESTS[i]}" ]]; then
      present=$((present + 1))
      printf '  have  %s  already in ECR with the source digest\n' "$label"
      continue
    fi
    note=""
    if (( rc == 0 )); then note="  (replacing ${DIGEST:0:19})"; fi

    if retry docker buildx imagetools create --tag "$dst" "$src"; then
      printf '%s\n%s\n' "\$ docker buildx imagetools create --tag ${dst} ${src}" "$RETRY_ERR" >>"$LOG"
      copied=$((copied + 1))
      printf '  copy  %s  from %s  %s%s\n' "$label" "$src" "$(duration $(( $(date +%s) - started )))" "$note"
    else
      PROBLEMS[i]="copy failed: $(err_line "$RETRY_ERR")"
      printf '  FAIL  %s  %s\n' "$label" "${PROBLEMS[i]}"
    fi
  done
  printf '   copied %d, already in ECR %d\n' "$copied" "$present"
  phase_done
fi

# --- 3. Workload image --------------------------------------------------------------------
if [[ "$VERIFY_ONLY" == false ]]; then
  phase "build and push ${WORKLOAD_NAME} (linux/amd64)"
  started=$(date +%s)
  # buildx needs the same throwaway config for the push; the base image is pulled anonymously.
  if docker buildx build --platform linux/amd64 --provenance=false --sbom=false \
      --tag "$WORKLOAD_REF" --push --metadata-file "$WORK/workload.json" workload/ >"$LOGS/workload-build.log" 2>&1; then
    jq -r '."containerimage.digest"' "$WORK/workload.json" >"$WORKLOAD_DIGEST_FILE"
    ok "$(printf '%-*s' "$name_width" "$WORKLOAD_NAME")  $(head -c 19 "$WORKLOAD_DIGEST_FILE")  built and pushed in $(duration $(( $(date +%s) - started )))"
  else
    tail -n 20 "$LOGS/workload-build.log" >&2
    fail "workload image build or push failed (full log: ${LOGS}/workload-build.log)"
    exit 1
  fi
  phase_done
fi

# --- 4. Verify ----------------------------------------------------------------------------
phase "verify ECR against source"
failed_names=()
for (( i = 0; i < count; i++ )); do
  src="${SRC_REFS[i]}"; dst="${DST_REFS[i]}"; name="${NAMES[i]}"
  label="$(printf '%-*s' "$name_width" "$name")"
  if [[ -n "${PROBLEMS[i]}" ]]; then
    fail "${label}  ${PROBLEMS[i]}"
    failed_names+=("$name")
    continue
  fi
  if [[ -z "${SRC_DIGESTS[i]}" ]]; then
    rc=0; digest_of "$src" || rc=$?
    if (( rc != 0 )); then
      if (( rc == 2 )); then reason="does not exist"; else reason="cannot be read: $(err_line "$RETRY_ERR")"; fi
      fail "${label}  source ${reason}"
      failed_names+=("$name")
      continue
    fi
    SRC_DIGESTS[i]="$DIGEST"
  fi
  rc=0; digest_of "$dst" || rc=$?
  if (( rc == 2 )); then
    fail "${label}  missing from ECR (source ${SRC_DIGESTS[i]:0:19})"
    failed_names+=("$name")
  elif (( rc == 1 )); then
    fail "${label}  cannot read from ECR: $(err_line "$RETRY_ERR")"
    failed_names+=("$name")
  elif [[ "$DIGEST" != "${SRC_DIGESTS[i]}" ]]; then
    fail "${label}  digest differs"
    echo "          ECR     ${DIGEST}"
    echo "          source  ${SRC_DIGESTS[i]}  (${src})"
    failed_names+=("$name")
  else
    ok "${label}  ${DIGEST:0:19}"
  fi
done
# The workload image is compared with the digest its last build recorded.
label="$(printf '%-*s' "$name_width" "$WORKLOAD_NAME")"
want="$(cat "$WORKLOAD_DIGEST_FILE" 2>/dev/null || true)"
rc=0; digest_of "$WORKLOAD_REF" || rc=$?
if [[ -z "$want" ]]; then
  fail "${label}  no recorded build digest: run ./03_images.sh (without --verify-only)"
  failed_names+=("$WORKLOAD_NAME")
elif (( rc == 2 )); then
  fail "${label}  missing from ECR (built ${want:0:19})"
  failed_names+=("$WORKLOAD_NAME")
elif (( rc == 1 )) || [[ "$DIGEST" != "$want" ]]; then
  fail "${label}  ECR ${DIGEST:-unreadable} differs from the build's ${want}"
  failed_names+=("$WORKLOAD_NAME")
else
  ok "${label}  ${DIGEST:0:19}"
fi
count=$((count + 1))
phase_done

# --- Summary ------------------------------------------------------------------------------
printf '\n\033[1m== Timings\033[0m\n%s' "$timings"
printf '  %-30s %9s\n' "total" "$(duration $(( $(date +%s) - script_start )))"
if (( rate_limited > 0 )); then
  echo "Rate limited ${rate_limited} time(s) (HTTP 429); waited $(duration "$waited") in all"
fi
if (( ${#failed_names[@]} > 0 )); then
  echo "${#failed_names[@]} of ${count} images are missing from ECR or could not be checked:" >&2
  for name in "${failed_names[@]}"; do echo "  ${name}" >&2; done
  echo "Run ./03_images.sh again to copy them (details: ${LOG})" >&2
  exit 1
fi
echo "all ${count} images are in ECR with the digest of their source"
