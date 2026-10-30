#!/usr/bin/env bash
# Build the AX CLI, ax-server and ax-task-runner from the pinned ref, push the
# images to ECR, and deploy PostgreSQL and ax-server into ax-system.
# Follows docs/installation.md steps 2 and 3.
set -euo pipefail
source "$(dirname "$0")/env.sh"

AX_IMAGE_REPO="$IMAGE_REPO/ax"
IMAGE_TAG="ax-${AX_REF:0:12}"

step "AX CLI"
(cd "$AX_SRC" && GOBIN="$AX_SRC/bin" make install >/dev/null)
"$AX_SRC/bin/ax" version 2>/dev/null || true
go version -m "$AX_SRC/bin/ax" > "$RECORDS/cli-build-info.txt"

step "make images"
start=$(date +%s)
(
  cd "$AX_SRC"
  make images CONTAINER_CLI=docker IMAGE_ARCH=amd64 \
    AX_IMAGE_REPO="$AX_IMAGE_REPO" IMAGE_TAG="$IMAGE_TAG" \
    AX_SERVER_IMAGE="$AX_IMAGE_REPO/ax-server:$IMAGE_TAG" \
    AX_TASK_RUNNER_IMAGE="$AX_IMAGE_REPO/ax-task-runner:$IMAGE_TAG"
) > "$RECORDS/make-images.log" 2>&1 || { tail -20 "$RECORDS/make-images.log"; exit 1; }
grep -E '^==>' "$RECORDS/make-images.log"
echo "make images took $(( $(date +%s) - start ))s"

# Deploy by digest, never by tag (the install guide insists, and Substrate
# rejects mutable references for task images).
AX_SERVER_IMAGE="$AX_IMAGE_REPO/ax-server@$(cat "$AX_SRC/bin/ax-server.digest")"
AX_DEFAULT_TASK_IMAGE="$AX_IMAGE_REPO/ax-task-runner@$(cat "$AX_SRC/bin/ax-task-runner.digest")"
printf 'AX_REF=%s\nSUBSTRATE_REF=%s\nAX_SERVER_IMAGE=%s\nAX_DEFAULT_TASK_IMAGE=%s\n' \
  "$AX_REF" "$SUBSTRATE_REF" "$AX_SERVER_IMAGE" "$AX_DEFAULT_TASK_IMAGE" \
  | tee "$RECORDS/source-and-images.txt"

step "Render ax-server.yaml"
sed -e "s|\${AX_SERVER_IMAGE}|$AX_SERVER_IMAGE|g" \
    -e "s|\${AX_DEFAULT_TASK_IMAGE}|$AX_DEFAULT_TASK_IMAGE|g" \
    "$AX_SRC/deploy/ax-server.yaml" > "$RECORDS/ax-server.yaml"

step "PostgreSQL"
# Create the password only once: PostgreSQL reads it when it initializes an
# empty volume, so replacing it later breaks authentication.
k -n ax-system get secret ax-postgres >/dev/null 2>&1 \
  || k -n ax-system create secret generic ax-postgres \
       --from-literal=password="$(openssl rand -hex 24)"
k apply -f "$AX_SRC/deploy/postgres.yaml"
k -n ax-system rollout status statefulset/ax-postgres --timeout=5m

step "ax-server"
k apply -f "$RECORDS/ax-server.yaml"
k -n ax-system rollout status deployment/ax-server --timeout=3m
k -n ax-system get pods
