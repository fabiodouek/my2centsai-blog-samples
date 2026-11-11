# AWS Nitro Sandboxes on Amazon EKS

Part 4 of the AWS Nitro Sandboxes series. On EKS a pod opts in with `runtimeClassName: isolated-sandbox` or `confidential-sandbox`, and EKS Auto Mode provisions a compatible node with the Nitro Sandbox runtime; there is no cluster or node setting to turn on. Terraform creates a VPC and an Auto Mode cluster; everything after that is `kubectl` and the AWS CLI. Then: a plain pod against a sandboxed one on the same node, start latency, host-level settings and network isolation, how many sandbox pods fit on a node, and a confidential pod that gets a KMS secret through EKS Pod Identity only while its Cedar policy matches, plus what happens when the pod is changed.

Blog post: [AWS Nitro Sandboxes on Amazon EKS](https://my2cents.ai/deep-dive/nitro-sandboxes-eks)

Video walkthrough: https://youtu.be/2tRvC-QUdYI

Validated end to end on 2026-11-10 in us-east-1, twice: an exploration run and the recorded run the video shows (EKS 1.37, kubectl 1.37.2, AWS CLI 2.39.7). The manifests here are the ones that ran, and the commands are those runs' commands without the recording wrappers (the recording built the policy-carrying manifests with a short Python step; `jq` does the same here). The expected output is the recorded run's, trimmed; your PCR17, node names and timings will differ. The cluster in both runs came from a single-file version of [`terraform/`](terraform) with the same resources (54 to destroy), fixed Availability Zones `us-east-1a`/`us-east-1b` and no Region variable.

## Files

| File | What it is |
|---|---|
| `terraform/` | VPC `10.63.0.0/16` (two private and two public subnets, one NAT gateway) and an EKS Auto Mode cluster `my2cents-nitro-eks` on Kubernetes 1.37 with only the `system` built-in node pool |
| `nodepool.yaml` | A Graviton NodePool: `m8g.xlarge`, on demand, capped at 4 vCPUs (one node). The built-in general-purpose pool is amd64 only |
| `pod-plain.yaml`, `pod-sandbox.yaml` | The probe ([`probe.sh`](probe.sh)) in an ordinary pod and in `isolated-sandbox` |
| `pod-netiso.yaml` | The probe with `sandbox.amazonaws.com/network-isolated: "true"` |
| `pod-hostnetwork.yaml`, `pod-hostpid.yaml` | The probe in `isolated-sandbox` with `hostNetwork: true` or `hostPID: true` |
| `density.yaml` | A Deployment of 22 `isolated-sandbox` replicas that sleep, 50m / 64Mi each, to fill a node's sandbox slots |
| `sa.yaml` | Service account `attest`, for EKS Pod Identity |
| `pod-trust.json` | Trust policy for the Pod Identity role |
| `pod-attest-isolated.json`, `pod-confidential.json` | The attest container from part 3 (`../ecs/attest.sh`) in `isolated-sandbox` and `confidential-sandbox`, with `enableServiceLinks: false`, `<NSM_PY_B64>` and `<SECRET_CT>` placeholders and no policy |
| `pod-genpolicy.yaml` | A plain pod that downloads, verifies and runs `eks-nitro-sandbox-genpolicy` on the manifest in ConfigMap `confidential-manifest` ([`genpolicy.sh`](genpolicy.sh)) |
| `kms-eks-policy.json` | Key policy: account administration, plus `kms:Decrypt` for the pod's role only with PCR17 and `PCR8:ServiceName = eks.amazonaws.com` |

## Before you start

You need the tools in the [series prerequisites](../README.md#prerequisites), including kubectl 1.36 or later and `jq`. In `bash`:

```bash
cd aws-nitro-sandboxes/eks && mkdir -p build
export AWS_REGION=us-east-1
CLUSTER=my2cents-nitro-eks
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

## Step 1: The cluster

Nitro Sandboxes need Kubernetes 1.36 or later. Neither the EKS API nor the Terraform module has a sandbox argument: Auto Mode includes the support. The apply took about 13 to 14 minutes.

```bash
terraform -chdir=terraform init
terraform -chdir=terraform apply
aws eks update-kubeconfig --region $AWS_REGION --name $CLUSTER
kubectl version | grep Server
kubectl get nodes
```

```text
Server Version: v1.37.1-eks-577a51e
No resources found
```

The `system` pool doesn't launch a node on its own; it's enabled because Auto Mode needs at least one built-in pool to create the `default` NodeClass that `nodepool.yaml` uses.

## Step 2: The RuntimeClasses, then a Graviton node pool

Auto Mode ships both RuntimeClasses before the cluster has a node:

```bash
kubectl get runtimeclass
kubectl get runtimeclass isolated-sandbox -o yaml
```

```text
NAME                   HANDLER                AGE
confidential-sandbox   confidential-sandbox   2m57s
isolated-sandbox       isolated-sandbox       2m57s
```

```yaml
apiVersion: node.k8s.io/v1
handler: isolated-sandbox
kind: RuntimeClass
metadata:
  name: isolated-sandbox
overhead:
  podFixed:
    eks.amazonaws.com/nitro-sandbox: "1"
    memory: 256Mi
scheduling:
  nodeSelector:
    eks.amazonaws.com/nitro-sandbox: "true"
```

`confidential-sandbox` is the same with `memory: 512Mi`. There is no toleration, although the launch blog mentions one, and the sandbox node gets no taint, so plain pods schedule onto sandbox nodes too.

```bash
kubectl apply -f nodepool.yaml
```

The NodePool has no sandbox setting. Its `limits.cpu: "4"` keeps it to one `m8g.xlarge`: pods that don't fit stay `Pending` instead of buying a second node.

## Step 3: A sandboxed pod and a plain pod

The first sandbox pod makes Auto Mode launch the node:

```bash
kubectl apply -f pod-sandbox.yaml && kubectl wait pod/hello-sandbox --for=condition=Ready --timeout=10m
kubectl get nodes -L node.kubernetes.io/instance-type,eks.amazonaws.com/nitro-sandbox
NODE=$(kubectl get pod hello-sandbox -o jsonpath='{.spec.nodeName}')
kubectl get node $NODE -o jsonpath='{"taints: "}{.spec.taints}{"\n"}{"sandbox slots: "}{.status.allocatable.eks\.amazonaws\.com/nitro-sandbox}{"\n"}'
```

```text
NAME                  STATUS   ROLES    AGE   VERSION               INSTANCE-TYPE   NITRO-SANDBOX
i-056659827b67f2792   Ready    <none>   14s   v1.37.0-eks-29d7970   m8g.xlarge      true
taints:
sandbox slots: 20
```

`hello-sandbox` was Ready 50 seconds after `kubectl apply` in the recorded run, including the node launch (85 seconds in the exploration run). Then the same pod without `runtimeClassName`:

```bash
kubectl apply -f pod-plain.yaml && kubectl wait pod/hello-plain --for=condition=Ready --timeout=5m
kubectl get pods -o wide
kubectl logs hello-sandbox
kubectl logs hello-plain
```

Both run on the same node. Selected lines:

| | `hello-sandbox` | `hello-plain` |
|---|---|---|
| `kernel` | `6.18.54 #1 SMP Thu Oct 22 17:48:46 UTC 2026` | `6.18.56 #2 SMP Fri Oct 30 22:32:43 UTC 2026` (the node's) |
| `uptime` | 5.43 | 28.12 (the node's) |
| `nproc` | 1 | 4 |
| `MemTotal` | 666384 kB | 16048204 kB |
| `disks` | `dm-0 vda vdb vdc` | `dm-0 loop0 … loop7 nvme0n1 nvme1n1 zram0` |
| `/dev/nsm` | `crw-rw-rw-. 1 root root 10, 258` | `No such file or directory` |
| `imds placement/region` | `us-east-1 [HTTP 200]` | `[HTTP 401]` |
| `imds iam/security-credentials/` | `Not Found [HTTP 404]` | `[HTTP 401]` |

The sandbox is its own VM, sized from the pod's requests (500m rounds up to one vCPU; the memory comes from the 512Mi request, not the 1Gi limit). The IMDSv2-only metadata surrogate answers the placement paths and returns 404 for credentials, as the EKS docs describe. The probe's `sa token` line shows the service account token mounted in isolated mode (checked in the exploration run), although the overview page says it isn't; turn off automounting yourself if you rely on that.

## Step 4: Start time on a warm node

With the node up and the image cached:

```bash
for i in 1 2; do
  sed "s/name: hello-sandbox/name: warm-sandbox-$i/" pod-sandbox.yaml > build/ws.yaml
  time (kubectl apply -f build/ws.yaml && kubectl wait pod/warm-sandbox-$i --for=condition=Ready --timeout=2m)
  sed "s/name: hello-plain/name: warm-plain-$i/" pod-plain.yaml > build/wp.yaml
  time (kubectl apply -f build/wp.yaml && kubectl wait pod/warm-plain-$i --for=condition=Ready --timeout=2m)
done
kubectl delete pod warm-sandbox-1 warm-sandbox-2 warm-plain-1 warm-plain-2 --wait=false
```

Recorded run: 5.32 s and 4.69 s for the sandboxed pods, 1.97 s and 1.69 s for the plain ones. The sandbox adds about three seconds.

## Step 5: Host-level settings and network isolation

```bash
kubectl apply -f pod-hostpid.yaml -f pod-netiso.yaml -f pod-hostnetwork.yaml
kubectl wait pod/sandbox-hostpid pod/sandbox-network-isolated --for=condition=Ready --timeout=5m
kubectl exec sandbox-hostpid -- bash -c 'echo pid1: $(cat /proc/1/comm), processes: $(ls /proc | grep -c ^[0-9])'
kubectl exec sandbox-network-isolated -- bash -c 'echo interfaces: $(tail -n +3 /proc/net/dev | cut -d: -f1 | tr -d " " | tr "\n" " "); curl -sS -m 5 https://aws.amazon.com 2>&1 | head -1'
kubectl get pod sandbox-hostnetwork --no-headers | awk '{print $1, $3}'
kubectl get events --field-selector involvedObject.name=sandbox-hostnetwork,reason=FailedCreatePodSandBox -o jsonpath='{.items[0].message}' | grep -o 'cri-proxy: .*'
```

```text
pid1: systemd, processes: 167
interfaces: lo dummy0
curl: (6) Could not resolve host: aws.amazon.com
sandbox-hostnetwork ContainerCreating
cri-proxy: hostNetwork is not supported for nitro-sandbox pods; remove `hostNetwork: true` from the pod spec
```

- `hostPID: true` runs, and "host" is the sandbox VM: PID 1 is the guest's `systemd`, and nothing on the node is visible.
- `sandbox.amazonaws.com/network-isolated: "true"` leaves only `lo` and `dummy0`: no DNS, egress, IMDS or Pod Identity. Logs and exec still work over vsock.
- `hostNetwork: true` is accepted by the API server and scheduled, then retries in `ContainerCreating` forever. If you want it refused at admission, write an admission policy.

```bash
kubectl delete pod sandbox-hostpid sandbox-network-isolated sandbox-hostnetwork --wait=false
```

## Step 6: How many sandbox pods fit on a node

```bash
kubectl apply -f density.yaml
kubectl get pods -l app=density --no-headers | awk '{print $3}' | sort | uniq -c   # repeat for a few minutes
P=$(kubectl get pods -l app=density --field-selector=status.phase=Pending -o name | head -1)
kubectl get events --field-selector involvedObject.name=${P#pod/},reason=FailedScheduling -o custom-columns=M:.message --no-headers | tail -1
kubectl get nodes -o jsonpath='{.items[0].status.allocatable.eks\.amazonaws\.com/nitro-sandbox}{"\n"}'
kubectl delete deployment density
```

The recorded run held at 19 running and 3 pending from 54 seconds on:

```text
0/1 nodes are available: 1 Insufficient eks.amazonaws.com/nitro-sandbox ...
20
```

The node advertises 20 sandbox slots (5 × its 4 vCPUs). With this Deployment, four runs on fresh or recently used nodes got 18, 20, 18 and 19 pods running; I didn't establish what held the missing slots. One run on a fresh node also had two pods rejected by the kubelet with `OutOfeks.amazonaws.com/nitro-sandbox ... used: 20, capacity: 20`, so a Deployment can leave `Failed` pods behind. Don't plan on all 20.

Auto Mode consolidates an empty sandbox node like any other, so the node name can change between steps.

## Step 7: A confidential pod

### 7.1 Pod Identity role

The role has no IAM policy: `kms:Decrypt` comes from the key policy alone, as in part 3. Auto Mode needs no Pod Identity Agent add-on.

```bash
kubectl apply -f sa.yaml
aws iam create-role --role-name my2cents-nitro-eks-attest --assume-role-policy-document file://pod-trust.json
aws eks create-pod-identity-association --cluster-name $CLUSTER --namespace default --service-account attest \
  --role-arn arn:aws:iam::$ACCOUNT_ID:role/my2cents-nitro-eks-attest
```

### 7.2 KMS key and secret

The key starts with the admin-only policy from part 3. The key ID goes in `build/key-id` for cleanup.

```bash
sed "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" ../ecs/kms-ecs-initial.json > build/kms-eks-initial.json
KEY_ID=$(aws kms create-key --description "Nitro Sandboxes on EKS demo" \
  --policy file://build/kms-eks-initial.json --query KeyMetadata.KeyId --output text)
echo "$KEY_ID" > build/key-id
printf '%s' 'eks: only a measured pod can read this' > build/secret.txt
SECRET_CT=$(aws kms encrypt --key-id "$KEY_ID" --plaintext fileb://build/secret.txt --query CiphertextBlob --output text)
```

### 7.3 The pods, without a policy

```bash
NSM_PY_B64=$(base64 < ../nsm/nsm.py | tr -d '\n')
FILL='.spec.containers[0].env |= map(if .name == "NSM_PY_B64" then .value = $nsm elif .name == "SECRET_CT" then .value = $ct else . end)'
for p in pod-attest-isolated pod-confidential; do
  jq --arg nsm "$NSM_PY_B64" --arg ct "$SECRET_CT" "$FILL" $p.json > build/$p.json
done
```

Both manifests set `enableServiceLinks: false`. Without it the generator refuses the manifest: "Kubelet will inject unpredictable service discovery env vars that break exact env matching." The command is a single line, and both `AWS_REGION` and `AWS_DEFAULT_REGION` are set.

### 7.4 Generate the policy on the cluster

`eks-nitro-sandbox-genpolicy` runs on Linux only, so here it runs in a plain pod on the `m8g.xlarge` node, with `--profile confidential --with-pod-identity --image-architecture arm64`. `--with-pod-identity` is needed because the Pod Identity webhook adds its token volume after the tool has read the manifest.

This is a demo shortcut. In confidential mode the node is the untrusted party, and this pod runs on it and returns its output through the node's kubelet. In production, generate the policy in the CI/CD pipeline that produces your manifests, or on a Linux workstation or container (glibc 2.34 or later), pin a release in the download URL instead of `latest`, and review the policy before you trust its PCR17.

```bash
kubectl create configmap confidential-manifest --from-file=manifest.json=build/pod-confidential.json
kubectl apply -f pod-genpolicy.yaml
until kubectl logs genpolicy 2>/dev/null | grep -q '^POLICY_END$'; do sleep 10; done
kubectl logs genpolicy > build/genpolicy.log
grep -E 'Verified OK|runtime.tar.gz: OK|^eks-nitro|exit=|HARDEN' build/genpolicy.log
awk '/^--- Base64 Encoded Policy Envelope ---/{f=1;next} /^--- Predicted PCR17 ---/{f=0} f' build/genpolicy.log | tr -d '\n ' > build/policy.b64
PCR17=$(grep -o '"kms:RecipientAttestation:PCR17": "[0-9a-f]*"' build/genpolicy.log | grep -o '[0-9a-f]\{96\}')
echo "$PCR17"
kubectl delete pod genpolicy
```

```text
Verified OK
nitro-sandboxes-runtime.tar.gz: OK
eks-nitro-sandbox-genpolicy 0.1.4274.0
exit=0 bytes=28151
// HARDEN: set securityContext.readOnlyRootFilesystem: true to enforce read-only root filesystem in the policy
// HARDEN: set securityContext.allowPrivilegeEscalation: false to enforce no_new_privileges in the policy
// HARDEN: set securityContext.runAsNonRoot: true to enforce a non-root user in the policy
// HARDEN: set securityContext.capabilities.drop: ["ALL"] to restrict capabilities in the policy
5ccb2d706da3b61f6ecbb85799cc24c54fddc841b2b259d0a67f732dc9ace02212cba02e6a3ad441c284eb4b1da3b5da
```

The tool's stdout has five sections: `--- Admission Policy ---` and `--- Runtime Policy ---` as readable Cedar, `--- Base64 Encoded Policy Envelope ---` (the annotation value, about 8,240 characters), `--- Predicted PCR17 ---` and `--- KMS Key Policy Condition ---`. `build/genpolicy.log` keeps all of it; read the Cedar before you trust it. The policy pins the image digest, the command, every environment value (including `SECRET_CT`), the mounts and the pod's hostname, and has no permit for exec. Your PCR17 will differ from the one above, because your ciphertext does.

### 7.5 Pin PCR17 in the key policy

```bash
sed -e "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" -e "s/<PCR17>/$PCR17/" kms-eks-policy.json > build/kms-eks-policy.json
aws kms put-key-policy --key-id "$KEY_ID" --policy-name default --policy file://build/kms-eks-policy.json
```

The `AccountAdmin` statement is fine for the demo but not for production: it lets any IAM principal in the account with a matching identity policy call a plain `kms:Decrypt`. AWS's blueprints use a narrower administration statement and explicit `Deny` statements for requests without a matching attestation.

### 7.6 Attach the policy and run both pods

The policy goes in the `sandbox.amazonaws.com/policy` annotation. On a Deployment or another controller, it belongs on the pod template, not on the controller's own metadata. The annotation is immutable on a running pod, so a change means deleting and recreating the pod.

```bash
jq --rawfile p build/policy.b64 '.metadata.annotations["sandbox.amazonaws.com/policy"] = $p' \
  build/pod-confidential.json > build/pod-confidential-final.json
kubectl apply -f build/pod-confidential-final.json -f build/pod-attest-isolated.json
until kubectl logs attest-confidential 2>/dev/null | grep -q ATTEST_DONE && kubectl logs attest-isolated 2>/dev/null | grep -q ATTEST_DONE; do sleep 10; done
kubectl logs attest-confidential | grep -E 'PCR17|SECRET|AccessDenied'
kubectl logs attest-isolated | grep -E 'PCR17|SECRET|AccessDenied'
kubectl exec attest-isolated -- uname -r
kubectl exec attest-confidential -- uname -r
```

```text
PCR17: 5ccb2d706da3b61f6ecbb85799cc24c54fddc841b2b259d0a67f732dc9ace02212cba02e6a3ad441c284eb4b1da3b5da
SECRET: eks: only a measured pod can read this

PCR17: 8d833c2f8409829a…
aws: [ERROR]: An error occurred (AccessDeniedException) when calling the Decrypt operation: is not authorized to perform ...

6.18.54
... policy denied Agent::Action::"Exec" on container "attest" ...
```

- The confidential pod's PCR17 is the value the generator predicted, and KMS released the secret to it through Pod Identity.
- The isolated pod has an NSM device and attests too, with the same PCR0 (guest image) and PCR8 (signer), but its PCR17 is the built-in permissive policy's (`8d833c2f…`, the same value as an isolated ECS task in part 3), so KMS refuses it.
- `kubectl exec` works into the isolated pod and is denied by the guest's agent in the confidential one.

### 7.7 Change the command, keep the name and the policy

```bash
jq '.spec.containers[0].command[2] |= "echo tampered; " + .' build/pod-confidential-final.json > build/pod-tampered.json
kubectl delete pod attest-confidential --wait=true
kubectl apply -f build/pod-tampered.json
kubectl get pod attest-confidential --no-headers | awk '{print $1, $3}'
kubectl get events --field-selector involvedObject.name=attest-confidential -o custom-columns=M:.message --no-headers \
  | grep -o 'policy denied[^"]*"[A-Za-z]*' | sort -u
```

```text
attest-confidential CrashLoopBackOff
policy denied Agent::Action::\\\"CreateContainer
```

A changed command is refused at `CreateContainer`, the same stage as on ECS, and the pod retries in `CrashLoopBackOff` instead of failing. Renaming the pod as well is caught one step earlier: in the exploration run, a copy named `attest-tampered` sat in `ContainerCreating` with `FailedCreatePodSandBox ... policy denied SetupSandbox`, because the generated `SetupSandbox` permit pins `context.hostname == "attest-confidential"`. Either way the pod never runs, so watch for the event, not for a `Failed` phase.

## Gotchas

- **Kubernetes 1.36 or later.** There is no other cluster setting.
- **The built-in `general-purpose` pool is amd64 only.** Graviton needs your own NodePool, and Auto Mode needs at least one built-in pool (here `system`) to create the `default` NodeClass.
- **No toleration on the RuntimeClasses and no taint on sandbox nodes.** Plain pods share sandbox nodes unless you keep them off.
- **The service account token is mounted in isolated pods.** Turn off automounting if an untrusted image shouldn't have it.
- **`hostNetwork: true` isn't rejected at admission.** The pod retries in `ContainerCreating` with a `cri-proxy` event.
- **A node advertises 5 × vCPUs sandbox slots, and not all of them were always usable.** On an `m8g.xlarge`, 20 advertised and 18 to 20 used.
- **`enableServiceLinks: false`** or the generator refuses the manifest. Its two other suggestions, `--allow-additional-env` and a profile with `pin_env: false`, loosen what the policy pins.
- **`--with-pod-identity`** when generating for a pod that uses Pod Identity.
- **The policy pins the pod's name, command, environment and image digest.** Re-encrypting the secret, editing `nsm.py`, a new image or a rename means a new policy, a new PCR17 and a key policy update.
- **No `kubectl exec` into a confidential pod** with a policy generated without exec permits.
- **`--recipient AttestationDocument=` takes inline base64**, `$(base64 -w0 att.doc)`, not `fileb://`.
- **Failures retry instead of failing.** A tampered confidential pod sits in `CrashLoopBackOff` or `ContainerCreating`; read its events.

## Cost

Nitro Sandboxes and isolated or confidential pods have no additional charge. In us-east-1: the EKS control plane ($0.10 an hour), the `m8g.xlarge` node ($0.17952 an hour on demand) plus its Auto Mode fee ($0.02154 an hour), the NAT gateway ($0.045 an hour plus data) and the KMS key ($1 a month, prorated). Auto Mode removes an empty node; the control plane and the NAT gateway run until `terraform destroy`.

## Cleanup

Delete the pods and the NodePool first, so Auto Mode terminates the node, then the Pod Identity association, the role, the key and the infrastructure:

```bash
kubectl delete deployment density --ignore-not-found
kubectl delete pods --all --wait=false
kubectl delete configmap confidential-manifest --ignore-not-found
kubectl delete -f sa.yaml --ignore-not-found
kubectl delete -f nodepool.yaml
kubectl get nodes    # wait until the m8g.xlarge node is gone

ASSOC=$(aws eks list-pod-identity-associations --cluster-name $CLUSTER --namespace default --service-account attest \
  --query 'associations[0].associationId' --output text)
aws eks delete-pod-identity-association --cluster-name $CLUSTER --association-id "$ASSOC"
aws iam delete-role --role-name my2cents-nitro-eks-attest

# schedule-key-deletion takes the key ID, not an alias; 7 days is the shortest window
aws kms schedule-key-deletion --key-id "$(cat build/key-id)" --pending-window-in-days 7 --query KeyState --output text

terraform -chdir=terraform destroy
rm -rf build
```

```text
PendingDeletion
...
Destroy complete! Resources: 54 destroyed.
```
