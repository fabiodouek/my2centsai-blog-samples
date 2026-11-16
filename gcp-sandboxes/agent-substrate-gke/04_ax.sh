#!/usr/bin/env bash
# AX on top of Substrate: build the ax CLI and the two images from AX main at $AX_REF, push them to Artifact Registry,
# deploy PostgreSQL and ax-server. Port of google-ax-eks/scripts/05_ax.sh.
set -euo pipefail
source "$(dirname "$0")/env.sh"
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
AX_SRC="$WORK/ax"; RECORDS="$WORK/records"; mkdir -p "$RECORDS" "$WORK/bin"
HOST="$REGION-docker.pkg.dev"

step "Sources"
[ -d "$AX_SRC/.git" ] || git clone -q https://github.com/google/ax.git "$AX_SRC"
git -C "$AX_SRC" fetch -q origin "$AX_REF"; git -C "$AX_SRC" checkout -q --detach "$AX_REF"
echo "ax at $(git -C "$AX_SRC" rev-parse --short=12 HEAD) ($(git -C "$AX_SRC" log -1 --format=%cs))"
(cd "$AX_SRC" && go list -m go.ate.dev/substrate)

step "Docker credentials for Artifact Registry (a private config, your ~/.docker is untouched)"
mkdir -p "$RECORDS/docker"
for d in cli-plugins contexts; do [ -e "$HOME/.docker/$d" ] && ln -sfn "$HOME/.docker/$d" "$RECORDS/docker/$d"; done
printf '{"currentContext": "%s", "credHelpers": {"%s": "gcloud"}}\n' "$(docker context show 2>/dev/null || echo default)" "$HOST" > "$RECORDS/docker/config.json"
export DOCKER_CONFIG="$RECORDS/docker"

step "AX CLI"
(cd "$AX_SRC" && GOBIN="$WORK/bin" make install >/dev/null)
go version -m "$WORK/bin/ax" > "$RECORDS/cli-build-info.txt"

step "make images"
AX_IMAGE_REPO="$KO_DOCKER_REPO/ax"; IMAGE_TAG="ax-${AX_REF:0:12}"
start=$(date +%s)
(cd "$AX_SRC" && make images CONTAINER_CLI=docker IMAGE_ARCH=amd64 AX_IMAGE_REPO="$AX_IMAGE_REPO" IMAGE_TAG="$IMAGE_TAG" \
   AX_SERVER_IMAGE="$AX_IMAGE_REPO/ax-server:$IMAGE_TAG" AX_TASK_RUNNER_IMAGE="$AX_IMAGE_REPO/ax-task-runner:$IMAGE_TAG") \
   > "$RECORDS/make-images.log" 2>&1 || { tail -20 "$RECORDS/make-images.log"; exit 1; }
echo "make images took $(( $(date +%s) - start )) s"
AX_SERVER_IMAGE="$AX_IMAGE_REPO/ax-server@$(cat "$AX_SRC/bin/ax-server.digest")"
AX_DEFAULT_TASK_IMAGE="$AX_IMAGE_REPO/ax-task-runner@$(cat "$AX_SRC/bin/ax-task-runner.digest")"
printf 'AX_REF=%s\nSUBSTRATE_TAG=%s\nAX_SERVER_IMAGE=%s\nAX_DEFAULT_TASK_IMAGE=%s\n' "$AX_REF" "$SUBSTRATE_TAG" "$AX_SERVER_IMAGE" "$AX_DEFAULT_TASK_IMAGE" | tee "$RECORDS/source-and-images.txt"
sed -e "s|\${AX_SERVER_IMAGE}|$AX_SERVER_IMAGE|g" -e "s|\${AX_DEFAULT_TASK_IMAGE}|$AX_DEFAULT_TASK_IMAGE|g" "$AX_SRC/deploy/ax-server.yaml" > "$RECORDS/ax-server.yaml"

step "PostgreSQL (10 GiB on hyperdisk-balanced: the project's 500 GB pd-balanced quota is spent on Substrate's PostgreSQL)"
cat <<'YAML' | k apply -f -
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: {name: hyperdisk-balanced-rwo}
provisioner: pd.csi.storage.gke.io
parameters: {type: hyperdisk-balanced}
volumeBindingMode: WaitForFirstConsumer
allowVolumeExpansion: true
YAML
k -n ax-system get secret ax-postgres >/dev/null 2>&1 || k -n ax-system create secret generic ax-postgres --from-literal=password="$(openssl rand -hex 24)"
python3 - "$AX_SRC/deploy/postgres.yaml" > "$RECORDS/postgres.yaml" <<'PY'
import re, sys
print(re.sub(r'^(\s*)accessModes: \["ReadWriteOnce"\]', r'\g<0>\n\1storageClassName: hyperdisk-balanced-rwo', open(sys.argv[1]).read(), flags=re.M), end="")
PY
grep -n "storageClassName" -B3 "$RECORDS/postgres.yaml" | head
k apply -f "$RECORDS/postgres.yaml"
k -n ax-system rollout status statefulset/ax-postgres --timeout=5m

step "ax-server"
k apply -f "$RECORDS/ax-server.yaml"
k -n ax-system rollout status deployment/ax-server --timeout=3m
k -n ax-system get pods
