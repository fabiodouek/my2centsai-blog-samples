# Shared settings for every script. Source it, don't run it.

# Pins from AX's install guide (docs/installation.md, "Development baseline").
# Build the AX CLI, server and task runner from the same AX ref, and Substrate
# from the ref AX's go.mod records. `go install ...@latest` gives a CLI that
# can't talk to a server built from main.
AX_REF=a496fc1ff1cf9bea4c34924348e4119172cfea5b
SUBSTRATE_REF=d3f2363590a11b2f50489971985ba033b4c9eff7

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/.src"
AX_SRC="$SRC/ax"
SUBSTRATE_SRC="$SRC/substrate"
RECORDS="$ROOT/.records"
mkdir -p "$RECORDS"

TF_DIR="$ROOT/terraform"
tf_out() { terraform -chdir="$TF_DIR" output -raw "$1"; }

CLUSTER_NAME="$(tf_out cluster_name)"
AWS_REGION="$(tf_out region)"
IMAGE_REPO="$(tf_out image_repo)"
SNAPSHOT_LOCATION="$(tf_out snapshot_location)"
KUBE_CONTEXT="$CLUSTER_NAME"

export GOWORK=off
# Both modules ask for Go 1.27.x; let the go command fetch the toolchain.
export GOTOOLCHAIN=auto
export AWS_REGION

# A Docker config of our own: ECR credentials come from
# scripts/docker-credential-ax-eks (a fresh `aws ecr get-login-password` per
# pull or push), so there's no `docker login` and nothing lands in the macOS
# keychain, which refuses non-interactive writes anyway. Plugins (buildx) and
# contexts still come from ~/.docker.
export PATH="$ROOT/scripts:$PATH"
if [ ! -f "$RECORDS/docker/config.json" ]; then
  ctx="$(docker context show 2>/dev/null || echo default)"
  mkdir -p "$RECORDS/docker"
  for d in cli-plugins contexts; do
    [ -e "$HOME/.docker/$d" ] && ln -sfn "$HOME/.docker/$d" "$RECORDS/docker/$d"
  done
  printf '{"currentContext": "%s", "credHelpers": {"%s": "ax-eks"}}\n' \
    "$ctx" "${IMAGE_REPO%%/*}" > "$RECORDS/docker/config.json"
fi
export DOCKER_CONFIG="$RECORDS/docker"

ax() { "$AX_SRC/bin/ax" --context "$KUBE_CONTEXT" -n ax-system "$@"; }
k() { kubectl --context "$KUBE_CONTEXT" "$@"; }

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
