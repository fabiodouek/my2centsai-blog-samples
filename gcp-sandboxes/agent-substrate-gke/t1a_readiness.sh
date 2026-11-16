#!/usr/bin/env bash
# Test 1a: the readiness checks from the EKS install post (I2), on GKE. About 10 seconds.
#  (a) are the certificate APIs served as v1?  (b) does a pod with a podCertificate volume pass admission?
#  (c) is the node kernel >= 6.8 (OverlayFS lowerdir+)?   Plus the node facts the post records.
set -euo pipefail
source "$(dirname "$0")/env.sh"
t0=$(date +%s)
echo "== (a) certificates.k8s.io versions served"
k get --raw /apis/certificates.k8s.io | jq -r '.versions[].version' | paste -sd' ' -
k api-resources --api-group=certificates.k8s.io
echo "== (b) server-side dry run of a pod with a projected podCertificate volume"
cat <<'YAML' | k apply --dry-run=server -o jsonpath='{.spec.volumes[0].projected.sources[0].podCertificate.signerName}{"\n"}' -f -
apiVersion: v1
kind: Pod
metadata: {name: pcr-dryrun, namespace: default}
spec:
  containers: [{name: c, image: busybox, command: [sleep, "1"]}]
  volumes:
  - name: id
    projected:
      sources:
      - podCertificate: {signerName: podidentity.podcert.ate.dev/identity, keyType: ECDSAP256, credentialBundlePath: b.pem}
YAML
echo "== (c) node kernel, OS, allocatable"
k get nodes -o custom-columns=NAME:.metadata.name,KERNEL:.status.nodeInfo.kernelVersion,OS:.status.nodeInfo.osImage,RUNTIME:.status.nodeInfo.containerRuntimeVersion,CPU:.status.allocatable.cpu,MEMORY:.status.allocatable.memory
echo "readiness checks took $(( $(date +%s) - t0 )) s"
echo "== versions"
gcloud version | head -2; kubectl version --client | head -1; go version; docker --version
k version -o json | jq -r '"server " + .serverVersion.gitVersion'
git -C "$WORK/substrate-gke" describe --tags; git -C "$WORK/substrate" describe --tags; git -C "$WORK/ax" rev-parse --short=12 HEAD
