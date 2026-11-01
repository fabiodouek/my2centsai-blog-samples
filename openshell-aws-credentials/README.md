# OpenShell and AWS credentials on Amazon EKS

Companion code for part 5 of the my2cents.ai series "OpenShell on Amazon EKS": how a sandbox
reaches AWS with only placeholder keys in its environment. The OpenShell gateway gets an identity from **EKS Pod
Identity**, mints a short-lived **STS** session for a least-privilege sandbox role, and the
**supervisor signs every request with SigV4** on the way out. The sandbox sees only placeholder
`AWS_*` values. S3, Bedrock (`amazon.nova-micro-v1:0`) and a GetCallerIdentity STS demo, all on
OpenShell **0.1.6**, sandboxes on runc.

Video walkthrough: [youtu.be/HV08Dzx9Zww](https://youtu.be/HV08Dzx9Zww)

## How it fits together

```
sandbox workload            supervisor (per sandbox)              gateway                AWS
  AWS_*=<placeholder>  --->  terminates TLS, strips the client's  --(EKS Pod Identity)-->
  aws / boto3 / curl         auth, re-signs with SigV4 using  --->  osh-eks-gateway
                             the sandbox-role session       <---    sts:AssumeRole
                                                                    osh-eks-sandbox-s3bedrock
                                                             --->  S3 / Bedrock / STS
```

- **Gateway identity:** a Pod Identity association on the gateway ServiceAccount (`openshell`)
  gives it the `osh-eks-gateway` role, which may only `sts:AssumeRole` + `sts:TagSession` into
  `osh-eks-sandbox-*`. The gateway is the only AWS principal in the cluster; sandboxes never get
  an IAM identity.
- **Sandbox role** `osh-eks-sandbox-s3bedrock`: one bucket, `amazon.nova-micro-v1:0` (Converse and
  ConverseStream), `sts:GetCallerIdentity`, and an explicit **Deny** on credential-vending and
  escalation actions (`sts:AssumeRole*`, `GetSessionToken`, `GetFederationToken`,
  `ecr:GetAuthorizationToken`, `s3express:CreateSession`, `secretsmanager:GetSecretValue`,
  `ssm:GetParameter*`, `iam:*`, …). OpenShell signs whatever request reaches a signed endpoint and
  does not rewrite responses, so IAM is the only thing that stops an API that would hand back real
  credentials. IAM is the only control here: the role allows none of these actions, and the explicit
  deny covers them even if another statement ever does.
- **Provider and policy:** the endpointless `aws` profile carries the three `AWS_*` credentials and
  the `aws_sts_assume_role` refresh; `policies/aws-signing.yaml` binds them to the S3, Bedrock and
  STS endpoints with `credential_binding.provider` and turns on `credential_signing` (one provider
  can sign several services only when its profile is endpointless). Every inspected endpoint is
  `enforcement: enforce` (the L7 default is `audit`).

## Requirements

- The `osh-eks` cluster and gateway from [`../openshell-eks`](../openshell-eks) (part 2): scripts
  01 to 05 done, the port-forward running (`../openshell-eks/09_scale.sh up` restarts it). The EKS
  Pod Identity agent add-on is part of that install.
- The `default` AWS profile with rights to create IAM roles, an S3 bucket and a Pod Identity
  association; Terraform >= 1.9; the AWS CLI v2; `kubectl`, `jq`, `envsubst`, `python3`.
- The OpenShell CLI 0.1.6. The gateway is reached as `-g osh-eks` (put `-g` before the subcommand).
- Bedrock model access for `amazon.nova-micro-v1:0` in us-east-1 (on in the `default` account).

The account ID is never printed: anything the scripts echo, and the saved logs, show it as
`<account>`.

## Setup

```bash
(cd terraform && terraform init && terraform apply)   # bucket + two roles + Pod Identity assoc.
./01_setup.sh                                         # restart gateway, import, provider, refresh
```

`terraform apply` creates only IAM, the S3 bucket and the Pod Identity association, and restarts
nothing. `01_setup.sh` does the one disruptive step: it `kubectl rollout restart`s the gateway
StatefulSet so the new Pod picks up its Pod Identity credentials, then restarts the port-forward.

## Scripts

| Script | What it shows |
|---|---|
| `01_setup.sh` | terraform outputs → `env.sh`; restart the gateway for Pod Identity; import the `aws`/`aws-s3` profiles; create provider `p5-aws` (`--runtime-credentials`); `refresh configure --strategy aws-sts-assume-role`; `refresh rotate`; `refresh status` |
| `02_identity.sh` | The `AWS_*` env vars are placeholders; `aws sts get-caller-identity` and boto3 both return the sandbox-role session |
| `03_s3.sh` | list, small put, a 50 MB `aws s3 cp`, download + checksum, boto3; keys that need percent-encoding (space, `+`, non-ASCII, `a//b`); `s3 rm` (DELETE) vs `delete-objects` (POST); the `sigv4:body` (10 MiB cap) and `sigv4:no_body` modes |
| `04_bedrock.py` | Converse, ConverseStream time-to-first-token, and a model-ARN call the signer's path handling refuses |
| `05_ca_trust.sh` | Which CA variable each client needs. OpenShell sets `REQUESTS_CA_BUNDLE`/`SSL_CERT_FILE`/`CURL_CA_BUNDLE` but **not** `AWS_CA_BUNDLE`; toggles each for the AWS CLI, boto3 and the CLI's CRT transfer client |
| `06_cloudtrail.sh` | Session names in event history: two sandboxes on the shared provider share one name; a second provider with its own `session_name` gives a third its own attribution |
| `07_attacks.sh` | Unsigned curl signed anyway; `AssumeRole`/`GetSessionToken` refused by IAM (the role doesn't allow them; `GetSessionToken` never works from a role session); a presigned URL; IMDS and the Pod Identity agent; scanning env/`/proc`/files for real keys |
| `08_lifetime.sh` | Refresh timing (300 s before expiry), a manual `rotate`, and provider delete as revocation |
| `99_cleanup.sh` | Delete the `p5-*` sandboxes and providers, delete the `aws`/`aws-s3` profiles, empty the bucket |

```bash
./02_identity.sh
./03_s3.sh
python3 04_bedrock.py      # or: uv run python 04_bedrock.py
./05_ca_trust.sh
./06_cloudtrail.sh
./07_attacks.sh
./08_lifetime.sh
```

Each script creates only `p5-*` sandboxes/providers, prints short result lines, never prints a
credential (only placeholders, lengths and SHA-256 prefixes), and cleans up in a trap even if
interrupted. `06_cloudtrail.sh` waits on CloudTrail event history, which lags a few minutes.

## Configuration files

| File | Why |
|---|---|
| `terraform/` | The AWS side: the bucket (`osh-eks-creds-<random>`, 7-day expiry, `force_destroy`), the `osh-eks-gateway` role + Pod Identity association, the `osh-eks-sandbox-s3bedrock` role with the explicit denies. Providers pinned, local state |
| `profiles/aws.yaml` | The endpointless `aws` profile (v0.1.6, verbatim): the three `AWS_*` credentials and the STS refresh. The main flow uses this |
| `profiles/aws-s3.yaml` | The endpoint-bearing S3 profile (v0.1.6) with this image's binaries; `/bin/bash` dropped. Kept as the S3-only alternative |
| `policies/aws-signing.yaml` | The signing policy: S3 bucket, Bedrock runtime (Converse/ConverseStream), regional STS, all signed and enforced. `${BUCKET}`/`${REGION}` filled in by `01_setup.sh` |
| `logs/` | `terraform-plan.txt` and `verify.txt` (IAM simulation + Bedrock smoke test), account masked |

## What this does not solve

- While the sandbox runs, it can use the role's permissions. Signing is per endpoint, not per
  binary, and needs no placeholder — any binary a rule lets reach a signed host is signed.
- Nothing in OpenShell blocks credential-vending APIs; only IAM does (the sandbox role allows none and denies them explicitly).
- One STS session per provider: sandboxes on the shared provider are indistinguishable in
  CloudTrail. Per-sandbox attribution needs one provider per sandbox (see `06_cloudtrail.sh`).
- Signer hardening PR #4558 was unmerged at v0.1.6.
- In 0.1.6, placeholders the workload places on a signed endpoint resolve to real values, a known weakness with a fix pending in [NVIDIA/OpenShell#4558](https://github.com/NVIDIA/OpenShell/pull/4558); until it ships, treat the role session as reachable by the agent.

## Cleanup

```bash
./99_cleanup.sh                        # gateway-side objects + bucket contents
(cd terraform && terraform destroy)    # the roles, Pod Identity association and bucket
```

`terraform destroy` removes the IAM roles, the Pod Identity association and the bucket
(`force_destroy` empties it). Scale the node group to zero between sessions with
`../openshell-eks/09_scale.sh down`.

## Results

From the recorded runs (OpenShell 0.1.6, EKS 1.37, us-east-1, AWS CLI 2.39.3, boto3 1.43.123):

| | Result |
|---|---|
| Gateway identity | EKS Pod Identity after a gateway restart; refresh `refreshed`, session 3,600 s |
| In the sandbox | `AWS_*` are placeholders (about 105 characters); no keys in env, `/proc` or files |
| `get-caller-identity` (CLI and boto3) | `assumed-role/osh-eks-sandbox-s3bedrock/p5-shared` |
| S3 | Small put, 50 MiB up and down with matching checksum, boto3 round trip |
| Keys with a space, `+`, non-ASCII | `SignatureDoesNotMatch` |
| Key `a//b.txt` | Reported uploaded, stored as `a/b.txt` |
| `aws s3 rm` / `delete-objects` | 403 at L7 / allowed (POST) |
| `sigv4:body` / `sigv4:no_body`, CLI defaults | `MalformedTrailerError` / `InvalidArgument`; both work with `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` |
| `sigv4:body`, one 11 MiB PutObject | Connection dropped (client: "SSL validation failed ... unexpected eof"); 9 MiB works |
| Bedrock | Converse ok; ConverseStream first token 278 ms; model ARN: connection closed |
| CA trust | CLI calls and boto3 work as is; `aws s3 cp` (CRT client) needs `AWS_CA_BUNDLE=/run/openshell-supervisor-ca/material/ca-bundle.crt` |
| CloudTrail | One session name per provider; the AssumeRole calls come from the gateway role's Pod Identity session |
| Unsigned `curl` to the bucket | 200: signed by the supervisor |
| Placeholders the workload places on signed endpoints | Resolved to real values in 0.1.6; a known weakness with a fix pending in [NVIDIA/OpenShell#4558](https://github.com/NVIDIA/OpenShell/pull/4558) (not reproduced here) |
| AssumeRole / GetSessionToken | `AccessDenied` from IAM (not allowed by the role; `sts:TagSession` for AssumeRole) |
| Presigned URL | 400 |
| Revocation | Detach refused while the policy binds the provider; policy without AWS endpoints, then detach (9.0 s), then delete |
