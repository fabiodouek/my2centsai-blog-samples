#!/usr/bin/env bash
# Removes everything this part created, in the order that leaves nothing billing. Safe to re-run.
#  tasks/actors -> WorkerPools -> cluster -> orphaned pvc-* disks -> buckets -> Artifact Registry -> service accounts
#  and IAM bindings -> Cloud NAT and router. Then prints what is left, so you can check Billing the next day.
set -uo pipefail
source "$(dirname "$0")/env.sh"
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "tasks, actors and WorkerPools (best effort: the cluster may already be gone)"
for as in default lifecycle stress native creds; do
  ATESPACE=$as python3 -c "import common; common.delete_all()" 2>/dev/null || true
done
k delete pod router-probe -n ax-system --ignore-not-found 2>/dev/null || true
k delete workerpool --all -A 2>/dev/null || true

step "the cluster, then the volumes it leaves behind"
gcloud container clusters delete "$CLUSTER" --zone "$ZONE" --quiet
# Substrate's 500 GiB and AX's 10 GiB PostgreSQL volumes come from PersistentVolumeClaims, so they outlive the cluster.
for d in $(gcloud compute disks list --filter="name~^pvc-" --format="value(name)"); do
  gcloud compute disks delete "$d" --zone "$ZONE" --quiet
done

step "buckets, images, service accounts, IAM"
gcloud storage rm -r "gs://$BUCKET_NAME" "gs://$BUCKET_NAME-assets" 2>/dev/null || true
gcloud artifacts repositories delete ate-images --location "$REGION" --quiet 2>/dev/null || true
# substrate-gke's own teardown removes the node and snapshot-transfer service accounts and the bindings it made.
(cd "$WORK/substrate-gke/tools/setup-gcp" && PROJECT_ID="$PROJECT_ID" PROJECT_NUMBER="$PROJECT_NUMBER" BUCKET_NAME="$BUCKET_NAME" \
  CLUSTER_NAME="$CLUSTER" CLUSTER_LOCATION="$ZONE" GCE_REGION="$REGION" NODE_GSA_NAME=ate-gke-node \
  SNAPSHOT_TRANSFER_GSA_NAME=ate-snapshot-transfer NO_DEV_ENV=1 ./teardown.sh --all) || true   # --all also deletes the buckets
gcloud iam service-accounts delete "ate-snapshot-transfer@$PROJECT_ID.iam.gserviceaccount.com" --quiet 2>/dev/null || true
gcloud iam service-accounts delete "ate-gke-node@$PROJECT_ID.iam.gserviceaccount.com" --quiet 2>/dev/null || true

step "Cloud NAT and router"
gcloud compute routers nats delete ate-nat --router ate-router --region "$REGION" --quiet 2>/dev/null || true
gcloud compute routers delete ate-router --region "$REGION" --quiet 2>/dev/null || true
gcloud compute networks subnets update default --region "$REGION" --no-enable-private-ip-google-access 2>/dev/null || true

step "what is left (all of these should be empty)"
gcloud container clusters list
gcloud compute disks list
gcloud compute instances list
gcloud compute routers list
gcloud storage buckets list --format="value(name)" | grep -E "^ate-snap" || echo "(no snapshot buckets)"
gcloud artifacts repositories list --location "$REGION" 2>/dev/null
gcloud iam service-accounts list --format="value(email)" | grep -E "ate-" || echo "(no ate-* service accounts)"
