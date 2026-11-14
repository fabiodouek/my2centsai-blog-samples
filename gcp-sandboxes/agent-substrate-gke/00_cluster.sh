#!/usr/bin/env bash
# The CLI twin of the console steps: network pieces, then a zonal Standard cluster of 2 x c3-standard-4.
# About 7 minutes for the cluster. Costs about $0.61/hour while it runs (see README).
set -euo pipefail
: "${PROJECT_ID:?source env.sh first}" "${ZONE:?}" "${CLUSTER:?}" "${REGION:?}" "${CTX:?}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Private nodes need Cloud NAT for anything that is not Google, and Private Google Access for Google APIs.
gcloud compute networks subnets update default --region "$REGION" --enable-private-ip-google-access
gcloud compute routers create ate-router --network default --region "$REGION"
gcloud compute routers nats create ate-nat --router ate-router --region "$REGION" \
  --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges --no-enable-logging

# Rapid 1.37, Dataplane V2, Workload Identity, private nodes, the two beta certificate APIs Substrate needs,
# the node sysctls setup-gcp would set. No GKE Sandbox on the pool: its taint keeps Substrate's worker pods off.
# Boot disks are hyperdisk-balanced: a new project allows 500 GB of pd-balanced per region, and Substrate's
# PostgreSQL volume alone asks for 500 GiB of it.
version=$(gcloud container get-server-config --zone "$ZONE" --format=json |
  jq -r '.channels[] | select(.channel=="RAPID") | .defaultVersion')
gcloud beta container clusters create "$CLUSTER" --zone "$ZONE" --release-channel rapid --cluster-version "$version" \
  --num-nodes 2 --machine-type c3-standard-4 --disk-type hyperdisk-balanced --disk-size 100 --image-type COS_CONTAINERD \
  --enable-dataplane-v2 --workload-pool="$PROJECT_ID.svc.id.goog" --enable-private-nodes --enable-ip-alias \
  --no-enable-autoupgrade --no-enable-autorepair \
  --enable-kubernetes-unstable-apis=certificates.k8s.io/v1beta1/podcertificaterequests,certificates.k8s.io/v1beta1/clustertrustbundles \
  --system-config-from-file "$here/node-sysctls.yaml"

# Reach the control plane through its DNS endpoint only, as setup-gcp does for clusters it creates.
gcloud container clusters update "$CLUSTER" --zone "$ZONE" --enable-dns-access --enable-k8s-tokens-via-dns --enable-k8s-certs-via-dns
gcloud container clusters update "$CLUSTER" --zone "$ZONE" --no-enable-ip-access
gcloud container clusters get-credentials "$CLUSTER" --zone "$ZONE"
kubectl config rename-context "gke_${PROJECT_ID}_${ZONE}_${CLUSTER}" "$CTX"
kubectl --context "$CTX" get nodes -o custom-columns=NAME:.metadata.name,KERNEL:.status.nodeInfo.kernelVersion,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory
