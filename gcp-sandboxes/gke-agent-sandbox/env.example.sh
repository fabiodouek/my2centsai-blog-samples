# Source this first: `. ./env.example.sh` (copy to env.sh and edit)
export PROJECT=<your project id>          # billing on; APIs: container, compute, storage, iam
export REGION=us-central1
export ZONE=us-central1-a                 # zonal on purpose (a regional cluster hits "incompatible FeatureSet" on restore)
export CLUSTER=sbx-gke
export BUCKET=<globally unique name>      # Pod Snapshots bucket
export ROLE_ID=podSnapshotGcsReadWriter   # a deleted custom role ID cannot be reused for weeks: pick a new one if needed
export AGENT_SANDBOX_VERSION=v1.0.11      # the GKE docs still pin v1.0.6; the Python SDK version must match
export CLOUDSDK_CORE_PROJECT=$PROJECT
