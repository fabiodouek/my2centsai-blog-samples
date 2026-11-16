#!/usr/bin/env bash
# Substrate's own Counter demo (what `install.sh --demo` ends with): a gVisor actor that counts, deployed by
# `ate-setup deploy demo counter`. Releases don't publish the demo image, so ate-setup builds it with ko and
# pushes it to $KO_DOCKER_REPO (the Artifact Registry repository bootstrap made).
set -euo pipefail
source "$(dirname "$0")/env.sh"
export DOCKER_CONFIG="$WORK/records/docker"
cd "$WORK/substrate"
export PROJECT_ID PROJECT_NUMBER SNAPSHOT_LOCATION SNAPSHOT_TRANSFER_GSA_NAME NO_DEV_ENV=1 KO_DEFAULTPLATFORMS=linux/amd64 KO_DOCKER_REPO
export CLUSTER_NAME="$CLUSTER" CLUSTER_LOCATION="$ZONE" GCE_REGION="$REGION" VERSION="$SUBSTRATE_TAG"
export ATE_CREDENTIAL_PROVIDER='{"name":"k8s.io"}' ATE_INSTALL_SANDBOX_CLASSES=gvisor
export ATE_INGRESS_ALLOWED_SPIFFE_IDS="spiffe://cluster.local/ns/ax-system/sa/ax-server,spiffe://cluster.local/ns/ate-system/sa/atenet-client"
t0=$(date +%s)
go run ./cmd/ate-setup deploy demo counter 2>&1 | tee "$WORK/logs/demo.log"
echo "demo deploy took $(( $(date +%s) - t0 )) s"
