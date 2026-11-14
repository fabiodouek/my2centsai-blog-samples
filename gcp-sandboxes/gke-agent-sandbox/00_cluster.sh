#!/usr/bin/env bash
# Cluster, node pools and the snapshot bucket. About 7 min for the cluster, 1.5 min per pool.
# Costs about $0.41/hour while it runs (cluster fee + e2-standard-2 + c3-standard-4 + disks).
set -euo pipefail
: "${PROJECT:?source env.sh first}" "${ZONE:?}" "${CLUSTER:?}" "${BUCKET:?}" "${ROLE_ID:?}"

# Standard, zonal, Rapid channel (1.37.x), Workload Identity, Dataplane V2 (NetworkPolicy is enforced), Pod Snapshots.
gcloud beta container clusters create "$CLUSTER" --zone "$ZONE" --release-channel rapid --num-nodes 1 \
  --machine-type e2-standard-2 --workload-pool="$PROJECT.svc.id.goog" --enable-dataplane-v2 \
  --enable-pod-snapshots --no-enable-autoupgrade
gcloud container clusters get-credentials "$CLUSTER" --zone "$ZONE"

# The sandbox pool: C3, because whole-pod snapshots do not work on E2. GKE Sandbox needs a separate, non-sandboxed pool (the one above).
gcloud container node-pools create gvisor --cluster "$CLUSTER" --zone "$ZONE" --num-nodes 1 \
  --machine-type c3-standard-4 --image-type cos_containerd --sandbox=type=gvisor --workload-metadata=GKE_METADATA

# Snapshot bucket: hierarchical namespace on, soft delete off, plus a managed folder.
gcloud storage buckets create "gs://$BUCKET" --location="${REGION:-us-central1}" --uniform-bucket-level-access \
  --enable-hierarchical-namespace --soft-delete-duration=0
gcloud storage managed-folders create "gs://$BUCKET/snapshots/"

# Custom role + Workload Identity bindings for the namespace that will run snapshotted sandboxes.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
NS=pod-snapshots-ns; KSA=pod-snapshot-sa
gcloud iam roles create "$ROLE_ID" --project="$PROJECT" \
  --permissions="storage.objects.get,storage.objects.create,storage.objects.delete,storage.folders.create"
kubectl create namespace "$NS"
kubectl create serviceaccount "$KSA" -n "$NS"
POOL="projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$PROJECT.svc.id.goog"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --role=roles/storage.bucketViewer \
  --member="principalSet://iam.googleapis.com/$POOL/namespace/$NS"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --role="projects/$PROJECT/roles/$ROLE_ID" \
  --member="principal://iam.googleapis.com/$POOL/subject/ns/$NS/sa/$KSA"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --role=roles/storage.objectUser \
  --member="principal://iam.googleapis.com/$POOL/subject/ns/$NS/sa/$KSA"
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --role=roles/storage.objectUser \
  --member="serviceAccount:service-$PROJECT_NUMBER@container-engine-robot.iam.gserviceaccount.com"
