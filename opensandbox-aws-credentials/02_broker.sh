#!/usr/bin/env bash
# Part 5, step 2: build the credential broker, push it to ECR and run it in the cluster.
#
#  1. checks what the broker needs: the server and its API key Secret, the Pod Identity
#     association from 01_iam.sh, and no association in the sandboxes' namespace
#  2. builds broker/ for linux/amd64 and pushes it as opensandbox-eks/broker:release-1.2.0
#  3. applies manifests/broker.yaml in namespace opensandbox-broker, with the image pinned by
#     digest. The broker reads broker.env (as the ConfigMap osb-broker-env) and the server's
#     API key. That key is a Secret in opensandbox-system, and a pod can only read Secrets in
#     its own namespace, so this script copies it to the Secret osb-broker-server-key. The
#     copy goes through stdin: the key is never printed and never in a process argument.
#  4. checks the running broker: its AWS identity (an EKS Pod Identity role session) and,
#     with a session exactly like a sandbox's, that AWS refuses the credential-vending calls
#     and why (python broker.py deny-check, in the pod; it waits about a minute for AWS's
#     authorization details on the AssumeRole call)
#
# Safe to run again: the image builds from cache, and the Deployment only rolls when the
# image, broker.env or the API key changed.
#
# Then, in three steps (the port-forward stays running in its own terminal):
#   kubectl --context osb-eks -n opensandbox-broker port-forward svc/osb-broker 18090:80
#   curl -s -X POST 127.0.0.1:18090/sandboxes -d '{"image": "<registry>/opensandbox-eks/python:3.12-slim"}'
#   curl -s 127.0.0.1:18090/sandboxes/<sandbox id>
#
# Needs ../opensandbox-eks/01_cluster.sh to 04_install.sh (env.sh, the cluster, the server,
# the python:3.12-slim image in ECR) and ./01_iam.sh (broker.env).
set -euo pipefail
cd "$(dirname "$0")"

for tool in aws docker kubectl jq envsubst; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
docker buildx version >/dev/null 2>&1 || { echo "docker buildx is not available (see README.md)" >&2; exit 1; }
[[ -f ../opensandbox-eks/env.sh ]] || { echo "run ../opensandbox-eks/01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ../opensandbox-eks/env.sh
: "${ECR_REGISTRY:?../opensandbox-eks/env.sh does not set ECR_REGISTRY: run ../opensandbox-eks/01_cluster.sh again}"
: "${ECR_PREFIX:?../opensandbox-eks/env.sh does not set ECR_PREFIX: run ../opensandbox-eks/01_cluster.sh again}"
: "${AWS_REGION:?../opensandbox-eks/env.sh does not set AWS_REGION: run ../opensandbox-eks/01_cluster.sh again}"
: "${CLUSTER_NAME:?../opensandbox-eks/env.sh does not set CLUSTER_NAME: run ../opensandbox-eks/01_cluster.sh again}"
: "${KUBE_CONTEXT:?../opensandbox-eks/env.sh does not set KUBE_CONTEXT: run ../opensandbox-eks/01_cluster.sh again}"
[[ -f broker.env ]] || { echo "broker.env not found: run ./01_iam.sh first" >&2; exit 1; }

# Every kubectl call names the cluster's context, so another terminal switching contexts
# can't point this script at a different cluster.
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }

# broker.env is read, not sourced: KEY=value lines only.
env_value() { sed -n "s/^$1=//p" broker.env; }
BROKER_NS="$(env_value BROKER_NS)"
BROKER_SA="$(env_value BROKER_SA)"
SANDBOX_NAMESPACE="$(env_value SANDBOX_NAMESPACE)"   # where the server creates sandbox pods
[[ -n "$SANDBOX_NAMESPACE" ]] || { echo "broker.env has no SANDBOX_NAMESPACE: run ./01_iam.sh again" >&2; exit 1; }
# manifests/broker.yaml names them literally; Terraform must agree.
if [[ "$BROKER_NS" != opensandbox-broker || "$BROKER_SA" != osb-broker ]]; then
  echo "broker.env names ${BROKER_NS}/${BROKER_SA}, manifests/broker.yaml opensandbox-broker/osb-broker: run ./01_iam.sh again" >&2
  exit 1
fi

IMAGE="${ECR_REGISTRY}/${ECR_PREFIX}/broker:release-1.2.0"
BASE_IMAGE="${ECR_REGISTRY}/${ECR_PREFIX}/python:3.12-slim"
SERVER_NS="opensandbox-system"
SERVER_SECRET="opensandbox-server-auth"   # created by ../opensandbox-eks/04_install.sh
KEY_SECRET="osb-broker-server-key"        # its copy in the broker's namespace
LOGS="logs"   # full build output (gitignored)
# The account ID is shown as <account> in everything this script prints (it is the first
# label of the ECR registry host).
ACCOUNT_ID="${ECR_REGISTRY%%.*}"
mask() { if [[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]]; then sed "s/${ACCOUNT_ID}/<account>/g"; else cat; fi; }
mkdir -p "$LOGS"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- output helpers (same as ../opensandbox-eks/01_cluster.sh) -------------------------
phase_start=0
duration() { local s=$1; if (( s >= 60 )); then printf '%dm %02ds' $((s / 60)) $((s % 60)); else printf '%ds' "$s"; fi; }
phase() { phase_start=$(date +%s); printf '\n\033[1m== %s\033[0m\n' "$1"; }
phase_done() { printf '   took %s\n' "$(duration $(( $(date +%s) - phase_start )))"; }
sha() { if command -v sha256sum >/dev/null; then sha256sum | cut -c1-16; else shasum -a 256 | cut -c1-16; fi; }
failures=0
ok()   { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }

# --- 1. What the broker needs --------------------------------------------------------------
phase "preconditions"
if kubectl -n "$SERVER_NS" get deploy opensandbox-server >/dev/null 2>&1; then
  ok "server deployment ${SERVER_NS}/opensandbox-server"
else
  fail "no deployment ${SERVER_NS}/opensandbox-server: run ../opensandbox-eks/04_install.sh"
fi
if kubectl -n "$SERVER_NS" get secret "$SERVER_SECRET" >/dev/null 2>&1; then
  ok "server API key Secret ${SERVER_NS}/${SERVER_SECRET}"
else
  fail "no Secret ${SERVER_NS}/${SERVER_SECRET}: run ../opensandbox-eks/04_install.sh"
fi
assoc="$(aws eks list-pod-identity-associations --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" \
  --namespace "$BROKER_NS" --service-account "$BROKER_SA" --query 'associations[].associationId' --output text)"
if [[ -n "$assoc" && "$assoc" != "None" ]]; then
  ok "Pod Identity association for ${BROKER_NS}/${BROKER_SA}"
else
  fail "no Pod Identity association for ${BROKER_NS}/${BROKER_SA}: run ./01_iam.sh"
fi
# Pod Identity puts its credentials into every container of an associated pod, so the
# namespace the sandboxes run in must have no association at all.
sandbox_assoc="$(aws eks list-pod-identity-associations --region "$AWS_REGION" --cluster-name "$CLUSTER_NAME" \
  --namespace "$SANDBOX_NAMESPACE" --query 'associations[].serviceAccount' --output text)"
if [[ -z "$sandbox_assoc" || "$sandbox_assoc" == "None" ]]; then
  ok "no Pod Identity association in namespace ${SANDBOX_NAMESPACE} (the sandboxes')"
else
  fail "namespace ${SANDBOX_NAMESPACE} has Pod Identity associations (${sandbox_assoc}): sandbox pods would get AWS credentials"
fi
if (( failures > 0 )); then
  echo "${failures} precondition(s) failed" >&2
  exit 1
fi
phase_done

# --- 2. Build and push ----------------------------------------------------------------------
phase "build and push ${ECR_PREFIX}/broker:release-1.2.0"
# The ECR login lives in a throwaway Docker config, deleted on exit (as in
# ../opensandbox-eks/03_images.sh): nothing lands in your keychain. That config has no Docker
# contexts, so the build is pointed at the daemon of your current context.
if [[ -z "${DOCKER_HOST:-}" ]]; then
  DOCKER_HOST="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
  if [[ -n "$DOCKER_HOST" ]]; then export DOCKER_HOST; fi
fi
if ! token="$(aws ecr get-login-password --region "$AWS_REGION")"; then
  echo "could not get an ECR login token: check your AWS credentials (aws sts get-caller-identity)" >&2
  exit 1
fi
real_config="${DOCKER_CONFIG:-$HOME/.docker}"
if [[ -d "$real_config/cli-plugins" ]]; then ln -s "$real_config/cli-plugins" "$WORK/cli-plugins"; fi
printf '{"auths":{"%s":{"auth":"%s"}}}\n' "$ECR_REGISTRY" "$(printf 'AWS:%s' "$token" | base64 | tr -d '\n')" >"$WORK/config.json"
chmod 600 "$WORK/config.json"
unset token
export DOCKER_CONFIG="$WORK"
echo "base image ${ECR_PREFIX}/python:3.12-slim (full output: ${LOGS}/02-broker-build.log)"
if ! docker buildx build --platform linux/amd64 \
    --build-arg "BASE_IMAGE=${BASE_IMAGE}" \
    --tag "$IMAGE" \
    --metadata-file "$WORK/build.json" \
    --push broker/ >"$LOGS/02-broker-build.log" 2>&1; then
  tail -n 30 "$LOGS/02-broker-build.log" | mask >&2
  exit 1
fi
digest="$(jq -r '."containerimage.digest" // empty' "$WORK/build.json")"
if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "the build did not report an image digest (see ${LOGS}/02-broker-build.log)" >&2
  exit 1
fi
ok "pushed ${ECR_PREFIX}/broker:release-1.2.0@${digest}"
phase_done

# --- 3. Kubernetes objects ------------------------------------------------------------------
phase "namespace ${BROKER_NS}"
# The first document of manifests/broker.yaml is the Namespace: it has to exist before the
# Secret and the ConfigMap, and those before the Deployment.
sed '/^---$/,$d' manifests/broker.yaml | kubectl apply -f - | sed 's/^/  /'

key_b64="$(kubectl -n "$SERVER_NS" get secret "$SERVER_SECRET" -o jsonpath='{.data.api-key}')"
if [[ -z "$key_b64" ]]; then
  echo "Secret ${SERVER_NS}/${SERVER_SECRET} has no api-key" >&2
  exit 1
fi
current_b64="$(kubectl -n "$BROKER_NS" get secret "$KEY_SECRET" -o jsonpath='{.data.api-key}' 2>/dev/null || true)"
if [[ "$current_b64" == "$key_b64" ]]; then
  echo "  secret/${KEY_SECRET} unchanged"
else
  kubectl apply --server-side --force-conflicts --field-manager=02_broker -f - >/dev/null <<EOF
apiVersion: v1
kind: Secret
metadata:
  name: ${KEY_SECRET}
  namespace: ${BROKER_NS}
  labels:
    app.kubernetes.io/part-of: opensandbox
type: Opaque
data:
  api-key: ${key_b64}
EOF
  if [[ -z "$current_b64" ]]; then echo "  secret/${KEY_SECRET} created (a copy of ${SERVER_NS}/${SERVER_SECRET})"
  else echo "  secret/${KEY_SECRET} updated: the server's key changed"; fi
fi
unset key_b64 current_b64

kubectl -n "$BROKER_NS" create configmap osb-broker-env --from-env-file=broker.env --dry-run=client -o yaml \
  | kubectl apply --server-side --force-conflicts --field-manager=02_broker -f - | sed 's/^/  /'
phase_done

phase "broker"
BROKER_IMAGE="${IMAGE}@${digest}"
CONFIG_SHA="$(sha <broker.env)"
KEY_VERSION="$(kubectl -n "$BROKER_NS" get secret "$KEY_SECRET" -o jsonpath='{.metadata.resourceVersion}')"
export BROKER_IMAGE CONFIG_SHA KEY_VERSION
# shellcheck disable=SC2016  # envsubst takes ${...} literally
envsubst '${BROKER_IMAGE} ${CONFIG_SHA} ${KEY_VERSION}' <manifests/broker.yaml >"$WORK/broker.yaml"
# shellcheck disable=SC2016
if grep -n '\${' "$WORK/broker.yaml" >&2; then echo "manifests/broker.yaml: unfilled \${...} above" >&2; exit 1; fi
kubectl apply -f "$WORK/broker.yaml" | sed 's/^/  /'
if ! kubectl -n "$BROKER_NS" rollout status deploy/osb-broker --timeout=300s | sed 's/^/  /'; then
  kubectl -n "$BROKER_NS" get pods -l app.kubernetes.io/name=osb-broker -o wide | mask >&2 || true
  kubectl -n "$BROKER_NS" logs deploy/osb-broker --tail=30 | mask >&2 || true
  exit 1
fi
phase_done

# --- 4. The running broker ---------------------------------------------------------------------
phase "checks"
env_names="$(kubectl -n "$BROKER_NS" get pod -l app.kubernetes.io/name=osb-broker \
  -o jsonpath='{.items[0].spec.containers[0].env[*].name}')"
if [[ " $env_names " == *" AWS_CONTAINER_CREDENTIALS_FULL_URI "* ]]; then
  ok "EKS Pod Identity injected its credentials endpoint into the broker pod"
else
  fail "the broker pod has no AWS_CONTAINER_CREDENTIALS_FULL_URI: was the association there before the pod started?"
fi
echo "  \$ python broker.py whoami"
if ! kubectl -n "$BROKER_NS" exec deploy/osb-broker -c broker -- python /app/broker.py whoami 2>&1 | mask | sed 's/^/    /'; then
  fail "whoami failed in the broker pod"
fi
echo "  \$ python broker.py deny-check"
set +e
kubectl -n "$BROKER_NS" exec deploy/osb-broker -c broker -- python /app/broker.py deny-check 2>&1 | mask | sed 's/^/    /'
deny_rc=${PIPESTATUS[0]}
set -e
if (( deny_rc == 0 )); then
  ok "deny-check: AWS refused every credential-vending call (its last line says how many it showed the Deny refused)"
else
  fail "deny-check: a credential-vending call was allowed, or AWS showed it was not the Deny that refused it"
fi
phase_done

if (( failures > 0 )); then
  echo "${failures} check(s) failed" >&2
  exit 1
fi
echo
echo "The broker is running. Try it (the port-forward in its own terminal, left running):"
echo "  kubectl --context ${KUBE_CONTEXT} -n ${BROKER_NS} port-forward svc/osb-broker 18090:80"
cat <<'EOF'
  source ../opensandbox-eks/env.sh   # sets ECR_REGISTRY and ECR_PREFIX
  curl -s -X POST 127.0.0.1:18090/sandboxes -d "{\"image\": \"$ECR_REGISTRY/$ECR_PREFIX/python:3.12-slim\"}"
  curl -s 127.0.0.1:18090/sandboxes/<sandbox id>
  curl -s -X POST 127.0.0.1:18090/sandboxes/<sandbox id>/refresh
  curl -s -X DELETE 127.0.0.1:18090/sandboxes/<sandbox id>
EOF
