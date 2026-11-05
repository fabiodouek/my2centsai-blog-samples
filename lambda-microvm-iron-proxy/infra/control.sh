#!/bin/bash
# The `control` sidecar in the iron-proxy task (control_plane=true). It runs as the task role and keeps two files on
# the task's shared volume in step with the control bucket:
#
#   $SHARED/iron-proxy.yaml  iron-proxy's config, from s3://$BUCKET/$CONFIG_KEY
#   $SHARED/creds.json       a credential_process file for the sandbox role named in s3://$BUCKET/$SCOPE_KEY
#                            (`role`, default $DEFAULT_ROLE), assumed with no session policy, or, when the scope's
#                            `session_policy` is true, with its statements plus the proxy's baseline (its own secrets)
#
# Every $POLL_S seconds (30 by default) it fetches both objects if their ETag changed. When the config changed, or the scope
# changed, or the session is 40 minutes old (role chaining caps it at an hour), it updates the files atomically
# and calls iron-proxy's POST /v1/reload on loopback. Then it writes what it applied to s3://$BUCKET/$STATUS_KEY,
# right away after a change and every $HEARTBEAT_S seconds otherwise (its updated_at is the heartbeat), and sleeps.
# Session names are $SESSION_PREFIX-<role>-v<scope version> (proxy-view-only-v7), so CloudTrail shows the scope.
#
# A config that would let sandbox AWS calls reach Secrets Manager is refused (guard, below): iron-proxy fetches its
# own secrets with the same session it signs those calls with. The last good config stays in place.
#
# AWS requests are signed by curl (--aws-sigv4) with the task role's credentials from the ECS endpoint: an aws
# CLI call costs about a second of CPU, a lot for a 0.5 vCPU task that also runs the proxy.
set -uo pipefail

: "${BUCKET:?}" "${ROLE_ARNS_JSON:?}" "${BASELINE_JSON:?}" "${IRON_MANAGEMENT_API_KEY:?}"
DEFAULT_ROLE=${DEFAULT_ROLE:-view-only}
REGION=${AWS_REGION:-us-east-1}
SHARED=${SHARED:-/control}
CONFIG_KEY=${CONFIG_KEY:-iron-proxy.yaml}
SCOPE_KEY=${SCOPE_KEY:-aws-scope.json}
STATUS_KEY=${STATUS_KEY:-status.json}
SESSION_PREFIX=${SESSION_PREFIX:-ws}
POLL_S=${POLL_S:-30}
RETRY_S=${RETRY_S:-2}   # the next round's delay while a reload is still owed (iron-proxy not listening yet, an error)
RENEW_S=${RENEW_S:-2400}
HEARTBEAT_S=${HEARTBEAT_S:-30}
MGMT_URL=${MGMT_URL:-http://127.0.0.1:9092/v1/reload}
S3=${S3_URL:-https://$BUCKET.s3.$REGION.amazonaws.com}
STS=${STS_URL:-https://sts.$REGION.amazonaws.com/}
CREDS_URL=${CREDS_URL:-http://169.254.170.2${AWS_CONTAINER_CREDENTIALS_RELATIVE_URI:-}}

umask 077
mkdir -p "$SHARED"
TMP=$(mktemp -d)

now() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }

# log <event> [--arg name value ...]: one JSON line on stderr, next to iron-proxy's own in CloudWatch
log() {
  local ev=$1; shift
  jq -cn --arg time "$(now)" --arg event "$ev" "$@" '{time: $time, msg: "control", event: $event} + ($ARGS.named | del(.time, .event))' >&2
}

# Credentials and the bearer token go to curl through config files, not the command line.
printf 'header = "Authorization: Bearer %s"\n' "$IRON_MANAGEMENT_API_KEY" > "$TMP/mgmt.curlrc"

CRED_EXP=0
task_creds() {  # the task role's credentials, refreshed 10 minutes before they expire
  (( $(date +%s) < CRED_EXP - 600 )) && return 0
  local j
  j=$(curl -sf --max-time 3 "$CREDS_URL") || { log task_credentials_failed; return 1; }
  jq -r '"user = \"\(.AccessKeyId):\(.SecretAccessKey)\"\nheader = \"x-amz-security-token: \(.Token)\""' <<<"$j" > "$TMP/aws.curlrc"
  CRED_EXP=$(date -d "$(jq -r .Expiration <<<"$j")" +%s)
}

aws_curl() {  # aws_curl <service> <curl args...>: one request signed as the task role
  local svc=$1; shift
  curl -sS --max-time 10 -K "$TMP/aws.curlrc" --aws-sigv4 "aws:amz:$REGION:$svc" "$@"
}

# fetch <key> <etag> <dest>: download s3://$BUCKET/<key> to <dest>.part if its ETag isn't <etag>.
# Prints the new ETag after a download, nothing when unchanged (304). Returns 1 on errors.
fetch() {
  local key=$1 etag=$2 dest=$3 code cond=()
  [[ -n $etag ]] && cond=(-H "If-None-Match: $etag")
  code=$(aws_curl s3 -o "$dest.part" -D "$TMP/headers" -w '%{http_code}' -H "x-amz-content-sha256: UNSIGNED-PAYLOAD" \
    "${cond[@]}" "$S3/$key") || { log s3_get_failed --arg key "$key" --arg error "curl exit $?"; return 1; }
  case $code in
    200) grep -i '^etag:' "$TMP/headers" | cut -d' ' -f2- | tr -d '\r' ;;
    304) rm -f "$dest.part" ;;
    *) log s3_get_failed --arg key "$key" --arg http "$code" --arg error "$(head -c 300 "$dest.part" 2>/dev/null)"
       rm -f "$dest.part"; return 1 ;;
  esac
}

# ---- state reported in status.json
TASK=$(curl -sf --max-time 2 "${ECS_CONTAINER_METADATA_URI_V4:-http://127.0.0.1:1}/task" | jq -r '.TaskARN // empty' | awk -F/ '{print $NF}')
STARTED_AT=$(now)
cfg_etag="" cfg_version="" cfg_synced_at="" cfg_rejected="" cfg_refused=""
scope_etag="" scope_version="" session_name="" source_identity="" assumed_at="" expiration="" scope_error=""
role="" session_policy=""
# STS's AssumeRole answer: SessionTokenUtilization (% of the maximum session token size) and SessionTokenSize
# (bytes) replace the deprecated PackedPolicySize, kept as a fallback for answers without them
token_util="" token_size="" packed=""
renew_at=0
# applied_*_at: when that version first went live (a renewal or repeat reload doesn't move it)
applied_cfg="" applied_scope="" cfg_applied_at="" scope_applied_at=""
reload_at="" reload_http="" reload_ok="" reload_error="" reload_pending=1
dirty=1 last_status=0 owner_logged=0 nap=$POLL_S

# ---- the Secrets Manager guard
# guard <file>: prints why a config must not be applied, or nothing. Refused:
#   - "secretsmanager" anywhere but a secret_id (the secrets' own ARNs);
#   - an aws_auth transform without a non-empty allowed_services (iron-proxy 0.55.0 reads an empty list as every
#     service);
#   - an allowlist entry that matches a Secrets Manager endpoint, wildcards included (*.amazonaws.com, *).
SM_HOSTS=("secretsmanager.$REGION.amazonaws.com" "secretsmanager-fips.$REGION.amazonaws.com" "secretsmanager.amazonaws.com")

section() {  # section <file> <transform name>: the lines of that transform's block, comments dropped
  sed 's/[[:space:]]#.*$//; /^[[:space:]]*#/d' "$1" | awk -v want="$2" '
    /^[[:space:]]*-[[:space:]]*name:/ { n = $0; sub(/^[[:space:]]*-[[:space:]]*name:[[:space:]]*/, "", n)
                                        gsub(/["\047[:space:]]/, "", n); inb = (n == want); next }
    /^[^[:space:]]/ { inb = 0 }
    inb { print }'
}

guard() {
  local f=$1 auth svcs tok h
  if awk 'tolower($0) ~ /secretsmanager/ && $0 !~ /^[[:space:]]*secret_id:/ { bad = 1 } END { exit !bad }' "$f"; then
    echo "mentions secretsmanager outside a secret_id"; return
  fi
  auth=$(section "$f" aws_auth)
  if [[ -n $auth ]]; then
    svcs=$(awk '
      /^[[:space:]]*allowed_services:/ { v = $0; sub(/^[^:]*:[[:space:]]*/, "", v)
        if (v != "") { gsub(/[][",\047]/, " ", v); print v; exit }
        block = 1; next }
      block && /^[[:space:]]*-/ { v = $0; sub(/^[[:space:]]*-[[:space:]]*/, "", v); gsub(/["\047]/, "", v); print v; next }
      block { exit }' <<<"$auth")
    if [[ -z ${svcs//[[:space:]]/} ]]; then
      echo "aws_auth has no allowed_services (iron-proxy reads an empty list as every service)"; return
    fi
  fi
  while read -r tok; do
    [[ -z $tok || $tok == *[/:]* ]] && continue
    for h in "${SM_HOSTS[@]}"; do
      # shellcheck disable=SC2053  # $tok is a pattern on purpose: allowlist entries are wildcards
      if [[ $h == $tok ]]; then echo "the allowlist entry $tok matches $h"; return; fi
    done
  done < <(section "$f" allowlist | sed 's/[][]/ /g' | tr -s ' \t,"\047' '\n')
}

sync_config() {
  local etag ver reason
  etag=$(fetch "$CONFIG_KEY" "$cfg_etag" "$SHARED/iron-proxy.yaml") || return
  [[ -z $etag ]] && return
  ver=$(sed -n '1s/.*config v\([0-9][0-9]*\).*/\1/p' "$SHARED/iron-proxy.yaml.part")
  reason=$(guard "$SHARED/iron-proxy.yaml.part")
  if [[ -n $reason ]]; then
    rm -f "$SHARED/iron-proxy.yaml.part"
    cfg_etag=$etag cfg_rejected=$ver cfg_refused=$reason dirty=1
    log config_refused --arg version "$ver" --arg reason "$reason"
    return
  fi
  mv -f "$SHARED/iron-proxy.yaml.part" "$SHARED/iron-proxy.yaml"
  cfg_etag=$etag cfg_version=$ver cfg_refused=""
  cfg_synced_at=$(now) reload_pending=1 dirty=1
  log config_synced --arg version "$cfg_version" --arg etag "$etag"
}

assume() {
  local ver r arn sp policy name si code args
  ver=$(jq -r '.version // 0' "$SHARED/aws-scope.json") || ver=0
  r=$(jq -r --arg d "$DEFAULT_ROLE" '.role // $d' "$SHARED/aws-scope.json")
  arn=$(jq -r --arg r "$r" '.[$r] // empty' <<<"$ROLE_ARNS_JSON")
  if [[ -z $arn ]]; then
    scope_error="unknown role: $r" renew_at=$(( $(date +%s) + 30 )) dirty=1
    log assume_failed --arg role "$r" --arg error "$scope_error"
    return 1
  fi
  # No session policy unless the scope asks for one (an older scope without the flag but with statements does)
  sp=$(jq -r 'if has("session_policy") then (.session_policy == true) else ((.statements // []) | length > 0) end' \
    "$SHARED/aws-scope.json")
  name="$SESSION_PREFIX-$r-v$ver"
  # SourceIdentity: who applied the scope, so CloudTrail shows it on every sandbox call (2-64 of [\w+=,.@-])
  si=$(jq -j '.updated_by // ""' "$SHARED/aws-scope.json" | tr -c 'A-Za-z0-9_+=,.@-' '-' | cut -c1-64)
  args=(--data-urlencode "Action=AssumeRole" --data-urlencode "Version=2011-06-15"
        --data-urlencode "RoleArn=$arn" --data-urlencode "RoleSessionName=$name"
        --data-urlencode "DurationSeconds=3600")
  if [[ $sp == true ]]; then
    policy=$(jq -c --argjson base "$BASELINE_JSON" \
      '{Version: "2012-10-17", Statement: ([$base] + (.statements // []))}' "$SHARED/aws-scope.json") \
      || { scope_error="aws-scope.json is not valid JSON"; renew_at=$(( $(date +%s) + 30 )) dirty=1; return 1; }
    args+=(--data-urlencode "Policy=$policy")
  fi
  (( ${#si} >= 2 )) && args+=(--data-urlencode "SourceIdentity=$si")
  code=$(aws_curl sts -o "$TMP/assume.json" -w '%{http_code}' -H 'Accept: application/json' "${args[@]}" "$STS") || code=000
  if [[ $code != 200 ]]; then
    scope_error=$(jq -r '(.Error // .ErrorResponse.Error // {}) | "\(.Code // "error"): \(.Message // "")"' "$TMP/assume.json" 2>/dev/null \
      || head -c 300 "$TMP/assume.json")
    scope_error="${scope_error:-HTTP $code}"
    renew_at=$(( $(date +%s) + 30 )) dirty=1
    log assume_failed --arg session "$name" --arg http "$code" --arg error "$scope_error"
    return 1
  fi
  jq '.AssumeRoleResponse.AssumeRoleResult.Credentials as $c | {Version: 1, AccessKeyId: $c.AccessKeyId,
      SecretAccessKey: $c.SecretAccessKey, SessionToken: $c.SessionToken,
      Expiration: ($c.Expiration | if type == "number" then (floor | todate) else . end)}' \
    "$TMP/assume.json" > "$SHARED/creds.json.part" && mv -f "$SHARED/creds.json.part" "$SHARED/creds.json"
  expiration=$(jq -r .Expiration "$SHARED/creds.json")
  token_util=$(jq -r '.AssumeRoleResponse.AssumeRoleResult.SessionTokenUtilization // empty' "$TMP/assume.json")
  token_size=$(jq -r '.AssumeRoleResponse.AssumeRoleResult.SessionTokenSize // empty' "$TMP/assume.json")
  packed=$(jq -r '.AssumeRoleResponse.AssumeRoleResult.PackedPolicySize // empty' "$TMP/assume.json")
  rm -f "$TMP/assume.json"
  scope_version=$ver session_name=$name source_identity=$si assumed_at=$(now) scope_error="" role=$r session_policy=$sp
  renew_at=$(( $(date +%s) + RENEW_S )) reload_pending=1 dirty=1
  log assumed --arg session "$name" --arg role "$r" --arg session_policy "$sp" --arg source_identity "$si" --arg expiration "$expiration" \
    --arg session_token_utilization "$token_util" --arg session_token_size "$token_size" --arg packed_policy_size "$packed"
}

sync_scope() {
  local etag
  etag=$(fetch "$SCOPE_KEY" "$scope_etag" "$SHARED/aws-scope.json") || return
  if [[ -n $etag ]]; then
    mv -f "$SHARED/aws-scope.json.part" "$SHARED/aws-scope.json"
    scope_etag=$etag
    assume
  elif [[ -s $SHARED/aws-scope.json ]] && (( $(date +%s) >= renew_at )); then
    assume
  fi
}

reload() {
  local code
  code=$(curl -s -K "$TMP/mgmt.curlrc" -o "$TMP/reload.json" -w '%{http_code}' --max-time 30 -X POST "$MGMT_URL")
  case $code in
    000) return ;;  # not listening yet: iron-proxy waits for this container, then reads both files at start
    200) reload_ok=true reload_error="" reload_pending=0 cfg_rejected=""
         [[ $applied_cfg != "$cfg_version" ]] && cfg_applied_at=$(now)
         [[ $applied_scope != "$scope_version" ]] && scope_applied_at=$(now)
         applied_cfg=$cfg_version applied_scope=$scope_version
         cp -p "$SHARED/iron-proxy.yaml" "$SHARED/.iron-proxy.yaml.good" ;;
    422) # iron-proxy kept its previous pipeline. Put the last good file back so a restart doesn't load this one.
         reload_ok=false reload_pending=0 cfg_rejected=$cfg_version
         reload_error=$(jq -r '.error // empty' "$TMP/reload.json" 2>/dev/null | head -c 600)
         [[ -s $SHARED/.iron-proxy.yaml.good ]] && cp -p "$SHARED/.iron-proxy.yaml.good" "$SHARED/iron-proxy.yaml" ;;
    *)   reload_ok=false reload_error="HTTP $code" ;;  # retried next round
  esac
  reload_at=$(now) reload_http=$code dirty=1
  log reload --arg http "$code" --arg config_version "$cfg_version" --arg scope_version "$scope_version" --arg error "$reload_error"
}

# During a rollout the old and the new task both run for a while (the old one drains). status.json belongs to the
# newest task: before writing, check whether a task that started later already writes it.
newer_owner() {
  local code
  code=$(aws_curl s3 -o "$TMP/current.json" -w '%{http_code}' -H "x-amz-content-sha256: UNSIGNED-PAYLOAD" "$S3/$STATUS_KEY") \
    || return 1
  [[ $code == 200 ]] || return 1
  jq -e --arg me "$TASK" --arg started "$STARTED_AT" '.task != $me and .task != null and .started_at > $started' \
    "$TMP/current.json" >/dev/null 2>&1
}

write_status() {
  if [[ -n $TASK ]] && newer_owner; then
    (( owner_logged )) || log status_owned_by_newer_task --arg task "$(jq -r .task "$TMP/current.json")"
    owner_logged=1 dirty=0 last_status=$(date +%s)
    return
  fi
  jq -n --arg updated_at "$(now)" --arg task "$TASK" --arg started_at "$STARTED_AT" --arg poll_s "$POLL_S" \
    --arg next_poll_at "$(date -u -d "+$nap seconds" +%Y-%m-%dT%H:%M:%S.%3NZ)" \
    --arg cfg_version "$cfg_version" --arg cfg_etag "$cfg_etag" --arg cfg_synced_at "$cfg_synced_at" \
    --arg applied_cfg "$applied_cfg" --arg applied_scope "$applied_scope" \
    --arg cfg_applied_at "$cfg_applied_at" --arg scope_applied_at "$scope_applied_at" \
    --arg cfg_rejected "$cfg_rejected" --arg cfg_refused "$cfg_refused" --arg scope_version "$scope_version" \
    --arg session_name "$session_name" \
    --arg source_identity "$source_identity" --arg assumed_at "$assumed_at" --arg expiration "$expiration" \
    --arg token_util "$token_util" --arg token_size "$token_size" --arg packed "$packed" --arg scope_error "$scope_error" \
    --arg role "$role" --arg session_policy "$session_policy" --arg reload_at "$reload_at" --arg reload_http "$reload_http" \
    --arg reload_ok "$reload_ok" --arg reload_error "$reload_error" --argjson pending "$reload_pending" '
    def s: if . == "" then null else . end;
    def n: if . == "" then null else tonumber end;
    {updated_at: $updated_at, task: ($task | s), started_at: $started_at, poll_s: ($poll_s | n),
     next_poll_at: $next_poll_at,
     config: {synced_version: ($cfg_version | n), etag: ($cfg_etag | s), synced_at: ($cfg_synced_at | s),
              applied_version: ($applied_cfg | n), applied_at: ($cfg_applied_at | s), rejected_version: ($cfg_rejected | n),
              refused_reason: ($cfg_refused | s)},
     scope: {synced_version: ($scope_version | n), applied_version: ($applied_scope | n),
             applied_at: ($scope_applied_at | s), role: ($role | s),
             session_policy: (if $session_policy == "" then null else $session_policy == "true" end),
             session_name: ($session_name | s),
             source_identity: ($source_identity | s), assumed_at: ($assumed_at | s), expiration: ($expiration | s),
             session_token_utilization: ($token_util | n), session_token_size: ($token_size | n),
             packed_policy_size: ($packed | n), error: ($scope_error | s)},
     reload: {at: ($reload_at | s), http_status: ($reload_http | n),
              ok: (if $reload_ok == "" then null else $reload_ok == "true" end), error: ($reload_error | s),
              pending: ($pending == 1)}}' > "$TMP/status.json"
  local code
  code=$(aws_curl s3 -o "$TMP/put.out" -w '%{http_code}' --upload-file "$TMP/status.json" \
    -H 'Content-Type: application/json' -H "x-amz-content-sha256: UNSIGNED-PAYLOAD" "$S3/$STATUS_KEY") || code=000
  if [[ $code == 200 ]]; then
    dirty=0 last_status=$(date +%s)
  else
    log status_put_failed --arg http "$code" --arg error "$(head -c 300 "$TMP/put.out" 2>/dev/null)"
  fi
}

# bash is PID 1 here, which ignores SIGTERM unless trapped: stop when ECS stops the task. The sleep runs in the
# background so the trap fires at once instead of after up to $POLL_S seconds (ECS kills the container after 30 s).
trap 'log stopping; exit 0' TERM INT
log started --arg task "$TASK" --arg bucket_key "$CONFIG_KEY" --arg poll_s "$POLL_S"
while :; do
  if task_creds; then
    sync_config
    sync_scope
  fi
  if (( reload_pending )) && [[ -s $SHARED/iron-proxy.yaml && -s $SHARED/creds.json ]]; then
    reload
  fi
  # A reload still owed (at start iron-proxy isn't listening until this container is healthy) is retried soon,
  # so status.json doesn't wait a whole interval to report a config iron-proxy already runs
  nap=$POLL_S
  (( reload_pending && RETRY_S < POLL_S )) && nap=$RETRY_S
  if (( dirty || $(date +%s) - last_status >= HEARTBEAT_S )); then
    task_creds && write_status
  fi
  sleep "$nap" &
  wait $!
done
