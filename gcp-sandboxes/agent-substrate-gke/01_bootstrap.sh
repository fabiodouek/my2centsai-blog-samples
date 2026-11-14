#!/usr/bin/env bash
# What the installer's "Provision GCP resources" step runs: setup-gcp bootstrap, from the substrate-gke checkout.
# It turns the beta APIs on (a control-plane update, about 10 minutes), creates the buckets, the snapshot transfer
# service account, the Artifact Registry repository, the IAM bindings and three monitoring dashboards.
set -euo pipefail
source "$(dirname "$0")/env.sh"
mkdir -p "$WORK/logs" "$WORK/bin"
[ -d "$WORK/substrate-gke" ] || git clone -q https://github.com/ai-on-gke/substrate-gke.git "$WORK/substrate-gke"
git -C "$WORK/substrate-gke" checkout -q "$SUBSTRATE_GKE_TAG"
cd "$WORK/substrate-gke"
# The installer exports these (installer/internal/snapshot, Builder.env). We bring our own two-node pool, so no system pools.
export PROJECT_ID PROJECT_NUMBER BUCKET_NAME SNAPSHOT_TRANSFER_GSA_NAME NO_DEV_ENV=1 SEPARATE_SYSTEM_NODES=false
export GCE_REGION="$REGION" CLUSTER_LOCATION="$ZONE" CLUSTER_NAME="$CLUSTER" RELEASE_CHANNEL=rapid
export ARTIFACT_REGISTRY_REPOSITORY=ate-images CREATE_ARTIFACT_REPOSITORY=true ENABLE_NESTED_VIRTUALIZATION=false
t0=$(date +%s)
go -C tools/setup-gcp run . bootstrap 2>&1 | tee "$WORK/logs/bootstrap.log"
echo "bootstrap took $(( $(date +%s) - t0 )) s"
