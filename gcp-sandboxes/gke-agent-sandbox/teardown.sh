#!/usr/bin/env bash
# Delete everything this series part created, then verify. Order matters: PVCs before the cluster (their disks outlive it otherwise).
set -uo pipefail
: "${PROJECT:?source env.sh first}" "${ZONE:?}" "${CLUSTER:?}" "${BUCKET:?}" "${ROLE_ID:?}"
V=${AGENT_SANDBOX_VERSION:-v1.0.11}
RAW=https://raw.githubusercontent.com/kubernetes-sigs/agent-sandbox/$V

echo "### in the cluster"
kubectl delete sandboxclaims,sandboxwarmpools,sandboxtemplates,sandboxes --all -A --wait=false
kubectl delete podsnapshotmanualtriggers.podsnapshot.gke.io,podsnapshots.podsnapshot.gke.io --all -A --wait=false
kubectl delete pvc --all -A --wait=true --timeout=120s
kubectl delete pod runc-probe target burn-runc --ignore-not-found
kubectl delete validatingadmissionpolicybinding secure-sandbox-binding --ignore-not-found
kubectl delete validatingadmissionpolicy secure-sandbox-policy --ignore-not-found
kubectl delete -f "$RAW/sandbox-router/deploy/rbac.yaml" --ignore-not-found
kubectl delete -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/$V/sandbox-with-extensions.yaml" --ignore-not-found
kubectl get pv 2>&1 | head -3

echo "### node pools and cluster"
for p in microvm mismatch; do gcloud container node-pools delete "$p" --cluster "$CLUSTER" --zone "$ZONE" --quiet 2>/dev/null; done
gcloud container clusters delete "$CLUSTER" --zone "$ZONE" --quiet

echo "### bucket, role, bindings"
gcloud storage rm -r "gs://$BUCKET" --quiet            # removes the objects and the managed folder; the bucket's IAM bindings go with it
gcloud iam roles delete "$ROLE_ID" --project "$PROJECT" --quiet   # the role ID cannot be reused for about 37 days

echo "### verify"
gcloud container clusters list
gcloud compute instances list
gcloud compute disks list
gcloud storage buckets list --filter="name~snap" --format="value(name)"
gcloud iam roles list --project "$PROJECT" --filter="name~$ROLE_ID" --format="value(name,deleted)" --show-deleted
