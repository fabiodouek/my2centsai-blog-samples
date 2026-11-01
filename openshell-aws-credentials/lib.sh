# Shared by the scripts in this directory. The cluster and gateway come from ../openshell-eks
# (part 2): its env.sh, the kubectl context it created, and the CLI gateway osh-eks. The part 5
# AWS resources (bucket and role ARNs) come from terraform; 01_setup.sh appends them to env.sh.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL="${HERE}/../openshell-eks"
[[ -f "${INSTALL}/env.sh" ]] || { echo "${INSTALL}/env.sh not found: run part 2 (../openshell-eks) first" >&2; exit 1; }
# shellcheck source=/dev/null
source "${INSTALL}/env.sh"
NS="openshell"

# Part 5 facts, written by 01_setup.sh (terraform outputs). Sourced if present so every script
# after 01 sees BUCKET / SANDBOX_ROLE_ARN / GATEWAY_ROLE_ARN without re-reading terraform.
P5_ENV="${HERE}/env.sh"
# shellcheck source=/dev/null
[[ -f "$P5_ENV" ]] && source "$P5_ENV"

# Names: everything this part creates is prefixed p5- so it never collides with part 4's
# github / anthropic / echo-demo providers or its sandboxes.
PROVIDER="p5-aws"

kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
aws() { command aws --region "${AWS_REGION:-us-east-1}" "$@"; }
os() { openshell -g osh-eks "$@"; }   # -g before the subcommand, never after a -- command
os status >/dev/null 2>&1 || { echo "Gateway not reachable at 127.0.0.1:17680: run ../openshell-eks/05_install.sh (or 09_scale.sh up)" >&2; exit 1; }

say() { printf '\n\033[1m$ %s\033[0m\n' "$*"; }
ok()  { printf '  ok    %s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
nocolor() { sed 's/\x1b\[[0-9;]*m//g'; }
now() { python3 -c 'import time; print(f"{time.time():.1f}")'; }
since() { python3 -c "import time; print(f'{time.time() - $1:.1f}')"; }

# The one place the account id could reach output. Mask it everywhere we print or write.
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
mask() { if [[ -n "$ACCOUNT_ID" ]]; then sed "s/${ACCOUNT_ID}/<account>/g"; else cat; fi; }

# Never print a credential. These helpers print only shapes: a length and a SHA-256 prefix.
# Use them on any value that might be a key, token or signature.
sha12() { python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read().rstrip(b"\n")).hexdigest()[:12])'; }
shape() {  # label value  -> "label: len=<n> sha256=<12hex>…"  (never the value itself)
  local label="$1" val="$2" n
  n=${#val}
  printf '  %-30s len=%-5s sha256=%s…\n' "$label" "$n" "$(printf '%s' "$val" | sha12)"
}

sandbox_id() { os sandbox get "$1" -o json 2>/dev/null | jq -r .id; }
in_sb() { local n="$1"; shift; os sandbox exec "$n" --no-login-shell --no-tty -- "$@"; }

# Create a sandbox from the series workload image and wait until it can run commands.
# Extra arguments (providers, policy, labels) go to `sandbox create`.
make_sandbox() {
  local n="$1"; shift
  os sandbox delete "$n" >/dev/null 2>&1
  os sandbox create --name "$n" --detach --no-tty "$@" >/dev/null 2>&1 || { echo "sandbox create ${n} failed" >&2; return 1; }
  for _ in $(seq 1 45); do in_sb "$n" true >/dev/null 2>&1 && return 0; sleep 2; done
  echo "sandbox ${n} not usable" >&2; return 1
}

# Restart the port-forward to the gateway (after anything that restarts the gateway Pod).
# Identical to ../openshell-lifecycle/lib.sh so the main session's expectations hold.
restart_pf() {
  local pidf="${INSTALL}/.port-forward.pid"
  [[ -f "$pidf" ]] && { kill "$(cat "$pidf")" 2>/dev/null; wait "$(cat "$pidf")" 2>/dev/null; }
  kubectl -n "$NS" rollout status statefulset/openshell --timeout=5m >/dev/null
  nohup kubectl -n "$NS" port-forward svc/openshell 17680:8080 >"${INSTALL}/logs/port-forward.log" 2>&1 &
  echo $! >"$pidf"
  disown  # no job-control message when a later run stops it
  for _ in $(seq 1 30); do os status >/dev/null 2>&1 && return 0; sleep 1; done
  echo "gateway not reachable after restarting the port-forward" >&2; return 1
}

# The CA bundle OpenShell sets inside a sandbox (boundary runtime path). curl --cacert and the
# AWS SDKs read it; AWS_CA_BUNDLE is NOT set by OpenShell (05_ca_trust.sh explores that).
CA_BUNDLE="/run/openshell-supervisor-ca/material/ca-bundle.crt"

# The signing policy with ${BUCKET}/${REGION} filled in from env.sh, written to stdout. Scripts
# policy_file writes it to logs/ (the CLI doesn't read a process-substitution path), for --policy.
render_policy() {
  BUCKET="${BUCKET:?BUCKET not set; run 01_setup.sh}" REGION="${AWS_REGION:-us-east-1}" \
    envsubst '${BUCKET} ${REGION}' <"${HERE}/policies/aws-signing.yaml"
}

# Render the policy into logs/ and print the path. An optional provider name replaces p5-aws in
# the credential bindings (06 and 08 use their own providers).
policy_file() {
  local prov="${1:-$PROVIDER}" out="${HERE}/logs/aws-signing.${1:-$PROVIDER}.yaml"
  mkdir -p "${HERE}/logs"
  render_policy | sed "s/provider: ${PROVIDER}/provider: ${prov}/" >"$out" && echo "$out"
}

# Create a sandbox with the p5-aws provider attached and the signing policy applied, plus the
# non-secret region and bucket as env vars so the inner commands can reference $BUCKET and
# $AWS_DEFAULT_REGION. Extra args (a different --policy, more --env) go after the name.
signing_sandbox() {
  local n="$1"; shift
  make_sandbox "$n" --provider "$PROVIDER" --policy "$(policy_file)" \
    --env AWS_DEFAULT_REGION="${AWS_REGION:-us-east-1}" \
    --env AWS_REGION="${AWS_REGION:-us-east-1}" \
    --env BUCKET="${BUCKET:-}" "$@"
}
