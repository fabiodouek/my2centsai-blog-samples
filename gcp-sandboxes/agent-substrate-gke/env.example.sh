# Source this first: `. ./env.sh` (copy to env.sh and edit). Everything below is a setting, not a secret.
export PROJECT_ID=<your project id>            # billing on
export REGION=us-central1
export ZONE=us-central1-a                      # zonal on purpose: one control plane, fewer zones to pay for
export CLUSTER=ate-sbx
export CTX=ate-gke                             # kubectl context alias, so the project ID stays off screen
export WORK=$HOME/ate-gke-work                 # sources, binaries and logs go here, outside this repo

# Versions this post was run with
export SUBSTRATE_GKE_TAG=v1.0.0-gke.0          # the GKE installer; it pins Substrate v1.0.0
export SUBSTRATE_TAG=v1.0.0
export AX_REF=eac8313392111328560793cc7fe664cf3e7b4e4c   # AX main on 2026-11-13; go.mod pins go.ate.dev/substrate v1.0.0

# Set by setup-gcp/ate-setup (names are the installer's defaults except the bucket, which is made unique)
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
export BUCKET_NAME="ate-snap-$PROJECT_NUMBER"
export SNAPSHOT_LOCATION="gs://$BUCKET_NAME/"
export SNAPSHOT_TRANSFER_GSA_NAME=ate-snapshot-transfer
export KO_DOCKER_REPO="$REGION-docker.pkg.dev/$PROJECT_ID/ate-images"
export GOTOOLCHAIN=auto GOWORK=off             # Substrate's go.mod names a newer toolchain than most laptops have
export PATH="$WORK/bin:$PATH"                  # kubectl-ate and ax live here
