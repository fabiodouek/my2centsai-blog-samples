# AWS Nitro Sandboxes on Amazon ECS

Part 3 of the AWS Nitro Sandboxes series. Amazon ECS Managed Instances runs each task in its own Nitro Sandbox when the capacity provider has Nitro Enclaves turned on and the task definition sets an isolation mode. Terraform creates the dependencies (VPC, NAT gateway, IAM roles, a log group); the cluster, the capacity provider and the task definitions are made in the console, with the AWS CLI equivalent under each step. Then: an isolated and a shared task side by side on the same instance, what the API refuses, the memory overhead per sandbox size, and a confidential task that gets a KMS secret only while its attestation policy matches.

Blog post: [AWS Nitro Sandboxes on Amazon ECS](https://my2cents.ai/deep-dive/nitro-sandboxes-ecs)

Video walkthrough: https://youtu.be/BfcSVs7J2vs

## Files

| File | What it is |
|---|---|
| `terraform/` | VPC `10.62.0.0/16` (two private and two public subnets, one NAT gateway), an egress-only security group, the Managed Instances infrastructure role, the instance role and profile, the task execution role, the task role, and the log group `/ecs/my2cents-nitro-sandbox` |
| `capacity-provider.json` | CLI input for the capacity provider: `m8g.xlarge` only, 30 GiB, local storage off, `nitroEnclavesEnabled: true` |
| `td-isolated.json`, `td-shared.json` | The probe task (`probe.sh`) as `ISOLATED_SANDBOX` and as an ordinary shared task |
| `td-confidential.json` | The attest task (`attest.sh`) as `CONFIDENTIAL_SANDBOX`, without its attestation policy: the input for policy generation |
| `td-attest-isolated.json` | The same container as `ISOLATED_SANDBOX` |
| `td-tampered.json` | The confidential task with `echo tampered; ` in front of its command, carrying the original policy |
| `td-genpolicy.json` | A shared task that downloads, verifies and runs `ecs-nitro-sandbox-genpolicy` on the cluster (`genpolicy.sh`) |
| `kms-ecs-initial.json`, `kms-ecs-final.json` | The key policy before and after the policy measurement (PCR17) is known |
| `probe.sh`, `attest.sh`, `genpolicy.sh` | Readable copies of the commands embedded in the task definitions, which are the source of truth |

The templates carry placeholders: `<ACCOUNT_ID>`, `<NSM_PY_B64>`, `<SECRET_CT>`, `<TD_B64>`, `<ATTESTATION_POLICY>` and `<PCR17>`. The steps fill them in with `sed` and `jq` and write the results to `build/` (gitignored). If you paste a template into the console's JSON editor instead, replace them by hand.

## Before you start

You need the tools in the [series prerequisites](../README.md#prerequisites), including AWS CLI 2.39.7 or later: older versions don't know `trustedExecutionConfiguration` or `nitroEnclavesEnabled`. Run everything from this directory, in `bash`:

```bash
cd aws-nitro-sandboxes/ecs && mkdir -p build
export AWS_REGION=us-east-1
CLUSTER=my2cents-nitro-ecs
CP=my2cents-nitro-cp
LOG_GROUP=/ecs/my2cents-nitro-sandbox
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

## Step 1: Dependencies with Terraform

```bash
terraform -chdir=terraform init
terraform -chdir=terraform apply
NET=$(terraform -chdir=terraform output -raw run_task_network_configuration)
```

`NET` is the `--network-configuration` value for `run-task`: the two private subnets and the task security group. The task role has no KMS permission on purpose; Step 8 grants `kms:Decrypt` in the key policy, with attestation conditions, and nowhere else.

## Step 2: The cluster

**Console:** Amazon ECS > Clusters > **Create cluster**, name `my2cents-nitro-ecs`. The wizard has no Nitro or sandbox control anywhere, including under "Fargate and Managed Instances" and "Use custom - advanced", so create it as **Fargate only** and add the capacity provider in Step 3.

**CLI equivalent:**

```bash
aws ecs create-cluster --cluster-name $CLUSTER
```

## Step 3: A capacity provider with Nitro Enclaves

**Console:** Clusters > `my2cents-nitro-ecs` > **Infrastructure** > Capacity providers > **Create**.

1. Name `my2cents-nitro-cp`.
2. **Infrastructure role** `ecsInfrastructureRole-my2cents` and **Instance profile** `ecsInstanceRole-my2cents`. Pick both from the dropdown list; typing the name gives "Value is not a valid profile ARN".
3. Subnets: the two private subnets of the `my2cents-nitro-ecs` VPC (`terraform -chdir=terraform output private_subnets`). Security group `my2cents-nitro-ecs-tasks`.
4. Instance selection: **Use custom - advanced**, CPU 4 to 4, Memory 16384 to 16384, and the attribute **Allowed instance types** = `m8g.xlarge`. The console shows "View matching instance types (1)".
5. Storage 30 GiB.
6. **Advanced configuration**: first **uncheck "Use local storage"**. It is checked by default, and while it is, the checkbox below is disabled ("Nitro Enclaves is not supported with local storage"). Then, under "Nitro Enclaves - new: Launch instances with Nitro Enclaves so that tasks can run in an isolated sandbox", check **Turn on Nitro Enclaves**. "GPU auto repair" is also checked by default; it doesn't matter on `m8g`.

**CLI equivalent:** `nitroEnclavesEnabled: true` in the instance launch template, with `useLocalStorage: false` (the two can't be combined):

```bash
jq --arg infra "$(terraform -chdir=terraform output -raw infrastructure_role_arn)" \
   --arg profile "$(terraform -chdir=terraform output -raw instance_profile_arn)" \
   --argjson subnets "$(terraform -chdir=terraform output -json private_subnets)" \
   --arg sg "$(terraform -chdir=terraform output -raw security_group)" \
   '.managedInstancesProvider.infrastructureRoleArn = $infra
    | .managedInstancesProvider.instanceLaunchTemplate.ec2InstanceProfileArn = $profile
    | .managedInstancesProvider.instanceLaunchTemplate.networkConfiguration = {subnets: $subnets, securityGroups: [$sg]}' \
   capacity-provider.json > build/capacity-provider.json
aws ecs create-capacity-provider --cli-input-json file://build/capacity-provider.json
```

Check it either way:

```bash
aws ecs describe-capacity-providers --capacity-providers $CP \
  --query 'capacityProviders[0].{updateStatus: updateStatus, nitro: managedInstancesProvider.instanceLaunchTemplate.nitroEnclavesEnabled, types: managedInstancesProvider.instanceLaunchTemplate.instanceRequirements.allowedInstanceTypes, storage: managedInstancesProvider.instanceLaunchTemplate.storageConfiguration.storageSizeGiB}'
```

```json
{
    "updateStatus": "CREATE_COMPLETE",
    "nitro": true,
    "types": [
        "m8g.xlarge"
    ],
    "storage": 30
}
```

The capacity provider has no instance cap. When a fifth 1-vCPU task arrived while the first instance's 4,096 CPU units were taken, ECS launched a second `m8g.xlarge`. Keep the demo small; both instances scaled in once idle.

## Step 4: Task definitions with an isolation mode

**Console:** Task definitions > **Create new task definition**. With Managed Instances as the only launch type, the form shows "Isolation mode - new: Choose how strongly this task is isolated from other tasks on the same instance". Check **Use task isolation** and pick one of three modes:

- **Shared**: "The task shares the instance with other tasks, using standard container isolation."
- **Isolated sandbox**: "The task is isolated from other tasks and from the instance itself, with dedicated memory."
- **Confidential sandbox**: "The task runs in an isolated sandbox with cryptographic attestation, so it can prove what it is running."

The section only appears when Managed Instances is the only launch type. **Create new task definition with JSON** accepts `trustedExecutionConfiguration` too; the recorded run registered `my2cents-isolated` and `my2cents-shared` by pasting `td-isolated.json` and `td-shared.json` there, with `<ACCOUNT_ID>` replaced.

**CLI equivalent:** the field is `trustedExecutionConfiguration`. `td-isolated.json` sets `{"isolationMode": "ISOLATED_SANDBOX"}`; `td-shared.json` leaves it out, which is an ordinary shared task.

```bash
for td in td-isolated td-shared; do
  sed "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" $td.json > build/$td.json
  aws ecs register-task-definition --cli-input-json file://build/$td.json --query 'taskDefinition.[family, revision]' --output text
done
```

Both run the same probe ([`probe.sh`](probe.sh)): kernel, `boot_id`, uptime, CPUs, memory, disks, PCI devices, `/dev/nsm`, network devices, the metadata service, the task role's credentials, the task metadata endpoint, capabilities, PID 1, and egress.

## Step 5: An isolated and a shared task on the same instance

**Console:** Run new task. In the capacity provider picker, FARGATE and FARGATE_SPOT say "Sandbox isolation requires a Managed Instances capacity provider"; pick `my2cents-nitro-cp`, labelled "Nitro Enclaves · Managed instances". Use the private subnets and the `my2cents-nitro-ecs-tasks` group, and under Troubleshooting, **Turn on ECS Exec**.

**CLI equivalent:**

```bash
ISO=$(aws ecs run-task --cluster $CLUSTER --task-definition my2cents-isolated \
  --capacity-provider-strategy capacityProvider=$CP,weight=1 \
  --network-configuration "$NET" --enable-execute-command \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-running --cluster $CLUSTER --tasks $ISO
```

The rest of this README uses the same command through a small function:

```bash
run_task() {  # run_task <family> [more run-task options]: prints the task ARN
  aws ecs run-task --cluster $CLUSTER --task-definition "$1" \
    --capacity-provider-strategy capacityProvider=$CP,weight=1 \
    --network-configuration "$NET" "${@:2}" \
    --query 'tasks[0].taskArn' --output text
}
SHR=$(run_task my2cents-shared --enable-execute-command)
aws ecs wait tasks-running --cluster $CLUSTER --tasks $SHR
```

The first task also launches the instance. Its timeline, from `describe-tasks`:

```bash
aws ecs describe-tasks --cluster $CLUSTER --tasks $ISO \
  --query 'tasks[0].{created: createdAt, pullStarted: pullStartedAt, pullStopped: pullStoppedAt, started: startedAt, isolation: trustedExecutionSummary, instance: containerInstanceArn}'
```

| Event | Time (UTC) |
|---|---|
| Run task clicked | 23:28:55.2 |
| `createdAt` | 23:28:56.0 |
| Container instance registered | 23:28:59.9 |
| PROVISIONING to PENDING | about 23:29:21 |
| `pullStartedAt` to `pullStoppedAt` | 23:29:22.7 to 23:29:24.4 (1.6 s) |
| `startedAt` | 23:29:30.1 |

About 34 seconds from `createdAt` to RUNNING, including launching the `m8g.xlarge`; 5.75 seconds from the end of the pull to the container start (sandbox boot plus start). The shared task on the warm instance took 7.1 seconds, with the image cached.

Both tasks should report the same `containerInstanceArn`. The instance:

```bash
CI=$(aws ecs describe-tasks --cluster $CLUSTER --tasks $ISO --query 'tasks[0].containerInstanceArn' --output text)
IID=$(aws ecs describe-container-instances --cluster $CLUSTER --container-instances $CI --query 'containerInstances[0].ec2InstanceId' --output text)
aws ec2 describe-instances --instance-ids $IID \
  --query 'Reservations[0].Instances[0].{type: InstanceType, enclaves: EnclaveOptions.Enabled, tokens: MetadataOptions.HttpTokens, hopLimit: MetadataOptions.HttpPutResponseHopLimit}'
aws ecs describe-container-instances --cluster $CLUSTER --container-instances $CI \
  --query 'containerInstances[0].{nitro: attributes[?name==`ecs.capability.nitro-enclaves`].name | [0], memoryMiB: registeredResources[?name==`MEMORY`].integerValue | [0]}'
```

An `m8g.xlarge` with Nitro Enclaves enabled, IMDSv2 required with a hop limit of 1, the container instance attribute `ecs.capability.nitro-enclaves`, and 13,347 MiB of registered memory out of 16,384. The AMI is the Bottlerocket-based ECS Managed Instances AMI.

Read the two probes from CloudWatch Logs (the stream is `<prefix>/<container>/<task-id>`):

```bash
aws logs tail $LOG_GROUP --log-stream-names my2cents-isolated/probe/${ISO##*/} --since 1h --format short
aws logs tail $LOG_GROUP --log-stream-names my2cents-shared/probe/${SHR##*/} --since 1h --format short
```

| | `ISOLATED_SANDBOX` (1 vCPU, 2 GiB) | Shared (1 vCPU, 2 GiB) |
|---|---|---|
| `trustedExecutionSummary` | `{"isolationMode": "ISOLATED_SANDBOX"}` | omitted |
| Kernel | `6.18.56 #1 SMP Tue Oct 27 17:44:05 UTC 2026` | `6.18.56 #2 SMP Fri Oct 30 22:32:43 UTC 2026` (the instance's) |
| `boot_id` | its own | the instance's |
| Uptime at probe | 6.11 s | 47.40 s (the instance's) |
| `nproc` | **1** | **4** |
| MemTotal | **2,319,336 kB** | **16,048,204 kB** |
| Disks | `dm-0 vda vdb vdc` | `dm-0 loop0-7 nvme0n1 nvme1n1` |
| `/` | overlay, 20G (fixed) | overlay, 30G (instance storage) |
| PCI | Amazon bridges, `1af4:1061` (NSM), three `1af4:1042` (block), `1af4:1041` (net), `1af4:1053` (vsock) | Amazon bridges, `1d0f:e4c1`, `1d0f:8061` (NVMe), `1d0f:ec20` (ENA), `1af4:1053` |
| `/dev/nsm` | **present** (`crw-rw-rw-`) | absent |
| Network devices | `lo dummy0 eth1` | `lo eth0 eth1` |
| Metadata service | routed via `eth1`, connections time out | blocked: immediate connect failure |
| Task role credentials (`169.254.170.2`) | work | work |
| Task metadata v4 | works | works |
| `CapEff` | `00000000a80425fb` (Docker default) | same |
| Egress through the NAT gateway | HTTP 200 | HTTP 200 |

The NSM is present in an isolated task. The Firecracker docs say direct mode has no NSM, so ECS isolated mode runs on the EC2 confidential memory mode underneath; what separates isolated from confidential on ECS is the attestation policy (Step 8), not memory protection.

The ECS docs say isolated tasks get an IMDSv2-only surrogate with region, Availability Zone and endpoint paths. In the recorded run nothing answered on `169.254.169.254` from the isolated task. Look for yourself with ECS Exec (it needs the session-manager-plugin):

```bash
aws ecs execute-command --cluster $CLUSTER --task $ISO --container probe --interactive --command "/bin/bash"
```

```bash
# inside the task
curl -sv -m5 -X PUT http://169.254.169.254/latest/api/token -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'
```

Stop both tasks. Stopping takes about a minute: `sleep` ignores SIGTERM, so ECS waits out the 30-second stop timeout.

```bash
aws ecs stop-task --cluster $CLUSTER --task $ISO --query task.lastStatus --output text
aws ecs stop-task --cluster $CLUSTER --task $SHR --query task.lastStatus --output text
aws ecs wait tasks-stopped --cluster $CLUSTER --tasks $ISO $SHR
```

## Step 6: What the API refuses, and what it doesn't

Variants of the isolated task definition:

```bash
# host network mode
jq '.family = "my2cents-neg-host" | .networkMode = "host"' build/td-isolated.json > build/neg-host.json
aws ecs register-task-definition --cli-input-json file://build/neg-host.json

# Fargate
jq '.family = "my2cents-neg-fargate" | .requiresCompatibilities = ["FARGATE"]' build/td-isolated.json > build/neg-fargate.json
aws ecs register-task-definition --cli-input-json file://build/neg-fargate.json

# privileged container with the host PID namespace
jq '.family = "my2cents-neg-priv" | .pidMode = "host" | .containerDefinitions[0].privileged = true
    | .containerDefinitions[0].logConfiguration.options["awslogs-stream-prefix"] = "my2cents-neg-priv"' \
  build/td-isolated.json > build/neg-priv.json
aws ecs register-task-definition --cli-input-json file://build/neg-priv.json --query taskDefinition.revision --output text
PRIV=$(run_task my2cents-neg-priv)
aws ecs wait tasks-running --cluster $CLUSTER --tasks $PRIV
sleep 20
aws logs tail $LOG_GROUP --log-stream-names my2cents-neg-priv/probe/${PRIV##*/} --since 1h --format short | grep -E 'kernel|caps|pid1|procs'
aws ecs stop-task --cluster $CLUSTER --task $PRIV --query task.lastStatus --output text

# pin the task to an instance (IID from Step 5)
run_task my2cents-isolated --placement-constraints "type=memberOf,expression=ec2InstanceId == $IID"
```

| Test | Result |
|---|---|
| `networkMode: host` | `RegisterTaskDefinition`: "Tasks using the ISOLATED_SANDBOX isolation mode require networkMode 'awsvpc'." |
| `requiresCompatibilities: [FARGATE]` | "trustedExecutionConfiguration with isolationMode ISOLATED_SANDBOX requires the requiresCompatibilities field to contain only MANAGED_INSTANCES" |
| `pidMode: host` plus `privileged: true` | **Accepted, and it runs.** PID 1 is `systemd`, 174 processes are visible, `CapEff` is `000001ffffffffff` (all), and the kernel is the guest's `#1 SMP Tue Oct 27`. "Host" means the sandbox VM, not the EC2 instance. |
| Placement constraint on `ec2InstanceId` | "not applicable for MANAGED_INSTANCES. Subject of expression must be one of [attribute:ecs.subnet-id, attribute:ecs.vpc-id, attribute:ecs.availability-zone, attribute:ecs.cpu-architecture, attribute:ecs.instance-type]" |

## Step 7: Memory overhead per sandbox size

ECS reserves memory around each sandbox for its kernel, the microVM and the sandbox agent. Run one task at a time on an otherwise idle instance, and compare the container instance's registered and remaining memory:

```bash
EXEC_ROLE=arn:aws:iam::$ACCOUNT_ID:role/my2cents-nitro-ecs-execution
for spec in SHARED:2048 ISOLATED_SANDBOX:512 ISOLATED_SANDBOX:1024 ISOLATED_SANDBOX:2048 ISOLATED_SANDBOX:4096 ISOLATED_SANDBOX:8192; do
  MODE=${spec%%:*}; MEM=${spec##*:}
  FAMILY=my2cents-mem-$(echo $MODE | tr 'A-Z_' 'a-z-')-$MEM
  jq -n --arg fam $FAMILY --arg mem $MEM --arg mode $MODE --arg exec $EXEC_ROLE '{
      family: $fam, networkMode: "awsvpc", requiresCompatibilities: ["MANAGED_INSTANCES"],
      runtimePlatform: {cpuArchitecture: "ARM64", operatingSystemFamily: "LINUX"},
      cpu: "1024", memory: $mem, executionRoleArn: $exec,
      containerDefinitions: [{name: "app", image: "public.ecr.aws/amazonlinux/amazonlinux:2023", essential: true, command: ["sleep", "600"]}]
    } + (if $mode == "SHARED" then {} else {trustedExecutionConfiguration: {isolationMode: $mode}} end)' > build/$FAMILY.json
  aws ecs register-task-definition --cli-input-json file://build/$FAMILY.json --query taskDefinition.revision --output text >/dev/null
  T=$(run_task $FAMILY)
  aws ecs wait tasks-running --cluster $CLUSTER --tasks $T
  CI=$(aws ecs describe-tasks --cluster $CLUSTER --tasks $T --query 'tasks[0].containerInstanceArn' --output text)
  read -r REG REM <<< "$(aws ecs describe-container-instances --cluster $CLUSTER --container-instances $CI \
    --query 'containerInstances[0].[registeredResources[?name==`MEMORY`].integerValue | [0], remainingResources[?name==`MEMORY`].integerValue | [0]]' --output text)"
  echo "$MODE $MEM MiB: footprint $((REG - REM)) MiB, overhead $((REG - REM - MEM)) MiB"
  aws ecs stop-task --cluster $CLUSTER --task $T --query task.lastStatus --output text >/dev/null
  aws ecs wait tasks-stopped --cluster $CLUSTER --tasks $T
done
```

| Task memory | Footprint on the instance | Overhead | Overhead % |
|---|---|---|---|
| Shared 2048 MiB | 2048 | 0 | 0% |
| Isolated 512 MiB | 774 | 262 | 51% |
| Isolated 1024 MiB | 1286 | 262 | 26% |
| Isolated 2048 MiB | 2416 | 368 | 18% |
| Isolated 4096 MiB | 4511 | 415 | 10% |
| Isolated 8192 MiB | 8701 | 509 | 6% |

The 8 GiB row matches the only example in the docs (about 8.7 GiB for a task with 8 GiB). Below that there's a floor of about 262 MiB, so small sandboxes pay proportionally the most. The same 2048 + 368 appeared with a shared and an isolated task running together.

## Step 8: A confidential task, end to end

A confidential task generates an attestation document from its NSM, and the task role asks KMS to decrypt a secret for it. The key policy only allows that when the document's PCR17 (the measurement of the task's attestation policy) and PCR8 (the service that signed the guest image) match. The attestation policy is Cedar, generated from the task definition: it pins the image digest, the command, the environment values and the security settings, and denies ECS Exec.

### 8.1 A KMS key and the secret

The key starts with an admin-only policy:

```bash
sed "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" kms-ecs-initial.json > build/kms-ecs-initial.json
KEY_ID=$(aws kms create-key --description "Nitro Sandboxes on ECS demo" \
  --policy file://build/kms-ecs-initial.json --query KeyMetadata.KeyId --output text)
aws kms create-alias --alias-name alias/my2cents-nitro-ecs --target-key-id "$KEY_ID"
printf '%s' 'ecs: only a measured task can read this' > build/secret.txt
SECRET_CT=$(aws kms encrypt --key-id "$KEY_ID" --plaintext fileb://build/secret.txt --query CiphertextBlob --output text)
```

### 8.2 The task definition, without its policy

The container (`amazonlinux:2023`) installs `openssl`, `awscli-2` and `python3`, writes [`nsm.py`](../nsm/nsm.py) from `NSM_PY_B64`, makes an RSA key pair, gets an attestation document, prints PCR0, PCR8 and PCR17, then calls `aws kms decrypt --recipient` with the task role and opens the answer with `openssl cms` ([`attest.sh`](attest.sh)). The command is a single line because `ecs-nitro-sandbox-genpolicy` rejects process arguments with a newline in them.

Fill in the account, the NSM client and the ciphertext. The policy pins the environment values, so they have to be final before the policy is generated:

```bash
NSM_PY_B64=$(base64 < ../nsm/nsm.py | tr -d '\n')
FILL='.containerDefinitions[0].environment |= map(if .name == "NSM_PY_B64" then .value = $nsm elif .name == "SECRET_CT" then .value = $ct else . end)'
sed "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" td-confidential.json \
  | jq --arg nsm "$NSM_PY_B64" --arg ct "$SECRET_CT" "$FILL" > build/td-confidential.json
```

### 8.3 Generate the attestation policy

`ecs-nitro-sandbox-genpolicy` ships in AWS's signed Nitro Sandboxes runtime release, for Linux only (x86_64 and aarch64, glibc 2.34 or later). The recorded run generated the policy on the cluster itself, in a shared task, which works from a Mac. The task downloads the release, verifies the signature over `SHA256SUM` with the public key from the ECS docs and the archive against `SHA256SUM`, runs the tool on the task definition (passed base64 in `TD_B64`), and prints the policy and the PCR17 prediction to its log ([`genpolicy.sh`](genpolicy.sh)).

```bash
TD_B64=$(base64 < build/td-confidential.json | tr -d '\n')
sed "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" td-genpolicy.json \
  | jq --arg td "$TD_B64" '.containerDefinitions[0].environment |= map(if .name == "TD_B64" then .value = $td else . end)' \
  > build/td-genpolicy.json
aws ecs register-task-definition --cli-input-json file://build/td-genpolicy.json --query taskDefinition.revision --output text
GP=$(run_task my2cents-genpolicy)
STREAM=my2cents-genpolicy/genpolicy/${GP##*/}
until aws logs get-log-events --log-group-name $LOG_GROUP --log-stream-name $STREAM \
        --query 'events[].message' --output text 2>/dev/null | grep -q RUNNER_DONE; do sleep 10; done
aws logs get-log-events --log-group-name $LOG_GROUP --log-stream-name $STREAM --start-from-head \
  --query 'events[].message' --output json > build/genpolicy-log.json
jq -r '.[]' build/genpolicy-log.json | sed '/^POLICY_BEGIN$/,/^POLICY_END$/d'
aws ecs stop-task --cluster $CLUSTER --task $GP --query task.lastStatus --output text
```

```text
Verified OK
nitro-sandboxes-runtime.tar.gz: OK
genpolicy: /tmp/ecs-nitro-sandbox-genpolicy
ecs-nitro-sandbox-genpolicy 0.1.4274.0
exit=0 bytes=8853
info: fetching image configs to resolve image-derived fields (argv, working dir, user); pulling layers for /etc/passwd
info: fetching image configs to resolve image-derived fields (argv, working dir, user); pulling layers for /etc/passwd
info: public.ecr.aws/amazonlinux/amazonlinux:2023 (arm64): cache hit for sha256:5e98730d7319
--- KMS Key Policy Condition ---
"Condition": {
  "StringEqualsIgnoreCase": {
    "kms:RecipientAttestation:PCR17": "9591b3121c466f31d68bbd796cb28c0eaa7d84a138597e97ec0cd01fffcc350d356b0454a0afb32e7747c8748651c817",
    "kms:RecipientAttestation:PCR8:ServiceName": "ecs.amazonaws.com"
  }
}
RUNNER_DONE
```

Take the policy (between `POLICY_BEGIN` and `POLICY_END`) and PCR17 out of the log:

```bash
jq -r '.[]' build/genpolicy-log.json | sed -n '/^POLICY_BEGIN$/,/^POLICY_END$/p' | grep -v '^POLICY_' | tr -d '\n' > build/policy.b64
PCR17=$(jq -r '.[]' build/genpolicy-log.json | grep -o '"kms:RecipientAttestation:PCR17": "[0-9a-f]*"' | cut -d'"' -f4)
echo "policy: $(wc -c < build/policy.b64 | tr -d ' ') base64 chars, PCR17: $PCR17"
base64 -d < build/policy.b64 | gunzip | jq -r 'keys[]'
```

```text
policy: 8852 base64 chars, PCR17: 9591b3121c466f31d68bbd796cb28c0eaa7d84a138597e97ec0cd01fffcc350d356b0454a0afb32e7747c8748651c817
admission_policy
runtime_policy
version
```

Your PCR17 will differ: the policy covers your ciphertext and the image digest of the day. The policy is gzip-compressed JSON holding two Cedar policies. Read them with `base64 -d < build/policy.b64 | gunzip | jq -r .admission_policy` (and `.runtime_policy`):

- **Admission** gates `CreateContainer`, `PrepareImage` and `MountHostImage`, deny by default. The permit for container `attest` requires `image_ref == "public.ecr.aws/amazonlinux/amazonlinux@sha256:…"`, the exact `process_args`, the environment values, user, capabilities and more.
- **Runtime** permits logs, stats, metrics, OOM events and the metadata surrogate, and forbids `DebugExec`, `WriteStdin`, `SetupVolume`, `StreamEfsCerts` and `GetBootTimings`. ECS Exec is denied unless you generate the policy with `--allow-exec-command`.

On a Linux machine you can run the tool directly instead (this is the path the ECS docs describe; the recorded run didn't use it):

```bash
cd build
U=https://d1uvmbvklg8rt.cloudfront.net/latest/$(uname -m)
curl -fsSLO $U/nitro-sandboxes-runtime.tar.gz && curl -fsSLO $U/SHA256SUM && curl -fsSLO $U/SHA256SUM.sig
jq -r '.containerDefinitions[0].environment[] | select(.name == "PUBKEY") | .value' ../td-genpolicy.json > nitro-sandboxes-runtime-v1.pub
openssl dgst -sha384 -verify nitro-sandboxes-runtime-v1.pub -signature SHA256SUM.sig SHA256SUM
sha256sum -c SHA256SUM --ignore-missing && tar -xzf nitro-sandboxes-runtime.tar.gz
./ecs-nitro-sandbox-genpolicy --image-architecture arm64 --task-definition td-confidential.json | tr -d '\n' > policy.b64
./ecs-nitro-sandbox-genpolicy --image-architecture arm64 --task-definition td-confidential.json --output pcrs
cd ..
```

### 8.4 Pin PCR17 in the key policy

The task role gets `kms:Decrypt` only when PCR17 matches and the guest image was signed by ECS (`PCR8:ServiceName`). This is the only place the role is allowed to decrypt.

```bash
sed -e "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" -e "s/<PCR17>/$PCR17/" kms-ecs-final.json > build/kms-ecs-final.json
aws kms put-key-policy --key-id "$KEY_ID" --policy-name default --policy file://build/kms-ecs-final.json
```

### 8.5 Register the confidential task definition

Splice the policy into `trustedExecutionConfiguration.attestationPolicy`:

```bash
jq --rawfile p build/policy.b64 '.trustedExecutionConfiguration.attestationPolicy = $p' \
  build/td-confidential.json > build/td-confidential-final.json
aws ecs register-task-definition --cli-input-json file://build/td-confidential-final.json --query taskDefinition.revision --output text
```

**Console:** Create new task definition with JSON, and paste `build/td-confidential-final.json`. That is how the recorded run registered `my2cents-confidential:1`.

### 8.6 Run it, then the same container isolated, then a tampered command

Don't pass `--enable-execute-command` to a confidential task: the policy doesn't permit the ECS Exec agent, and the task is denied at startup.

```bash
CONF=$(run_task my2cents-confidential)
aws ecs wait tasks-running --cluster $CLUSTER --tasks $CONF
aws ecs describe-tasks --cluster $CLUSTER --tasks $CONF --query 'tasks[0].trustedExecutionSummary'
until aws logs tail $LOG_GROUP --log-stream-names my2cents-confidential/attest/${CONF##*/} --since 1h --format short | grep -q ATTEST_DONE; do sleep 10; done
aws logs tail $LOG_GROUP --log-stream-names my2cents-confidential/attest/${CONF##*/} --since 1h --format short

# The same container as ISOLATED_SANDBOX, no policy
sed "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" td-attest-isolated.json \
  | jq --arg nsm "$NSM_PY_B64" --arg ct "$SECRET_CT" "$FILL" > build/td-attest-isolated.json
aws ecs register-task-definition --cli-input-json file://build/td-attest-isolated.json --query taskDefinition.revision --output text
ISOA=$(run_task my2cents-attest-isolated)
aws ecs wait tasks-running --cluster $CLUSTER --tasks $ISOA
until aws logs tail $LOG_GROUP --log-stream-names my2cents-attest-isolated/attest/${ISOA##*/} --since 1h --format short | grep -q ATTEST_DONE; do sleep 10; done
aws logs tail $LOG_GROUP --log-stream-names my2cents-attest-isolated/attest/${ISOA##*/} --since 1h --format short

# The confidential task with "echo tampered; " in front of its command, and the original policy
sed "s/<ACCOUNT_ID>/$ACCOUNT_ID/g" td-tampered.json \
  | jq --arg nsm "$NSM_PY_B64" --arg ct "$SECRET_CT" --rawfile p build/policy.b64 \
      "$FILL | .trustedExecutionConfiguration.attestationPolicy = \$p" > build/td-tampered.json
aws ecs register-task-definition --cli-input-json file://build/td-tampered.json --query taskDefinition.revision --output text
TAMP=$(run_task my2cents-confidential-tampered)
aws ecs wait tasks-stopped --cluster $CLUSTER --tasks $TAMP
aws ecs describe-tasks --cluster $CLUSTER --tasks $TAMP --query 'tasks[0].{status: lastStatus, stopCode: stopCode, reason: stoppedReason}'
```

All three ran on the same `m8g.xlarge`:

| | Confidential, with its policy | Same container, isolated | Confidential, command changed after policy generation |
|---|---|---|---|
| `trustedExecutionSummary` | `{"isolationMode": "CONFIDENTIAL_SANDBOX", "attestationPolicyStatus": "CONFIGURED"}` | `{"isolationMode": "ISOLATED_SANDBOX"}` | `CONFIDENTIAL_SANDBOX`, `CONFIGURED` |
| `/dev/nsm` | present | **present** | n/a |
| Attestation document | 4,810 bytes | 4,808 bytes | n/a |
| PCR0 (guest image) | `2e3aa034…aad4b` | **same** | n/a |
| PCR8 (image signer) | `336ea928…7b3fcb` | **same** | n/a |
| PCR17 (policy) | `9591b312…51c817`, the genpolicy prediction | `8d833c2f…aa08dc` | n/a |
| KMS decrypt | **`SECRET: ecs: only a measured task can read this`** | `AccessDeniedException ... not authorized to perform: kms:Decrypt` | n/a |
| Task outcome | RUNNING | RUNNING | **STOPPED**, `TaskFailedToStart`: `CannotStartContainerError: ResourceInitializationError: ... create_container: rpc status: Status { code: PERMISSION_DENIED, message: "policy denied Agent::Action::\"CreateContainer\" ...` |

Isolated tasks can attest too: they have an NSM, the same guest image (PCR0) and the same signer (PCR8). Only PCR17 differs. The confidential guarantee comes from pinning PCR17 in the key policy, which only means something in confidential mode, where the same policy also locks admission and denies exec. A key policy that checks only PCR0 or PCR8 would hand the secret to any isolated task with the role.

The `module_id` in an ECS sandbox's document starts with the instance ID of the parent.

Stop the two running tasks:

```bash
aws ecs stop-task --cluster $CLUSTER --task $CONF --query task.lastStatus --output text
aws ecs stop-task --cluster $CLUSTER --task $ISOA --query task.lastStatus --output text
```

## Gotchas

- **AWS CLI 2.39.7 or later** for `trustedExecutionConfiguration` and `nitroEnclavesEnabled`. On launch day Homebrew still had 2.39.6; AWS's own installer package worked.
- **The cluster wizard has no Nitro setting.** It lives on the capacity provider.
- **"Use local storage" is checked by default** in the capacity provider form and disables "Turn on Nitro Enclaves". Uncheck it first. The API refuses the combination too.
- **Isolation needs Managed Instances only and `awsvpc`.** `host` network mode and Fargate compatibility are rejected at registration.
- **`ecs-nitro-sandbox-genpolicy` rejects newlines in process arguments** (`error: process argument "..." contains an illegal control character '\n'`). Keep the confidential command on one line.
- **The `amazonlinux:2023` image has no `find`.** `genpolicy.sh` installs `findutils`.
- **The policy pins the environment.** Re-encrypting the secret, editing `nsm.py` or a new image digest means a new policy and a new PCR17 in the key policy.
- **`--recipient AttestationDocument=` takes inline base64**, `$(base64 -w0 att.doc)`, not `fileb://`.
- **No ECS Exec in a confidential task** unless the policy was generated with `--allow-exec-command`.
- **No instance cap on the capacity provider.** A task that doesn't fit launches another `m8g.xlarge`.
- **Don't count on the metadata surrogate** in isolated tasks: in the recorded run nothing answered on `169.254.169.254`.

## Cost

Nitro Sandboxes and isolated or confidential tasks have no additional charge. You pay for the `m8g.xlarge` instances ($0.17952 an hour each on demand in us-east-1) plus the ECS Managed Instances management fee, the NAT gateway ($0.045 an hour plus data), the KMS key ($1 a month, prorated) and CloudWatch Logs. Managed Instances scale in once idle; the NAT gateway runs until `terraform destroy`.

## Cleanup

Stop every task, delete the capacity provider (its instances terminate with it), delete the cluster, schedule the key for deletion, then destroy the dependencies:

```bash
TASKS=$(aws ecs list-tasks --cluster $CLUSTER --query 'taskArns[]' --output text)
if [ -n "$TASKS" ]; then
  for t in $TASKS; do aws ecs stop-task --cluster $CLUSTER --task $t --query task.lastStatus --output text; done
  aws ecs wait tasks-stopped --cluster $CLUSTER --tasks $TASKS
fi

aws ecs delete-capacity-provider --cluster $CLUSTER --capacity-provider $CP --query capacityProvider.updateStatus --output text
aws ecs describe-capacity-providers --capacity-providers $CP \
  --query 'capacityProviders[0].[status, updateStatus, updateStatusReason]' --output text
```

If `updateStatus` is `DELETE_FAILED` because tasks were still stopping, wait a minute and run `delete-capacity-provider` again. Then wait until the managed instances are gone (the command prints nothing), and finish. Use `list-container-instances` here: Managed Instances don't show up in `aws ec2 describe-instances` listings filtered by VPC.

```bash
aws ecs list-container-instances --cluster $CLUSTER --query 'containerInstanceArns[]' --output text
aws ecs delete-cluster --cluster $CLUSTER --query cluster.status --output text

# schedule-key-deletion takes the key ID, not the alias
KEY_ID=$(aws kms describe-key --key-id alias/my2cents-nitro-ecs --query KeyMetadata.KeyId --output text)
aws kms delete-alias --alias-name alias/my2cents-nitro-ecs
aws kms schedule-key-deletion --key-id "$KEY_ID" --pending-window-in-days 7

terraform -chdir=terraform destroy
rm -rf build
```

Task definitions cost nothing, but to remove them, deregister every revision of the `my2cents-` families and delete them:

```bash
ARNS=$(aws ecs list-task-definitions --family-prefix my2cents- --query 'taskDefinitionArns[]' --output text)
for a in $ARNS; do
  aws ecs deregister-task-definition --task-definition $a --query taskDefinition.status --output text
  aws ecs delete-task-definitions --task-definitions $a --query 'taskDefinitions[0].status' --output text
done
```
