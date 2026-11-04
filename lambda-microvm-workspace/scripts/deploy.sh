#!/usr/bin/env bash
# Deploy the workspace end to end: the sandbox image, infra (CloudFront, ALB, API service, Cognito, DynamoDB, ECR),
# backend image, SPA. Idempotent; safe from an empty state or as a redeploy. Part 2 must be applied first, with
# control_plane=true for the Proxy policy page (the config bucket, the sidecar and the four sandbox roles live there).
#
#   scripts/deploy.sh                    # everything
#   SKIP_BACKEND=1 scripts/deploy.sh     # SPA only (keeps the running image tag)
#   SKIP_FRONTEND=1 scripts/deploy.sh    # backend + infra only
#   SKIP_IMAGE=1 scripts/deploy.sh       # don't check or build the sandbox image
#
# Order on a fresh deploy:
#   1. checks: tools, part 2's state is the two-VPC stack (and whether its control plane is on)
#   2. the sandbox image, in the background (it needs only part 2: its CA, build role and artifacts bucket);
#      built when missing or when it was built for another CA
#   3. the ECR repository (a targeted apply), so the backend image can be pushed before the service exists
#   4. backend image, pushed with a timestamp tag
#   5. terraform apply with that tag and desired_count=1 (API, ALB, CloudFront, Cognito...; ~8 min the first time)
#   6. frontend build, config.json, S3 upload, CloudFront invalidation
#   7. waits for the API to be stable, for it to seed the proxy's AWS lists (once it has loaded the AWS catalog,
#      seconds after it starts; control plane only), and for the image build
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INFRA="$ROOT/infra"
export AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_DEFAULT_REGION="$AWS_REGION" AWS_PAGER=""
TS="$(date -u +%Y%m%d-%H%M%S)"
PART2_INFRA="${PART2_INFRA:-$HOME/Code/lambda-microvm-iron-proxy/lambda-microvm-iron-proxy/infra}"

tf() { terraform -chdir="$INFRA" "$@"; }
out() { tf output -raw "$1"; }
step() { printf '\n==> %s\n' "$*"; }
redact() { sed -E 's/[0-9]{12}/<account>/g'; }

for bin in terraform aws docker npm jq uv; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done

# ---- 1. part 2 must be the two-VPC stack (proxy VPC, CA, built-in secrets, ECS cluster)
step "checking part 2"
if ! terraform -chdir="$PART2_INFRA" output -raw proxy_vpc_id >/dev/null 2>&1; then
  echo "part 2 at $PART2_INFRA has no proxy_vpc_id output: deploy the two-VPC part 2 first" >&2
  exit 1
fi
echo "part 2 is up (proxy VPC $(terraform -chdir="$PART2_INFRA" output -raw proxy_vpc_id))"
CONTROL_PLANE="$(terraform -chdir="$PART2_INFRA" output -raw control_plane 2>/dev/null || echo false)"
if [[ "$CONTROL_PLANE" != "true" ]]; then
  echo "note: part 2's control_plane is off: the workspace works, but the Proxy policy page stays read-only and" \
       "revoke restarts the proxy. Apply part 2 with control_plane=true, then redeploy this."
fi

# ---- 2. the sandbox image, in the background
PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  step "creating .venv for the scripts"
  uv venv -q "$ROOT/.venv"
  uv pip install -q --python "$PY" boto3==1.43.123 websockets==15.0.1 httpx python-dotenv
fi
IMAGE_LOG="$(mktemp -t sandbox-image.XXXXXX)"
IMAGE_PID=""
if [[ -z "${SKIP_IMAGE:-}" ]]; then
  step "checking the sandbox image in the background (log: $IMAGE_LOG)"
  (cd "$ROOT" && PART2_INFRA="$PART2_INFRA" "$PY" scripts/build_sandbox_image.py --if-needed) >"$IMAGE_LOG" 2>&1 &
  IMAGE_PID=$!
fi

step "terraform init"
tf init -input=false >/dev/null

# ---- 3. the image push needs the ECR repo; on a fresh state create just that first. (Ask the state: on a state
# with no outputs, `terraform output -raw` prints a warning and still exits 0.)
if ! tf state list 2>/dev/null | grep -qx 'aws_ecr_repository.backend'; then
  step "creating the ECR repository (first run)"
  tf apply -input=false -auto-approve -target=aws_ecr_repository.backend | redact | tail -3
fi
REPO="$(out ecr_repo_url)"
REGISTRY="${REPO%%/*}"

# ---- 4. the backend image
if [[ -z "${SKIP_BACKEND:-}" ]]; then
  step "building and pushing the backend image ($TS, linux/arm64)"
  # Authenticate to ECR with a per-registry credential helper instead of `docker login`, which
  # needs to write to the desktop keychain (fails in non-interactive shells on macOS). A temporary
  # DOCKER_CONFIG keeps ~/.docker/config.json untouched but reuses your contexts and plugins.
  DOCKER_CONFIG="$(mktemp -d)"; export DOCKER_CONFIG
  trap 'rm -rf "$DOCKER_CONFIG"' EXIT
  for d in contexts buildx cli-plugins; do
    [[ -e "$HOME/.docker/$d" ]] && ln -s "$HOME/.docker/$d" "$DOCKER_CONFIG/$d"
  done
  mkdir -p "$DOCKER_CONFIG/bin"
  cat > "$DOCKER_CONFIG/bin/docker-credential-awsecr" <<'HELPER'
#!/bin/sh
# Minimal Docker credential helper: answers `get` with a fresh ECR token, ignores store/erase.
[ "$1" = "get" ] || exit 0
read -r server
printf '{"ServerURL":"%s","Username":"AWS","Secret":"%s"}' "$server" "$(aws ecr get-login-password)"
HELPER
  chmod +x "$DOCKER_CONFIG/bin/docker-credential-awsecr"
  export PATH="$DOCKER_CONFIG/bin:$PATH"
  CTX="$(jq -r '.currentContext // empty' "$HOME/.docker/config.json" 2>/dev/null || true)"
  jq -n --arg reg "$REGISTRY" --arg ctx "$CTX" \
    '{credHelpers: {($reg): "awsecr"}} + (if $ctx != "" then {currentContext: $ctx} else {} end)' \
    > "$DOCKER_CONFIG/config.json"
  docker buildx build --platform linux/arm64 --provenance=false \
    -t "$REPO:latest" -t "$REPO:$TS" --push "$ROOT/backend" 2>&1 | redact | tail -5
  IMAGE_TAG="$TS"
else
  # Keep whatever tag the service runs now (falls back to latest)
  IMAGE_TAG="$(aws ecs describe-task-definition --task-definition my2cents-ws-api \
    --query 'taskDefinition.containerDefinitions[0].image' --output text 2>/dev/null | sed 's/.*://' || true)"
  IMAGE_TAG="${IMAGE_TAG:-latest}"
fi

# ---- 5. everything else. A new tag registers a new task definition revision, which rolls the API service.
step "terraform apply (image_tag=$IMAGE_TAG, desired_count=1)"
tf apply -input=false -auto-approve -var desired_count=1 -var "image_tag=$IMAGE_TAG" | redact | tail -40

BUCKET="$(out spa_bucket)"
DIST="$(out cloudfront_distribution_id)"
DOMAIN="$(out cloudfront_domain)"
CLUSTER="$(out ecs_cluster)"
SERVICE="$(out ecs_service)"

# ---- 6. the SPA
if [[ -z "${SKIP_FRONTEND:-}" ]]; then
  step "building the frontend"
  (cd "$ROOT/frontend" && npm ci --no-audit --no-fund && npm run build)
  jq -n --arg region "$(out region)" --arg pool "$(out user_pool_id)" \
    --arg client "$(out client_id)" --arg domain "$(out cognito_domain)" \
    '{region: $region, userPoolId: $pool, clientId: $client, cognitoDomain: $domain, apiBase: ""}' \
    > "$ROOT/frontend/dist/config.json"

  step "uploading to s3://$BUCKET"
  # Hashed assets cache for a year; index.html and config.json must always revalidate.
  aws s3 sync "$ROOT/frontend/dist/" "s3://$BUCKET/" --delete --only-show-errors \
    --exclude index.html --exclude config.json \
    --cache-control "public, max-age=31536000, immutable"
  for f in index.html config.json; do
    aws s3 cp "$ROOT/frontend/dist/$f" "s3://$BUCKET/$f" --only-show-errors --cache-control "no-cache"
  done

  step "invalidating CloudFront"
  aws cloudfront create-invalidation --distribution-id "$DIST" --paths '/*' \
    --query 'Invalidation.Id' --output text
fi

# ---- 7. wait for the API, the seed and the image
step "waiting for the API to be stable"
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE"
aws ecs describe-services --cluster "$CLUSTER" --services "$SERVICE" \
  --query 'services[].{service:serviceName,running:runningCount,desired:desiredCount}' --output table | redact

# Part 2 seeds the proxy's AWS scope with the default role and sts/s3/ecs lists; the API rewrites the lists for that
# role once it has read the AWS catalog. Until then sandboxes reach only those three services (narrower, not wider).
if [[ "$CONTROL_PLANE" == "true" ]]; then
  step "waiting for the API to seed the proxy's AWS lists"
  CONTROL_BUCKET="$(terraform -chdir="$PART2_INFRA" output -raw control_bucket)"
  SCOPE_KEY="$(terraform -chdir="$PART2_INFRA" output -json control_keys | jq -r .scope)"
  seeded() { aws s3 cp "s3://$CONTROL_BUCKET/$SCOPE_KEY" - 2>/dev/null | jq -e 'has("seed") | not' >/dev/null; }
  for _ in $(seq 1 36); do
    seeded && { echo "seeded"; break; }
    sleep 5
  done
  seeded || echo "the scope isn't seeded yet (the API's AWS catalog load failed?); see its log group, then rerun" >&2
fi

if [[ -n "$IMAGE_PID" ]]; then
  step "waiting for the sandbox image check/build"
  if ! wait "$IMAGE_PID"; then
    redact < "$IMAGE_LOG"
    echo "the sandbox image build failed (log above); the rest is deployed" >&2
    exit 1
  fi
  redact < "$IMAGE_LOG" | tail -6
fi

step "done"
echo "https://$DOMAIN/"
echo "users: scripts/create_user.sh [--admin] <email>"
