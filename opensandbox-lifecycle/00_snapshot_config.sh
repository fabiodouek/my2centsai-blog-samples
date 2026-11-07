#!/usr/bin/env bash
# Turn on pause and resume, with Amazon ECR as the snapshot registry. Three steps:
#
#  1. The ECR repository opensandbox-eks/snapshots. Every snapshot goes into this one repository
#     (values/controller-snapshot.yaml puts the sandbox and container names in the tag). When it
#     does not exist, the script creates it with the settings of the registry's repository creation
#     template for opensandbox-eks/ (part 2's Terraform: tag mutability, encryption, tags, the
#     lifecycle rule). An explicit create-repository does not apply that template by itself; only
#     a push to a missing repository does.
#  2. The Secret opensandbox-snapshot-ecr (type kubernetes.io/dockerconfigjson) in the sandbox
#     namespace opensandbox, with a fresh token from `aws ecr get-login-password`. The commit Job
#     reads registry credentials only from such a Secret, and an ECR token lasts 12 hours: run
#     this script again before every session that pauses sandboxes. The token is never printed
#     and never put on a command line.
#  3. A Helm upgrade of the controller release (opensandbox-controller) with part 2's
#     values/controller.yaml plus values/controller-snapshot.yaml. This is a controller upgrade:
#     the first run replaces the controller pod. Later runs leave the manifests identical and the
#     pod alone, so a re-run only refreshes the token.
#
# Needs ../opensandbox-eks/01_cluster.sh to 04_install.sh to have run. Running part 2's
# 04_install.sh again resets the controller to values/controller.yaml alone (no snapshots): run
# this script again after it.
set -euo pipefail
cd "$(dirname "$0")"

EKS="../opensandbox-eks"
CHART="${EKS}/.src/OpenSandbox/manifests/charts/controller"
NS="opensandbox-system"
SANDBOX_NS="opensandbox"
RELEASE="opensandbox-controller"
SECRET="opensandbox-snapshot-ecr"
TOKEN_HOURS=12
HELM_TIMEOUT="${HELM_TIMEOUT:-5m}"
LOGS="logs"

[[ -f "${EKS}/env.sh" ]] || { echo "run ../opensandbox-eks/01_cluster.sh first" >&2; exit 1; }
for tool in aws kubectl helm jq envsubst base64 curl; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
# shellcheck source=/dev/null
source "${EKS}/env.sh"
: "${KUBE_CONTEXT:?env.sh has no KUBE_CONTEXT}" "${ECR_REGISTRY:?env.sh has no ECR_REGISTRY}"
: "${ECR_PREFIX:?env.sh has no ECR_PREFIX}" "${AWS_REGION:?env.sh has no AWS_REGION}"
REPO="${ECR_PREFIX}/snapshots"
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
helm() { command helm --kube-context "$KUBE_CONTEXT" "$@"; }
kubectl get --raw /readyz >/dev/null 2>&1 \
  || { echo "cannot reach context '${KUBE_CONTEXT}': is the cluster up? (../opensandbox-eks/08_scale.sh up)" >&2; exit 1; }
[[ -d "$CHART" ]] || { echo "${CHART} not found: run ../opensandbox-eks/04_install.sh first" >&2; exit 1; }
kubectl get namespace "$SANDBOX_NS" >/dev/null 2>&1 \
  || { echo "namespace ${SANDBOX_NS} not found: run ../opensandbox-eks/04_install.sh first" >&2; exit 1; }
mkdir -p "$LOGS"
RENDERED="$(mktemp -d)"
trap 'rm -rf "$RENDERED"' EXIT
trap 'exit 130' INT TERM

script_start=$(date +%s)
timings=""
phase_name=""
phase_start=0
phase() { phase_name="$1"; phase_start=$(date +%s); printf '\n\033[1m== %s\033[0m\n' "$1"; }
phase_done() {
  local took=$(( $(date +%s) - phase_start ))
  timings+="$(printf '  %-44s %5ss' "$phase_name" "$took")"$'\n'
  printf '   took %ss\n' "$took"
}
failures=0
ok()   { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }
host_free() { sed "s|${ECR_REGISTRY}|<registry>|g"; }   # the registry host holds the account ID
aws_error() { sed -n 's/.*An error occurred (\([^)]*\)).*/\1/p' "$1" | sed -n 1p; }

# --- 1. the repository ------------------------------------------------------------------
phase "ECR repository ${REPO}"
if info="$(aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$REPO" \
    --query 'repositories[0].createdAt' --output text 2>"$RENDERED/aws.err")"; then
  count="$(aws ecr list-images --region "$AWS_REGION" --repository-name "$REPO" --query 'length(imageIds)' --output text)"
  echo "  exists (created ${info%%.*}), ${count} image(s)"
elif [[ "$(aws_error "$RENDERED/aws.err")" == RepositoryNotFoundException ]]; then
  template="$(aws ecr describe-repository-creation-templates --region "$AWS_REGION" --prefixes "$ECR_PREFIX" \
    --query 'repositoryCreationTemplates[0]' --output json 2>/dev/null || echo null)"
  if [[ "$template" == null || -z "$template" ]]; then
    echo "  no repository creation template for ${ECR_PREFIX}/ (part 2's Terraform makes one): using ECR defaults"
    template='{}'
  fi
  args=(--image-tag-mutability "$(jq -r '.imageTagMutability // "MUTABLE"' <<<"$template")"
        --encryption-configuration "$(jq -c '.encryptionConfiguration // {"encryptionType": "AES256"}' <<<"$template")")
  tags="$(jq -c '.resourceTags // []' <<<"$template")"
  if [[ "$tags" != "[]" ]]; then args+=(--tags "$tags"); fi
  aws ecr create-repository --region "$AWS_REGION" --repository-name "$REPO" "${args[@]}" >/dev/null
  policy="$(jq -r '.lifecyclePolicy // empty' <<<"$template")"
  if [[ -n "$policy" ]]; then
    aws ecr put-lifecycle-policy --region "$AWS_REGION" --repository-name "$REPO" --lifecycle-policy-text "$policy" >/dev/null
  fi
  echo "  created, with the creation template's settings: $(jq -r '[.imageTagMutability, (.encryptionConfiguration.encryptionType // empty), ((.resourceTags // []) | map(.Key + "=" + .Value) | join(","))] | map(select(. != "")) | join(", ")' <<<"$template")"
  if [[ -n "$policy" ]]; then echo "  lifecycle rule: $(jq -r '.rules[0].description' <<<"$policy")"; fi
else
  echo "aws ecr describe-repositories failed: $(aws_error "$RENDERED/aws.err")" >&2
  exit 1
fi
phase_done

# --- 2. the registry Secret ------------------------------------------------------------
phase "Secret ${SANDBOX_NS}/${SECRET}"
old_type="$(kubectl -n "$SANDBOX_NS" get secret "$SECRET" -o jsonpath='{.type}' 2>/dev/null || true)"
old_issued="$(kubectl -n "$SANDBOX_NS" get secret "$SECRET" -o jsonpath='{.metadata.annotations.opensandbox-lifecycle/token-issued-at}' 2>/dev/null || true)"
if [[ -n "$old_type" && "$old_type" != kubernetes.io/dockerconfigjson ]]; then
  # The type of a Secret cannot change in place.
  kubectl -n "$SANDBOX_NS" delete secret "$SECRET" >/dev/null
  echo "  deleted the old Secret (type ${old_type}; it must be kubernetes.io/dockerconfigjson)"
  old_issued=""
fi
if [[ -n "$old_issued" ]]; then
  age=$(( $(date -u +%s) - $(date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$old_issued" +%s 2>/dev/null || date -u -d "$old_issued" +%s) ))
  printf '  the token in it was %dh %02dm old (it expires after %d h)\n' $((age / 3600)) $((age % 3600 / 60)) "$TOKEN_HOURS"
fi
# The token stays in shell variables and pipes: printf is a builtin, so it is in no process's
# arguments, and server-side apply keeps it out of the last-applied-configuration annotation.
token="$(aws ecr get-login-password --region "$AWS_REGION")"
issued="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
expires="$(date -u -v+"${TOKEN_HOURS}"H +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d "+${TOKEN_HOURS} hours" +%Y-%m-%dT%H:%M:%SZ)"
auth="$(printf 'AWS:%s' "$token" | base64 | tr -d '\n')"
dockercfg="$(printf '{"auths":{"%s":{"auth":"%s"}}}' "$ECR_REGISTRY" "$auth" | base64 | tr -d '\n')"
kubectl apply --server-side --force-conflicts --field-manager=00_snapshot_config -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ${SECRET}
  namespace: ${SANDBOX_NS}
  labels:
    app.kubernetes.io/part-of: opensandbox
  annotations:
    opensandbox-lifecycle/token-issued-at: "${issued}"
    opensandbox-lifecycle/token-expires-at: "${expires}"
type: kubernetes.io/dockerconfigjson
data:
  .dockerconfigjson: ${dockercfg}
EOF
# The registry accepts it: GET /v2/ with the same credentials (header read from stdin, not argv).
code="$(printf 'header = "Authorization: Basic %s"\n' "$auth" \
  | curl -sS -o /dev/null -w '%{http_code}' -K - "https://${ECR_REGISTRY}/v2/" || true)"
unset token auth dockercfg
stype="$(kubectl -n "$SANDBOX_NS" get secret "$SECRET" -o jsonpath='{.type}')"
if [[ "$stype" == kubernetes.io/dockerconfigjson ]]; then ok "type ${stype}, token issued ${issued}, valid until ${expires}"; else fail "type ${stype}"; fi
if [[ "$code" == 200 ]]; then ok "the registry accepts the token (GET /v2/ answered 200)"; else fail "the registry answered ${code} to the token"; fi
phase_done

# --- 3. the controller -----------------------------------------------------------------
phase "controller release ${RELEASE}"
# shellcheck disable=SC2016  # envsubst takes ${...} literally
envsubst '${ECR_REGISTRY} ${ECR_PREFIX}' <"${EKS}/values/controller.yaml" >"$RENDERED/controller.yaml"
# shellcheck disable=SC2016
envsubst '${ECR_REGISTRY} ${ECR_PREFIX}' <values/controller-snapshot.yaml >"$RENDERED/controller-snapshot.yaml"
# shellcheck disable=SC2016
if grep -n '\${' "$RENDERED/controller.yaml" "$RENDERED/controller-snapshot.yaml" >&2; then echo "unfilled \${...} above" >&2; exit 1; fi
v="$(helm show chart "$CHART" | sed -n 's/^version: //p')"
[[ "$v" == 1.2.0 ]] || { echo "chart controller is version ${v}, expected 1.2.0" >&2; exit 1; }
read -r prev_rev prev_status <<<"$(helm -n "$NS" status "$RELEASE" -o json | jq -r '"\(.version) \(.info.status)"')"
if [[ "$prev_status" == pending-* ]]; then
  echo "release ${RELEASE} is stuck in ${prev_status}: helm --kube-context ${KUBE_CONTEXT} -n ${NS} rollback ${RELEASE}" >&2
  exit 1
fi
pod_uid() { kubectl -n "$NS" get pods -l control-plane=controller-manager -o json \
  | jq -r '[.items[] | select(.metadata.deletionTimestamp == null)][0] | "\(.metadata.uid) \(.metadata.name)"'; }
read -r uid_before name_before <<<"$(pod_uid)"
if ! helm upgrade "$RELEASE" "$CHART" -n "$NS" -f "$RENDERED/controller.yaml" -f "$RENDERED/controller-snapshot.yaml" \
    --wait --timeout "$HELM_TIMEOUT" >"$LOGS/helm-controller.log" 2>&1; then
  tail -n 20 "$LOGS/helm-controller.log" | host_free >&2
  kubectl -n "$NS" get pods -l control-plane=controller-manager >&2 || true
  exit 1
fi
read -r rev status <<<"$(helm -n "$NS" status "$RELEASE" -o json | jq -r '"\(.version) \(.info.status)"')"
if diff <(helm -n "$NS" get manifest "$RELEASE" --revision "$prev_rev") <(helm -n "$NS" get manifest "$RELEASE" --revision "$rev") >/dev/null; then
  echo "  revision ${prev_rev} -> ${rev} (${status}), manifests identical"
else
  echo "  revision ${prev_rev} -> ${rev} (${status}), manifests changed"
fi
read -r uid_after name_after <<<"$(pod_uid)"
if [[ "$uid_before" == "$uid_after" ]]; then echo "  pod ${name_after} unchanged"; else echo "  pod ${name_after} replaced ${name_before}"; fi
kubectl -n "$NS" rollout status deploy/opensandbox-controller-manager --timeout=120s | sed 's/^/  /'
manager_args="$(kubectl -n "$NS" get deploy opensandbox-controller-manager -o json | jq -r '.spec.template.spec.containers[0].args[]')"
for flag in snapshot-registry snapshot-image-uri-template snapshot-push-secret resume-pull-secret image-committer-image; do
  line="$(grep -- "^--${flag}=" <<<"$manager_args" || true)"
  if [[ -n "$line" ]]; then ok "$(host_free <<<"$line")"; else fail "--${flag} is not among the controller's arguments"; fi
done
phase_done

printf '\n\033[1m== Summary\033[0m\n'
echo "  repository  ${REPO} (one repository for every snapshot)"
echo "  secret      ${SANDBOX_NS}/${SECRET}: token valid until ${expires}; run this script again before then"
echo "  controller  ${RELEASE} revision ${rev}, $(kubectl -n "$NS" get deploy opensandbox-controller-manager -o jsonpath='{.status.readyReplicas}')/1 ready"
printf "\n%s" "$timings"
printf '  %-44s %5ss\n' total $(( $(date +%s) - script_start ))
if (( failures > 0 )); then echo "${failures} check(s) failed" >&2; exit 1; fi
