#!/usr/bin/env bash
# Copy the container images this series runs into your ECR registry, so the cluster pulls
# from ECR and nothing is fetched from the internet when a sandbox starts.
#
# images.txt lists the source images, one per line. Each one is copied with its whole
# multi-architecture index to <registry>/opensandbox-eks/<name>:<tag>. The repositories don't
# exist beforehand: the ECR repository creation template that 01_cluster.sh sets up creates
# each one on its first push, with scan on push and a lifecycle rule.
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
# The Python workload image comes from Amazon ECR Public, which mirrors the official image
# with the same index digest as Docker Hub (checked: both sha256:aa943ce41142...). That is one
# fewer image to pull through Docker Hub's anonymous limit.
#
# Docker login: the ECR login lives in a throwaway Docker config that is deleted on exit.
# Nothing lands in your keychain, and your own Docker logins are not used, so the pulls from
# docker.io are anonymous. Docker Hub rate-limits anonymous pulls per IP address; when it
# answers 429 the script waits and tries again (15 s, then double, 5 attempts, 15 minutes of
# waiting in all) and says so. A registry that still answers 429 after that isn't asked again
# during the run (every request counts against the limit); the images it serves are listed as
# not checked. Run the script again later: it copies only what is missing.
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

for tool in aws docker; do
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
# For docker.io/opensandbox/server:release-1.2.0 the copy is
# <ECR registry>/<ECR prefix>/server:release-1.2.0: the last part of the path, tag included.
SRC_REFS=()     # source image
DST_REFS=()     # full ECR reference
NAMES=()        # <prefix>/<name>:<tag>, how this script talks about an image
SRC_DIGESTS=()  # filled in as the sources are read
PROBLEMS=()     # why an image could not be copied (empty when nothing went wrong)
name_width=0
while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%%#*}"
  line="${line//[[:space:]]/}"
  [[ -n "$line" ]] || continue
  if [[ "${line##*/}" != *:* || "$line" == *@* ]]; then
    echo "images.txt: '${line}' needs a tag (name:tag), not a digest or a bare name" >&2
    exit 1
  fi
  tail_ref="${line##*/}"
  SRC_REFS+=("$line")
  DST_REFS+=("${ECR_REGISTRY}/${ECR_PREFIX}/${tail_ref}")
  NAMES+=("${ECR_PREFIX}/${tail_ref}")
  SRC_DIGESTS+=("")
  PROBLEMS+=("")
  if (( ${#ECR_PREFIX} + 1 + ${#tail_ref} > name_width )); then name_width=$(( ${#ECR_PREFIX} + 1 + ${#tail_ref} )); fi
done < images.txt
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
blocked=" "     # registries that still answered 429 after the retries: not asked again this run

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

# manifest_digest <ref>: print the digest of the manifest (or index) a reference points to.
# It hashes the manifest exactly as the registry serves it, so it asks for the index only.
# (The formatted output of "imagetools inspect" also reads each platform's manifest, and
# public.ecr.aws answered 429 on one of those.)
manifest_digest() {
  local sum
  # Without "|| return 1" a failed inspect would hash nothing and "succeed".
  sum="$(docker buildx imagetools inspect --raw "$1" | if command -v sha256sum >/dev/null; then sha256sum; else shasum -a 256; fi)" || return 1
  printf 'sha256:%s\n' "${sum%% *}"
}

# host_of <ref>: the registry part of an image reference.
host_of() { printf '%s' "${1%%/*}"; }
# is_blocked <ref>: true when the reference's registry already gave up on us during this run.
is_blocked() { [[ "$blocked" == *" $(host_of "$1") "* ]]; }
# note_rate_limit <ref>: after a failed command, stop asking that registry if it was a 429.
# Every request counts against the limit, so asking again only makes the block last longer.
note_rate_limit() {
  if grep -qiE '429|toomanyrequests|too many requests' <<<"$RETRY_ERR"; then blocked+="$(host_of "$1") "; fi
}

# digest_of <ref>: sets DIGEST to the digest of the manifest or index the reference points to.
# Returns 0 when it found one, 2 when the image doesn't exist (DIGEST is empty), and 1 for any
# other error (the reason is in RETRY_ERR).
DIGEST=""
digest_of() {
  DIGEST=""
  if retry manifest_digest "$1"; then
    DIGEST="$RETRY_OUT"
    [[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] && return 0
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

    if is_blocked "$src"; then
      PROBLEMS[i]="not tried: $(host_of "$src") is rate limiting this run"
      printf '  skip  %s  %s\n' "$label" "${PROBLEMS[i]}"
      continue
    fi
    rc=0; digest_of "$src" || rc=$?
    if (( rc != 0 )); then
      if (( rc == 2 )); then PROBLEMS[i]="source does not exist"; else PROBLEMS[i]="cannot read source: $(err_line "$RETRY_ERR")"; note_rate_limit "$src"; fi
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
      note_rate_limit "$src"
      printf '  FAIL  %s  %s\n' "$label" "${PROBLEMS[i]}"
    fi
  done
  printf '   copied %d, already in ECR %d\n' "$copied" "$present"
  phase_done
fi

# --- 3. Verify ----------------------------------------------------------------------------
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
    if is_blocked "$src"; then
      fail "${label}  source not tried: $(host_of "$src") is rate limiting this run"
      failed_names+=("$name")
      continue
    fi
    rc=0; digest_of "$src" || rc=$?
    if (( rc != 0 )); then
      if (( rc == 2 )); then reason="does not exist"; else reason="cannot be read: $(err_line "$RETRY_ERR")"; note_rate_limit "$src"; fi
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
phase_done

# --- Summary ------------------------------------------------------------------------------
printf '\n\033[1m== Timings\033[0m\n%s' "$timings"
printf '  %-30s %9s\n' "total" "$(duration $(( $(date +%s) - script_start )))"
if (( rate_limited > 0 )); then
  echo "Rate limited ${rate_limited} time(s) (HTTP 429); waited $(duration "$waited") in all"
fi
if (( ${#failed_names[@]} > 0 )); then
  if [[ "$blocked" != " " ]]; then
    echo "Rate limited by:${blocked%?}. Wait for the limit to reset (Docker Hub's is per IP address), then run again." >&2
  fi
  echo "${#failed_names[@]} of ${count} images are missing from ECR or could not be checked:" >&2
  for name in "${failed_names[@]}"; do echo "  ${name}" >&2; done
  echo "Run ./03_images.sh again to copy them (details: ${LOG})" >&2
  exit 1
fi
echo "all ${count} images are in ECR with the digest of their source"
