#!/usr/bin/env bash
# The OpenSandbox developer console: a web UI (nginx serving a React app) and a BFF (a FastAPI
# service that logs developers in and calls the lifecycle server for them).
#
#   ./06_console.sh          build the images, then deploy
#   ./06_console.sh build    build both images from the release-1.2.0 source and push them to ECR
#   ./06_console.sh deploy   apply the manifests in manifests/console/ and wait for the rollouts
#
# build: .src/OpenSandbox/console (from 04_install.sh) at the release tag. The images are
# built for linux/amd64 with docker buildx, which runs the amd64 build steps under emulation on
# an arm64 Mac, so the first build takes a while; a second one comes from the build cache.
# The Dockerfiles take their base images as build arguments. Docker Hub rate-limits anonymous
# pulls, so the arguments point at public.ecr.aws, which serves the same official images.
# Pushing to ECR creates the repositories (the repository creation template from 01_cluster.sh).
# The pushed digests are then read back from ECR.
#
# deploy: the BFF logs developers in with a tenants.toml and forwards the key they log in
# with to the server. The server stays in single-key mode, so the tenant key is OSB_API_KEY
# from .env. The BFF session secret and the admin login token (BFF_SESSION_SECRET and
# BFF_ADMIN_TOKEN in .env) are generated when missing. Nothing secret is printed.
set -euo pipefail
cd "$(dirname "$0")"

MODE="${1:-all}"
case "$MODE" in
  all|build|deploy) ;;
  *) echo "usage: $0 [build|deploy]" >&2; exit 2 ;;
esac

OSB_VERSION="1.2.0"
OSB_TAG="release-${OSB_VERSION}"
SRC=".src/OpenSandbox"
CONSOLE="${SRC}/console"
NS="opensandbox-system"
MANIFESTS="manifests/console"
ENV_FILE=".env"
PLATFORM="linux/amd64"
# Base images for the Dockerfiles' ARGs (the defaults are on Docker Hub).
PYTHON_IMAGE="public.ecr.aws/docker/library/python:3.12-slim"
NODE_IMAGE="public.ecr.aws/docker/library/node:22-alpine"
NGINX_IMAGE="public.ecr.aws/nginx/nginx-unprivileged:1.27-alpine"
LOGS="logs"

for tool in docker aws openssl jq envsubst awk; do
  command -v "$tool" >/dev/null || { echo "$tool is not installed (see README.md)" >&2; exit 1; }
done
[[ -f env.sh ]] || { echo "env.sh not found: run ./01_cluster.sh first" >&2; exit 1; }
# shellcheck source=/dev/null
source ./env.sh
: "${KUBE_CONTEXT:?env.sh has no KUBE_CONTEXT: run ./01_cluster.sh again}"
: "${ECR_REGISTRY:?env.sh has no ECR_REGISTRY: run ./01_cluster.sh again}"
: "${ECR_PREFIX:?env.sh has no ECR_PREFIX: run ./01_cluster.sh again}"
: "${AWS_REGION:?env.sh has no AWS_REGION: run ./01_cluster.sh again}"
kubectl() { command kubectl --context "$KUBE_CONTEXT" "$@"; }
mkdir -p "$LOGS"
WORK="$(mktemp -d)"
env_tmp=""
trap 'rm -rf "$WORK"; [[ -z "$env_tmp" ]] || rm -f "$env_tmp"' EXIT
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

IMAGES=(console-web console-bff)
repo_of() { printf '%s/%s' "$ECR_PREFIX" "$1"; }
# ecr_digest <image>: the digest ECR has for the release tag, or nothing.
ecr_digest() {
  aws ecr describe-images --region "$AWS_REGION" --repository-name "$(repo_of "$1")" \
    --image-ids "imageTag=${OSB_TAG}" --query 'imageDetails[0].imageDigest' --output text 2>/dev/null || true
}

# --- .env helpers (the file holds secrets: mode 600, values never printed) ----------
env_get() {  # NAME -> the last NAME= value in .env, unquoted
  local v=""
  if [[ -f "$ENV_FILE" ]]; then v="$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1)"; fi
  v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  printf '%s' "$v"
}
env_set() {  # NAME VALUE -> replaces any NAME= lines in .env with one, keeping the rest
  env_tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  if [[ -f "$ENV_FILE" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" == "$1="* ]] || printf '%s\n' "$line"
    done <"$ENV_FILE" >"$env_tmp"
  fi
  printf '%s=%s\n' "$1" "$2" >>"$env_tmp"
  chmod 600 "$env_tmp"
  mv "$env_tmp" "$ENV_FILE"
  env_tmp=""
}

# --- build ------------------------------------------------------------------------
build() {
  command -v docker >/dev/null && docker buildx version >/dev/null 2>&1 \
    || { echo "docker buildx is not available (see README.md)" >&2; exit 1; }
  [[ -f "$CONSOLE/bff/Dockerfile" ]] || { echo "${CONSOLE} not found: run ./04_install.sh first" >&2; exit 1; }
  local head tag_commit
  head="$(git -C "$SRC" rev-parse HEAD)"
  tag_commit="$(git -C "$SRC" rev-parse -q --verify "refs/tags/${OSB_TAG}^{commit}" || true)"
  if [[ "$head" != "$tag_commit" ]]; then echo "${SRC} is not at ${OSB_TAG}" >&2; exit 1; fi
  if [[ -n "$(git -C "$SRC" status --porcelain)" ]]; then
    echo "${SRC} has local changes, which would change the image digests: undo them (or delete ${SRC} and run ./04_install.sh)" >&2
    exit 1
  fi

  phase "ECR login"
  local token real_config
  if ! token="$(aws ecr get-login-password --region "$AWS_REGION")"; then
    echo "could not get an ECR login token: check your AWS credentials (aws sts get-caller-identity)" >&2
    exit 1
  fi
  # A throwaway Docker config, so the login stays out of ~/.docker (same as 03_images.sh).
  real_config="${DOCKER_CONFIG:-$HOME/.docker}"
  if [[ -d "$real_config/cli-plugins" ]]; then ln -s "$real_config/cli-plugins" "$WORK/cli-plugins"; fi
  printf '{"auths":{"%s":{"auth":"%s"}}}\n' "$ECR_REGISTRY" "$(printf 'AWS:%s' "$token" | base64 | tr -d '\n')" >"$WORK/config.json"
  chmod 600 "$WORK/config.json"
  unset token
  export DOCKER_CONFIG="$WORK"
  ok "logged in to ${ECR_REGISTRY}"
  phase_done

  local name dir args ref started log meta digest remote
  for name in "${IMAGES[@]}"; do
    phase "build and push ${name}"
    case "$name" in
      console-bff) dir="bff"; args=(--build-arg "PYTHON_IMAGE=${PYTHON_IMAGE}") ;;
      console-web) dir="web"; args=(--build-arg "NODE_IMAGE=${NODE_IMAGE}" --build-arg "NGINX_IMAGE=${NGINX_IMAGE}") ;;
    esac
    ref="${ECR_REGISTRY}/$(repo_of "$name"):${OSB_TAG}"
    log="${LOGS}/build-${name}.log"
    meta="${WORK}/${name}.json"
    echo "  ${CONSOLE}/${dir}/Dockerfile for ${PLATFORM}, base images from public.ecr.aws"
    echo "  full output: ${log}"
    started=$(date +%s)
    # --progress=plain writes one block per build step. awk shows the step headers and how long
    # each step took, and the full output goes to the log. awk always exits 0, so pipefail
    # reports docker's own exit status.
    # The layer timestamps are set to the release commit's date and the provenance attestation
    # (which records the build time) is off, so the same source and cache give the same
    # digest. A re-run then leaves the Deployments' pod annotations, and so the pods, alone.
    SOURCE_DATE_EPOCH="$(git -C "$SRC" log -1 --format=%ct)" docker buildx build --platform "$PLATFORM" \
      --progress=plain --provenance=false --sbom=false "${args[@]}" \
      --metadata-file "$meta" -f "${CONSOLE}/${dir}/Dockerfile" \
      --output "type=image,name=${ref},push=true,unpack=false,rewrite-timestamp=true" "${CONSOLE}/${dir}" 2>&1 \
      | tee "$log" \
      | awk '/^#[0-9]+ \[/ { sub(/^#[0-9]+ /, ""); print "  step " substr($0, 1, 100); fflush() }
             /^#[0-9]+ pushing (manifest|layers)/ { print "  " substr($0, index($0, " ") + 1); fflush() }' \
      || { echo "  the build of ${name} failed; the end of ${log}:" >&2; tail -n 25 "$log" >&2; exit 1; }
    digest="$(jq -r '."containerimage.digest" // empty' "$meta")"
    if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then fail "${name}: no digest in the build metadata"; phase_done; continue; fi
    remote="$(ecr_digest "$name")"
    if [[ "$remote" == "$digest" ]]; then
      ok "${name}:${OSB_TAG} in ECR, digest ${digest}"
    else
      fail "${name}: built ${digest}, ECR has ${remote:-nothing} for tag ${OSB_TAG}"
    fi
    echo "  built and pushed in $(duration $(( $(date +%s) - started )))"
    phase_done
  done
}

# --- deploy -----------------------------------------------------------------------
deploy() {
  command -v kubectl >/dev/null || { echo "kubectl is not installed (see README.md)" >&2; exit 1; }
  kubectl get --raw /readyz >/dev/null 2>&1 \
    || { echo "cannot reach context '${KUBE_CONTEXT}': run ./01_cluster.sh first" >&2; exit 1; }

  phase "secrets"
  local osb_key session_secret admin_token
  osb_key="$(env_get OSB_API_KEY)"
  [[ -n "$osb_key" ]] || { echo "no OSB_API_KEY in ${ENV_FILE}: run ./04_install.sh first" >&2; exit 1; }
  session_secret="$(env_get BFF_SESSION_SECRET)"
  if [[ -z "$session_secret" ]]; then
    session_secret="$(openssl rand -hex 32)"; env_set BFF_SESSION_SECRET "$session_secret"
    echo "  BFF_SESSION_SECRET: none in ${ENV_FILE}, generated one and saved it there (mode 600)"
  else
    echo "  BFF_SESSION_SECRET from ${ENV_FILE}"
  fi
  admin_token="$(env_get BFF_ADMIN_TOKEN)"
  if [[ -z "$admin_token" ]]; then
    admin_token="$(openssl rand -hex 16)"; env_set BFF_ADMIN_TOKEN "$admin_token"
    echo "  BFF_ADMIN_TOKEN: none in ${ENV_FILE}, generated one and saved it there (mode 600)"
  else
    echo "  BFF_ADMIN_TOKEN from ${ENV_FILE}"
  fi
  if [[ ! "$osb_key" =~ ^[A-Za-z0-9._~+/=-]+$ ]]; then echo "OSB_API_KEY has characters that need escaping in tenants.toml" >&2; exit 1; fi
  export OSB_API_KEY="$osb_key" BFF_SESSION_SECRET="$session_secret" BFF_ADMIN_TOKEN="$admin_token"
  # Secrets go through stdin and server-side apply: no process argument or last-applied annotation holds them.
  local f
  for f in tenants-secret console-bff-secret; do
    envsubst '${OSB_API_KEY} ${BFF_SESSION_SECRET} ${BFF_ADMIN_TOKEN}' <"${MANIFESTS}/${f}.yaml" \
      | kubectl apply --server-side --force-conflicts --field-manager=06_console -f - | sed 's/^/  /'
  done
  unset osb_key session_secret admin_token OSB_API_KEY BFF_SESSION_SECRET BFF_ADMIN_TOKEN
  # The BFF reads both Secrets once at startup. Their resourceVersions (they change only when a
  # value does) go into its pod annotations, so it restarts exactly when a secret changes.
  SECRETS_VERSION="$(kubectl -n "$NS" get secret opensandbox-tenants opensandbox-console-bff \
    -o jsonpath='{range .items[*]}{.metadata.resourceVersion}{"."}{end}')"
  export SECRETS_VERSION
  phase_done

  phase "RBAC, BFF, web"
  BFF_DIGEST="$(ecr_digest console-bff)"; WEB_DIGEST="$(ecr_digest console-web)"
  if [[ -z "$BFF_DIGEST" || "$BFF_DIGEST" == None || -z "$WEB_DIGEST" || "$WEB_DIGEST" == None ]]; then
    echo "the console images are not in ECR: run ./06_console.sh build first" >&2; exit 1
  fi
  export BFF_DIGEST WEB_DIGEST ECR_REGISTRY ECR_PREFIX
  echo "  console-bff ${BFF_DIGEST}"
  echo "  console-web ${WEB_DIGEST}"
  kubectl apply -f "${MANIFESTS}/console-bff-rbac.yaml" | sed 's/^/  /'
  for f in console-bff-deployment console-web-deployment; do
    envsubst '${ECR_REGISTRY} ${ECR_PREFIX} ${BFF_DIGEST} ${WEB_DIGEST} ${SECRETS_VERSION}' <"${MANIFESTS}/${f}.yaml" >"${WORK}/${f}.yaml"
    if grep -n '\${' "${WORK}/${f}.yaml" >&2; then echo "${f}: unfilled \${...} above" >&2; exit 1; fi
    kubectl apply -f "${WORK}/${f}.yaml" | sed 's/^/  /'
  done
  phase_done

  phase "rollouts"
  local d
  for d in opensandbox-console-bff opensandbox-console-web; do
    if kubectl -n "$NS" rollout status "deployment/${d}" --timeout=300s | sed 's/^/  /'; then
      ok "${d} rolled out"
    else
      fail "${d} did not roll out"
      kubectl -n "$NS" get pods -l "app=${d}" -o wide >&2 || true
      kubectl -n "$NS" logs "deploy/${d}" --tail=20 >&2 || true
    fi
  done
  phase_done

  phase "checks"
  # From inside the pods (kubectl exec goes through the kubelet; the API server's service proxy
  # can't reach port 80 on the nodes here, see 04_install.sh).
  local health through
  health="$(kubectl -n "$NS" exec deploy/opensandbox-console-bff -- python -c \
    'import urllib.request as u; print(u.urlopen("http://127.0.0.1:8091/health", timeout=5).read().decode())' 2>"$WORK/health.err" || true)"
  if [[ -n "$health" ]]; then
    ok "BFF /health: ${health}"
  else
    fail "BFF /health: no answer"
    echo "        $(tail -n 1 "$WORK/health.err" | cut -c1-200)" >&2
  fi
  through="$(kubectl -n "$NS" exec deploy/opensandbox-console-web -- wget -qO- -T 5 http://127.0.0.1:8080/health 2>/dev/null || true)"
  if [[ -n "$through" ]]; then ok "web /health (nginx to the BFF Service): ${through}"; else fail "web /health through nginx: no answer"; fi
  # Log in as the tenant and list sandboxes, both inside the BFF pod. The script reads the key
  # from the mounted tenants.toml, so the key is in no command line and not in the output.
  local probe result
  probe='import tomllib, httpx
key = tomllib.load(open("/etc/opensandbox/tenants.toml", "rb"))["tenants"][0]["api_keys"][0]
with httpx.Client(base_url="http://127.0.0.1:8091/api", timeout=20) as c:
    r = c.post("/auth/tenant", json={"api_key": key}); print("login", r.status_code, r.json().get("tenant"))
    r = c.get("/sandboxes"); print("sandboxes", r.status_code, len(r.json().get("items", [])))'
  result="$(kubectl -n "$NS" exec deploy/opensandbox-console-bff -- python -c "$probe" 2>&1 || true)"
  if [[ "$(sed -n 1p <<<"$result")" == "login 200 "* && "$(sed -n 2p <<<"$result")" == "sandboxes 200 "* ]]; then
    ok "tenant login through the BFF: $(sed -n 1p <<<"$result"); $(sed -n 2p <<<"$result") (through to the lifecycle server)"
  else
    fail "tenant login or sandbox list through the BFF: $(tr '\n' ' ' <<<"$result" | cut -c1-200)"
  fi
  phase_done
}

[[ "$MODE" == deploy ]] || build
if (( failures == 0 )) && [[ "$MODE" != build ]]; then deploy; fi

printf '\n\033[1m== Timings\033[0m\n%s' "$timings"
printf '  %-30s %9s\n' "total" "$(duration $(( $(date +%s) - script_start )))"
if (( failures > 0 )); then
  echo "${failures} check(s) failed" >&2
  exit 1
fi
if [[ "$MODE" != build ]]; then
  echo "The console is running. Open it with:"
  echo "  kubectl --context ${KUBE_CONTEXT} -n ${NS} port-forward svc/opensandbox-console-web 5173:80"
  echo "then http://127.0.0.1:5173/login: sign in with OSB_API_KEY, or BFF_ADMIN_TOKEN as admin (both in ${ENV_FILE})"
else
  echo "Both console images are in ECR. Run ./06_console.sh deploy next."
fi
