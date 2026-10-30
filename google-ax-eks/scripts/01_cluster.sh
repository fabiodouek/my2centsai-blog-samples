#!/usr/bin/env bash
# Create the EKS cluster, S3 bucket and ECR template, then check the two
# things Substrate needs from the cluster that EKS doesn't advertise:
# the pod certificate APIs and a worker kernel with OverlayFS lowerdir+.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../terraform"

mkdir -p "$HERE/../.records"
LOG="$HERE/../.records/terraform-apply.log"
terraform init -input=false >/dev/null
start=$(date +%s)
# Full output goes to the log; the terminal shows what was created and how long it took.
terraform apply -input=false -auto-approve -no-color 2>&1 | tee "$LOG" \
  | grep -E --line-buffered 'Creation complete after|Modifications complete after|^Apply complete|^Error' || true
echo "terraform apply took $(( $(date +%s) - start ))s (full log: .records/terraform-apply.log)"
grep -q '^Apply complete' "$LOG"

source "$HERE/env.sh"

step "kubeconfig"
aws eks update-kubeconfig --region "$AWS_REGION" --name "$CLUSTER_NAME" --alias "$KUBE_CONTEXT"
k get nodes -o wide

step "Kubernetes version and certificate APIs"
k version -o json | jq -r '.serverVersion.gitVersion'
k api-resources --api-group=certificates.k8s.io -o wide
# Served versions per resource. 1.37 serves both under v1; v1beta1 is off and
# EKS gives you no way to turn it on.
k get --raw /apis/certificates.k8s.io | jq -r '.versions[].groupVersion'

step "Pod certificate projection survives admission"
# A pod with a podCertificate projected source, created server-side with
# --dry-run so nothing runs. If anything in the admission chain predates the
# field (old mutating webhooks, a stale schema cache), the source comes back
# empty: {}. kagent #3196 hit exactly that on EKS 1.37.
cat <<'EOF' | k create --dry-run=server -o jsonpath='{.spec.volumes[0].projected.sources}{"\n"}' -f -
apiVersion: v1
kind: Pod
metadata:
  name: podcert-probe
  namespace: default
spec:
  containers:
  - name: c
    image: public.ecr.aws/docker/library/busybox:1.37
    command: ["true"]
  volumes:
  - name: id
    projected:
      sources:
      - podCertificate:
          signerName: example.com/probe
          keyType: ED25519
          credentialBundlePath: bundle.pem
EOF

step "Worker kernel"
# Substrate's image cache mounts overlays with lowerdir+ (Linux 6.8+).
k get nodes -o custom-columns='NODE:.metadata.name,KERNEL:.status.nodeInfo.kernelVersion,OS:.status.nodeInfo.osImage,RUNTIME:.status.nodeInfo.containerRuntimeVersion'
