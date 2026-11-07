#!/usr/bin/env bash
# Install OpenSandbox 1.2.0 on the cluster: the base chart (CRDs and their RBAC), the
# controller and the lifecycle server. The charts aren't published to a Helm repository, so
# they come from the release-1.2.0 git tag, checked out into .src/OpenSandbox (gitignored;
# a shallow fetch, skipped when it is already there).
#
# Before the charts, the script applies:
#  - the namespaces opensandbox-system and opensandbox (manifests/namespace-opensandbox.yaml)
#  - an encrypted gp3 StorageClass and the server's SQLite PVC
#    (manifests/storageclass-gp3-encrypted.yaml, manifests/server-pvc.yaml)
#  - the BatchSandbox template ConfigMap (manifests/batchsandbox-template.yaml)
#  - the Secret opensandbox-server-auth with the API key, OSB_API_KEY in .env. If .env has no
#    key, the script generates a random one and writes it there (mode 600). It never prints it.
#
# values/*.yaml use ${ECR_REGISTRY} and ${ECR_PREFIX}; the script fills them in from env.sh
# into a temporary directory, so no account ID is stored in the repository. When
# values/server-gateway.yaml (written by 05_gateway.sh) exists, the server release gets it too,
# so running this script again keeps the gateway.
#
# Safe to run again: unchanged manifests are reported as unchanged, each Helm release gets a
# new revision with the same manifests, and no pod restarts. The server is replaced (its
# Deployment uses Recreate) when its config, the template or the API key changes, because it
# reads all three once at startup.
#
# The server runs sandboxes with runc for now ([secure_runtime] is not set).
set -euo pipefail
cd "$(dirname "$0")"

OSB_VERSION="1.2.0"
OSB_TAG="release-${OSB_VERSION}"
OSB_GIT="https://github.com/opensandbox-group/OpenSandbox.git"
SRC=".src/OpenSandbox"
CHARTS="${SRC}/manifests/charts"
NS="opensandbox-system"           # controller, server and their objects
SANDBOX_NS="opensandbox"          # where sandboxes run ([kubernetes] namespace)
SECRET="opensandbox-server-auth"  # key api-key, read by values/server.yaml
PVC="opensandbox-server-data"
TEMPLATE_CM="opensandbox-batchsandbox-template"
GATEWAY_VALUES="values/server-gateway.yaml"  # written by 05_gateway.sh
ENV_FILE=".env"
HELM_TIMEOUT="${HELM_TIMEOUT:-5m}"   # per release, for its pods to become ready
LOGS="logs"         # full Helm output (gitignored)

for tool in git kubectl helm jq envsubst openssl aws; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
[[ -f env.sh ]] || { echo "env.sh not found: run ./01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
: "${KUBE_CONTEXT:?env.sh has no KUBE_CONTEXT: run ./01_cluster.sh again}"
: "${ECR_REGISTRY:?env.sh has no ECR_REGISTRY: run ./01_cluster.sh again}"
: "${ECR_PREFIX:?env.sh has no ECR_PREFIX: run ./01_cluster.sh again}"
: "${AWS_REGION:?env.sh has no AWS_REGION: run ./01_cluster.sh again}"
# Every kubectl and helm call names the context, so switching contexts in another terminal
# can't point this script at a different cluster.
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
helm() { command helm --kube-context "$KUBE_CONTEXT" "$@"; }
kubectl get --raw /readyz >/dev/null 2>&1 \
  || { echo "cannot reach context '${KUBE_CONTEXT}': run ./01_cluster.sh first" >&2; exit 1; }
mkdir -p "$LOGS"
RENDERED="$(mktemp -d)"   # values files with the registry filled in
partial=""                # a source checkout in progress
env_tmp=""                # a new .env being written (it holds the API key)
trap 'rm -rf "$RENDERED"; [[ -z "$partial" ]] || rm -rf "$partial"; [[ -z "$env_tmp" ]] || rm -f "$env_tmp"' EXIT
trap 'exit 130' INT TERM

# --- output and timing helpers -------------------------------------------------
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
failures=0
ok()   { printf '  ok    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }
sha() { openssl dgst -sha256 | awk '{ print substr($NF, 1, 16) }'; }  # stdin -> short hash

# --- 1. Source ------------------------------------------------------------------
phase "OpenSandbox ${OSB_TAG} source"
if [[ -d "$SRC/.git" ]]; then
  head="$(git -C "$SRC" rev-parse HEAD)"
  tag_commit="$(git -C "$SRC" rev-parse -q --verify "refs/tags/${OSB_TAG}^{commit}" || true)"
  if [[ "$head" != "$tag_commit" ]]; then
    echo "${SRC} is not at ${OSB_TAG} (HEAD ${head:0:12}): delete it and run this again" >&2
    exit 1
  fi
  echo "  ${SRC} already at ${OSB_TAG} (${head:0:12}), not cloned again"
  if [[ -n "$(git -C "$SRC" status --porcelain)" ]]; then warn "${SRC} has local changes"; fi
elif [[ -e "$SRC" ]]; then
  echo "${SRC} exists but is not a git clone: delete it and run this again" >&2
  exit 1
else
  # A shallow fetch of the tag alone, the same as git clone --depth 1 --branch, which also
  # prints "warning: refs/tags/release-1.2.0 ... is not a commit!" for an annotated tag like
  # this one. It is checked out in a temporary directory and moved into place when complete,
  # so an interrupted run leaves no half-made checkout behind.
  mkdir -p "$(dirname "$SRC")"
  partial="$(mktemp -d "${SRC}.partial.XXXXXX")"
  git init --quiet --initial-branch=main "$partial"
  git -C "$partial" remote add origin "$OSB_GIT"
  git -C "$partial" fetch --quiet --depth 1 origin "refs/tags/${OSB_TAG}:refs/tags/${OSB_TAG}"
  git -C "$partial" -c advice.detachedHead=false checkout --quiet "${OSB_TAG}^{commit}"
  mv "$partial" "$SRC"
  partial=""
  echo "  fetched ${OSB_GIT} at ${OSB_TAG} ($(git -C "$SRC" rev-parse --short=12 HEAD)) into ${SRC}"
fi
for chart in base controller server; do
  v="$(helm show chart "$CHARTS/$chart" | sed -n 's/^version: //p')"
  if [[ "$v" != "$OSB_VERSION" ]]; then echo "chart ${chart} is version ${v}, expected ${OSB_VERSION}" >&2; exit 1; fi
done
echo "  charts base, controller and server: version ${OSB_VERSION}"
phase_done

# --- 2. Objects the charts need -------------------------------------------------
phase "namespaces, storage, API key"
for f in manifests/namespace-opensandbox.yaml manifests/storageclass-gp3-encrypted.yaml \
         manifests/server-pvc.yaml manifests/batchsandbox-template.yaml; do
  kubectl apply -f "$f" | sed 's/^/  /'
done

# The API key: the last OSB_API_KEY= line in .env (optionally quoted), or a new random key.
key=""
if [[ -f "$ENV_FILE" ]]; then key="$(sed -n 's/^OSB_API_KEY=//p' "$ENV_FILE" | tail -n 1)"; fi
key="${key%\"}"; key="${key#\"}"; key="${key%\'}"; key="${key#\'}"
if [[ -z "$key" ]]; then
  key="$(openssl rand -hex 32)"
  # Rewrite .env with the new key in place of any empty OSB_API_KEY= line, keeping every other
  # line. printf is a shell builtin, so the key never appears in a process's arguments. The
  # EXIT trap removes the temporary file if the run stops before the mv (.gitignore has .env.*).
  env_tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  written=0
  if [[ -f "$ENV_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" == OSB_API_KEY=* ]]; then
        if (( ! written )); then printf 'OSB_API_KEY=%s\n' "$key"; written=1; fi
      else
        printf '%s\n' "$line"
      fi
    done <"$ENV_FILE" >"$env_tmp"
  fi
  if (( ! written )); then printf 'OSB_API_KEY=%s\n' "$key" >>"$env_tmp"; fi
  chmod 600 "$env_tmp"
  mv "$env_tmp" "$ENV_FILE"
  env_tmp=""
  echo "  API key: none in ${ENV_FILE}, generated a random one and saved it there as OSB_API_KEY (mode 600)"
else
  echo "  API key: OSB_API_KEY from ${ENV_FILE}"
fi
if [[ ! "$key" =~ ^[A-Za-z0-9._~+/=-]{16,}$ ]]; then
  echo "OSB_API_KEY in ${ENV_FILE} must be at least 16 characters: letters, digits and . _ ~ + / = -" >&2
  exit 1
fi
# The Secret goes through stdin and server-side apply, so the key is in no process argument and
# no last-applied-configuration annotation. It is only written when the key changed.
key_b64="$(printf '%s' "$key" | base64 | tr -d '\n')"
current_b64="$(kubectl -n "$NS" get secret "$SECRET" -o jsonpath='{.data.api-key}' 2>/dev/null || true)"
if [[ "$current_b64" == "$key_b64" ]]; then
  echo "  secret/${SECRET} unchanged"
else
  kubectl apply --server-side --force-conflicts --field-manager=04_install -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ${SECRET}
  namespace: ${NS}
  labels:
    app.kubernetes.io/part-of: opensandbox
type: Opaque
data:
  api-key: ${key_b64}
EOF
  if [[ -z "$current_b64" ]]; then echo "  secret/${SECRET} created"; else echo "  secret/${SECRET} updated with the new key"; fi
fi
unset key key_b64 current_b64
# Two pod annotations restart the server when the key or the template changes: the Secret's
# resourceVersion (it only changes when the Secret is written) and a hash of the template.
key_version="$(kubectl -n "$NS" get secret "$SECRET" -o jsonpath='{.metadata.resourceVersion}')"
template_sha="$(kubectl -n "$NS" get configmap "$TEMPLATE_CM" -o json | jq -cS '.data' | sha)"
phase_done

# --- 3. Helm releases -----------------------------------------------------------
# Fill in the registry: values/<name>.yaml -> $RENDERED/<name>.yaml
# shellcheck disable=SC2016  # envsubst and grep take ${...} literally
for f in values/base.yaml values/controller.yaml values/server.yaml; do
  envsubst '${ECR_REGISTRY} ${ECR_PREFIX}' <"$f" >"$RENDERED/${f#values/}"
  if grep -n '\${' "$RENDERED/${f#values/}" >&2; then echo "${f}: unfilled \${...} above" >&2; exit 1; fi
done

# {release: {uid, name, restarts}} for the pods in $NS, keyed by app.kubernetes.io/instance
pod_map() {
  kubectl -n "$NS" get pods -o json | jq -c '[.items[] | select(.metadata.deletionTimestamp == null)
    | {key: (.metadata.labels["app.kubernetes.io/instance"] // .metadata.name),
       value: {uid: .metadata.uid, name: .metadata.name,
               restarts: ([.status.containerStatuses[]?.restartCount] | add // 0)}}] | from_entries'
}
release_status() {  # release -> "revision status", or nothing if it isn't installed
  helm -n "$NS" status "$1" -o json 2>/dev/null | jq -r '"\(.version) \(.info.status)"' 2>/dev/null || true
}
diagnose() {  # what to look at when a release doesn't become ready
  echo "--- pods in ${NS}" >&2
  kubectl -n "$NS" get pods -o wide >&2 || true
  for pod in $(kubectl -n "$NS" get pods -o name 2>/dev/null || true); do
    if kubectl -n "$NS" get "$pod" -o json | jq -e 'all(.status.containerStatuses[]?; .ready)' >/dev/null 2>&1; then continue; fi
    echo "--- ${pod}: events" >&2
    kubectl -n "$NS" get events --field-selector "involvedObject.name=${pod#pod/}" \
      -o custom-columns='LAST SEEN:.lastTimestamp,REASON:.reason,MESSAGE:.message' 2>/dev/null | tail -n 8 >&2 || true
    echo "--- ${pod}: last log lines (previous container first)" >&2
    kubectl -n "$NS" logs "$pod" --previous --tail=10 2>/dev/null >&2 || true
    kubectl -n "$NS" logs "$pod" --tail=10 2>/dev/null >&2 || true
  done
}
# helm upgrade --install, wait for the release's pods, then say what changed: the revision,
# whether the manifests differ from the previous revision, and whether the pod was replaced
# or restarted.
release() {
  local name="$1" chart="$2" before prev_rev prev_status rev log="$LOGS/helm-$1.log"
  shift 2
  before="$(pod_map)"
  read -r prev_rev prev_status <<<"$(release_status "$name")"
  if [[ "${prev_status:-}" == pending-* ]]; then
    echo "release ${name} is stuck in ${prev_status} (an earlier helm run was killed):" >&2
    echo "  helm --kube-context ${KUBE_CONTEXT} -n ${NS} rollback ${name}   (or uninstall it if it never deployed), then run this again" >&2
    exit 1
  fi
  if ! helm upgrade --install "$name" "$CHARTS/$chart" --namespace "$NS" "$@" \
      --wait --timeout "$HELM_TIMEOUT" >"$log" 2>&1; then
    tail -n 20 "$log" >&2
    diagnose
    exit 1
  fi
  read -r rev _ <<<"$(release_status "$name")"
  if [[ -z "${prev_rev:-}" ]]; then
    echo "  ${name}: installed, revision ${rev}"
  elif diff <(helm -n "$NS" get manifest "$name" --revision "$prev_rev") \
            <(helm -n "$NS" get manifest "$name" --revision "$rev") >/dev/null; then
    echo "  ${name}: revision ${rev}, manifests identical to revision ${prev_rev}"
  else
    echo "  ${name}: revision ${rev}, manifests changed since revision ${prev_rev}"
  fi
  jq -rn --arg r "$name" --argjson a "$before" --argjson b "$(pod_map)" '
    ($a[$r]) as $old | ($b[$r]) as $new
    | if $new == null then empty
      elif $old == null then "  pod \($new.name) started"
      elif $old.uid != $new.uid then "  pod \($new.name) replaced \($old.name)"
      elif $new.restarts > $old.restarts then "  WARN  pod \($new.name) restarted \($new.restarts - $old.restarts) time(s)"
      else "  pod \($new.name) unchanged (\($new.restarts) restarts)" end'
}

phase "base chart"
release opensandbox-base base -f "$RENDERED/base.yaml"
phase_done

phase "controller"
release opensandbox-controller controller -f "$RENDERED/controller.yaml"
phase_done

phase "server"
server_values=(-f "$RENDERED/server.yaml")
if [[ -f "$GATEWAY_VALUES" ]]; then
  server_values+=(-f "$GATEWAY_VALUES")
  echo "  with ${GATEWAY_VALUES} (the ingress gateway, from ./05_gateway.sh)"
fi
release opensandbox-server server "${server_values[@]}" \
  --set-string "server.podAnnotations.opensandbox-eks/api-key-version=${key_version}" \
  --set-string "server.podAnnotations.opensandbox-eks/template-sha256=${template_sha}"
phase_done

# --- 4. Checks ------------------------------------------------------------------
phase "checks"
releases="$(helm -n "$NS" list -o json)"
row='  %-24s %-9s %-10s %-30s %s\n'
# shellcheck disable=SC2059  # the format is $row
printf "$row" NAME REVISION STATUS CHART "APP VERSION"
jq -r '.[] | [.name, .revision, .status, .chart, .app_version] | @tsv' <<<"$releases" \
  | while IFS=$'\t' read -r name rev status chart app; do
      # shellcheck disable=SC2059
      printf "$row" "$name" "$rev" "$status" "$chart" "$app"
    done
for r in opensandbox-base opensandbox-controller opensandbox-server; do
  s="$(jq -r --arg r "$r" '.[] | select(.name == $r) | .status' <<<"$releases")"
  if [[ "$s" == deployed ]]; then ok "release ${r} deployed"; else fail "release ${r}: ${s:-not installed}"; fi
done

for d in opensandbox-controller-manager opensandbox-server; do
  line="$(kubectl -n "$NS" get pods -o json | jq -r --arg d "$d" '
    [.items[] | select(.metadata.deletionTimestamp == null and (.metadata.name | startswith($d + "-")))]
    | if length == 0 then "none" else .[0] |
        "\(.metadata.name) \(.status.phase) \(all(.status.containerStatuses[]?; .ready)) \([.status.containerStatuses[]?.restartCount] | add // 0) \(.spec.nodeName // "-")" end')"
  read -r pod phase_ ready restarts node <<<"$line"
  if [[ "$pod" == none ]]; then fail "${d}: no pod"; continue; fi
  zone="$(kubectl get node "$node" -o jsonpath='{.metadata.labels.topology\.kubernetes\.io/zone}' 2>/dev/null || true)"
  if [[ "$phase_" == Running && "$ready" == true ]]; then
    ok "${pod} Running and ready, ${restarts} restarts, on ${node} (${zone:-?})"
  else
    fail "${pod} ${phase_}, ready=${ready}, ${restarts} restarts"
  fi
done

# The server's own endpoints, from inside its pod (kubectl exec goes through the kubelet).
# /health and /version need no API key. The API server's service proxy can't reach the server
# here: the node security group lets the EKS control plane in on 443, 10250 and a few webhook
# ports only, not on 80.
probe='import urllib.request as u
for p in ("/health", "/version"):
    print(u.urlopen("http://127.0.0.1" + p, timeout=5).read().decode())'
answers="$(kubectl -n "$NS" exec deploy/opensandbox-server -c main -- python -c "$probe" 2>/dev/null || true)"
health="$(sed -n 1p <<<"$answers")"
version="$(sed -n 2p <<<"$answers" | jq -r '.version // empty' 2>/dev/null || true)"
if [[ "$(jq -r '.status // empty' <<<"$health" 2>/dev/null)" == healthy ]]; then ok "server /health: ${health}"; else fail "server /health: ${health:-no answer}"; fi
if [[ "$version" == "$OSB_VERSION" ]]; then ok "server /version: ${version}"; else fail "server /version: ${version:-no answer} (expected ${OSB_VERSION})"; fi

# Every image the controller and server releases use, including the two inside the server's
# config.toml, must come from our ECR registry. Only repository:tag is printed.
refs="$( { helm -n "$NS" get manifest opensandbox-controller; helm -n "$NS" get manifest opensandbox-server; } \
  | sed -n -E 's/^ *(- )?image: *"?([^" ]+)"?.*/\2/p; s/.*--image-committer-image=([^" ]+).*/\1/p; s/^ *(execd_image|image) *= *"([^"]+)".*/\2/p' \
  | sort -u)"
bad="$(grep -v "^${ECR_REGISTRY}/${ECR_PREFIX}/" <<<"$refs" || true)"
# shellcheck disable=SC2001  # one ref per line, each with the prefix at the start
names="$(sed "s|^${ECR_REGISTRY}/${ECR_PREFIX}/||" <<<"$refs" | tr '\n' ' ')"
if [[ -n "$refs" && -z "$bad" ]]; then
  ok "all $(wc -l <<<"$refs" | tr -d ' ') images come from ECR (${ECR_PREFIX}/): ${names% }"
else
  fail "images not from ECR: $(tr '\n' ' ' <<<"${bad:-none found}")"
fi

pv="$(kubectl -n "$NS" get pvc "$PVC" -o jsonpath='{.spec.volumeName}' 2>/dev/null || true)"
pvc_phase="$(kubectl -n "$NS" get pvc "$PVC" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
vol="$(kubectl get pv "${pv:-none}" -o jsonpath='{.spec.csi.volumeHandle}' 2>/dev/null || true)"
if [[ "$pvc_phase" == Bound && -n "$vol" ]]; then
  # A failed lookup (expired credentials, another profile) is not the same as "not encrypted".
  # Only the AWS error code, or the first sentence of the error, is printed: some AWS error
  # messages include the caller's ARN.
  if vol_info="$(aws ec2 describe-volumes --region "$AWS_REGION" --volume-ids "$vol" \
      --query 'Volumes[0].[Encrypted, VolumeType, Size, AvailabilityZone]' --output text 2>"$RENDERED/aws.err")"; then
    read -r enc vtype size az <<<"$vol_info"
    if [[ "$enc" == True ]]; then
      ok "PVC ${PVC} Bound to EBS ${vol}: ${size} GiB ${vtype}, encrypted, ${az}"
    else
      fail "PVC ${PVC} Bound to EBS ${vol}, but the volume is not encrypted (Encrypted: ${enc:-none})"
    fi
  else
    aws_error="$(sed -n 's/.*An error occurred (\([^)]*\)).*/\1/p' "$RENDERED/aws.err" | sed -n 1p)"
    if [[ -z "$aws_error" ]]; then aws_error="$(grep -m 1 . "$RENDERED/aws.err" || true)"; aws_error="${aws_error%%. *}"; fi
    fail "PVC ${PVC} Bound to EBS ${vol}: could not check that it is encrypted, aws ec2 describe-volumes failed (${aws_error:-no error message})"
  fi
else
  fail "PVC ${PVC}: ${pvc_phase:-missing}"
fi
db="$(kubectl -n "$NS" exec deploy/opensandbox-server -c main -- stat -c '%s bytes, modified %y' /data/opensandbox.db 2>/dev/null || true)"
if [[ -n "$db" ]]; then ok "/data/opensandbox.db on the PVC: ${db%%.*}"; else fail "no /data/opensandbox.db in the server pod"; fi

crds="$(kubectl get crd -o name)"
n_osb="$(grep -c '\.sandbox\.opensandbox\.io$' <<<"$crds" || true)"
n_fsb="$(grep -c '\.sandbox\.fast\.io$' <<<"$crds" || true)"
if (( n_osb == 3 )); then ok "CRDs: ${n_osb} sandbox.opensandbox.io (BatchSandbox, Pool, SandboxSnapshot), ${n_fsb} sandbox.fast.io"; else fail "${n_osb} sandbox.opensandbox.io CRDs, expected 3"; fi
psa="$(kubectl get namespace "$SANDBOX_NS" -o jsonpath='{.metadata.labels.pod-security\.kubernetes\.io/enforce}' 2>/dev/null || true)"
if [[ -n "$psa" ]]; then ok "sandbox namespace ${SANDBOX_NS} (Pod Security: ${psa})"; else fail "sandbox namespace ${SANDBOX_NS} missing or unlabelled"; fi

# What the server runs with (none of this is secret)
kubectl -n "$NS" get deploy opensandbox-controller-manager opensandbox-server -o json | jq -r '.items[]
  | .spec.template.spec.containers[0].resources as $r
  | "  \(.metadata.name): requests \($r.requests.cpu) CPU / \($r.requests.memory), limits \($r.limits.cpu) / \($r.limits.memory)"'
toml="$(kubectl -n "$NS" get configmap opensandbox-server-config -o jsonpath='{.data.config\.toml}')"
tv() { awk -v s="[$1]" -v k="$2" '$0 == s { in_s = 1; next } /^\[/ { in_s = 0 }
  in_s && $1 == k { sub(/^[^=]*= */, ""); gsub(/"/, ""); print; exit }' <<<"$toml"; }
echo "  config.toml: runtime $(tv runtime type), sandboxes in namespace $(tv kubernetes namespace), template $(tv kubernetes batchsandbox_template_file)"
echo "               store $(tv store type) at $(tv store path), egress $(tv egress mode), ingress $(tv ingress mode)"
phase_done

# --- Summary --------------------------------------------------------------------
printf '\n\033[1m== Timings\033[0m\n%s' "$timings"
printf '  %-30s %9s\n' "total" "$(duration $(( $(date +%s) - script_start )))"
if (( failures > 0 )); then
  echo "${failures} check(s) failed" >&2
  exit 1
fi
echo "OpenSandbox ${OSB_VERSION} is running. Reach the API with:"
echo "  kubectl --context ${KUBE_CONTEXT} -n ${NS} port-forward svc/opensandbox-server 8080:80"
echo "and the header OPEN-SANDBOX-API-KEY set to OSB_API_KEY from ${ENV_FILE}"
