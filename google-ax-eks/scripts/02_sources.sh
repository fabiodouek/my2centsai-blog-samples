#!/usr/bin/env bash
# Check out AX and Substrate at the pinned refs and apply the EKS patch.
set -euo pipefail
source "$(dirname "$0")/env.sh"

mkdir -p "$SRC" "$RECORDS"

checkout() {
  local url=$1 dir=$2 ref=$3
  [ -d "$dir/.git" ] || git clone --quiet "$url" "$dir"
  git -C "$dir" fetch --quiet origin "$ref"
  git -C "$dir" checkout --quiet --force --detach "$ref"
  git -C "$dir" clean --quiet -fdx -e bin/
  echo "$(basename "$dir") at $(git -C "$dir" rev-parse --short=12 HEAD) ($(git -C "$dir" log -1 --format=%cs))"
}

step "Sources"
checkout https://github.com/google/ax.git "$AX_SRC" "$AX_REF"
checkout https://github.com/agent-substrate/substrate.git "$SUBSTRATE_SRC" "$SUBSTRATE_REF"

step "AX's recorded Substrate dependency"
(cd "$AX_SRC" && go list -m go.ate.dev/substrate)

step "EKS overlay for Substrate"
# ate-setup only knows kind and GKE. The patch adds an ATE_SYSTEM_OVERLAY
# override and drops the GCS-only checks when it is set; the overlay itself
# lives in ../substrate-eks and is copied into the checkout.
git -C "$SUBSTRATE_SRC" apply "$ROOT/substrate-eks/ate-setup-overlay.patch"
rm -rf "$SUBSTRATE_SRC/manifests/ate-install/eks"
cp -R "$ROOT/substrate-eks/overlay" "$SUBSTRATE_SRC/manifests/ate-install/eks"
sed -i.bak "s/AWS_REGION_PLACEHOLDER/$AWS_REGION/" "$SUBSTRATE_SRC/manifests/ate-install/eks/kustomization.yaml"
rm "$SUBSTRATE_SRC/manifests/ate-install/eks/kustomization.yaml.bak"
git -C "$SUBSTRATE_SRC" status --short
