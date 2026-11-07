# OpenSandbox on Amazon EKS, part 5: AWS credentials through the egress

Companion code for part 5 of the my2cents.ai series on OpenSandbox 1.2.0 on Amazon EKS. A
sandbox calls AWS (STS, S3, DynamoDB, Bedrock) with the AWS CLI and boto3, and no AWS key
ever enters it:

- the sandbox has placeholder keys only (`AWS_ACCESS_KEY_ID=AKIA_OSB_PLACEHOLDER`);
- an in-cluster **broker** assumes an IAM role per sandbox (session name = sandbox ID, one
  hour, an inline session policy) and writes the temporary credentials into the sandbox's
  Credential Vault, then rotates them every 30 minutes;
- the sandbox's egress sidecar adds them to AWS requests as carrier headers, and a SigV4
  addon in the sidecar re-signs each request with them and strips the carriers.

Video walkthrough: https://youtu.be/3r04EAfY2bc

## Requirements

- The cluster from [`../opensandbox-eks`](../opensandbox-eks): `01_cluster.sh` to
  `04_install.sh` (the server, and the `python:3.12-slim` image in ECR).
- Part 3's directory [`../opensandbox-lifecycle`](../opensandbox-lifecycle): its `common.py` keeps the
  port-forward to the server on `127.0.0.1:18080`, and `switch_runtime.sh` is used by `08_kata.py`.
- Part 4's image: run [`../opensandbox-egress/00_image.sh`](../opensandbox-egress/00_image.sh) first. It
  builds `opensandbox-eks/egress-tools:1` (the AWS CLI v2 2.39.3 on Python 3.12), which `./00_image.sh` here
  extends with boto3.
- Terraform 1.9 or later, the AWS CLI, kubectl, Docker with buildx, jq, envsubst, Python 3,
  and [uv](https://docs.astral.sh/uv/) for the tests.
- An existing S3 bucket in the cluster's Region, named by `AWS_TEST_BUCKET` in a secrets
  file outside this repository: `OSB_SECRETS_ENV`, default `~/.config/opensandbox-eks/secrets.env`.
  `secrets_env.py` parses that file (it is never sourced), reads only `AWS_TEST_BUCKET`,
  and never prints it.

## Run order

Setup (once):

| Step | What it does |
|---|---|
| `./01_iam.sh` | Terraform in `terraform/` (its own state): IAM roles `osb-broker` (EKS Pod Identity for `opensandbox-broker/osb-broker`) and `osb-sandbox`, and the DynamoDB table `osb-cred-test`. Writes `broker.env` (the outputs, plus `SANDBOX_NAMESPACE` from `../opensandbox-eks/values/server.yaml`). Prints the account ID as `<account>`. |
| `./00_image.sh` | Builds `opensandbox-eks/aws-tools:1`: part 4's `egress-tools:1` plus boto3, and pushes it to ECR. The sandbox's network policy allows AWS hosts only, so it cannot `pip install`. |
| `./02_broker.sh` | Builds and pushes `opensandbox-eks/broker:release-1.2.0`, runs it in namespace `opensandbox-broker`, then checks its identity and the credential-vending Deny from inside the pod. |
| `./build_egress.sh` | Builds the derived egress image (the SigV4 addon on top of OpenSandbox's egress image) and points the server at it. The server runs with this image for the whole part. |

The tests. Each one makes its own sandbox through the broker, prints what it does, ends with `PASS`/`FAIL` lines, exits 1 on
a failure, and deletes its sandboxes (it starts the port-forwards it needs and stops the ones it started):

| Script | What it shows | Expect |
|---|---|---|
| `uv run python 02_identity.py` | `aws sts get-caller-identity` and boto3 return `arn:aws:sts::123456789012:assumed-role/osb-sandbox/<sandbox-id>`; the session name is the sandbox ID. `env` shows placeholders only. | all checks pass |
| `uv run python 03_s3.py` | `aws s3 ls` with no bucket is denied (by design); a bucket listing, a 19-byte, a 5 MiB (single PutObject) and a 20 MiB (multipart) upload, a checksum round trip, boto3 `put_object` and `list_objects_v2`. | every operation works; the sha256 matches |
| `uv run python 04_nons3_body.py` | DynamoDB `PutItem`/`GetItem`, Bedrock `Converse` (SigV4); then the 1 MiB limit: a 0.83 MiB DynamoDB batch works, a 1.11 MiB one is killed. | the big one fails with a closed connection and a `killed` log line; nothing is written |
| `uv run python 01_bedrock_bearer.py` | Bedrock through a short-term API key bound as a `bearer` credential. Shows that the vault replaced the placeholder `Authorization` header. | measured: the bearer path works (HTTP 200 with a placeholder token, so the vault replaced it; the vault logs `applied binding=bedrock-bearer`; no addon line for the Bedrock call). Exits 1 otherwise |
| `uv run python 05_cloudtrail.py` | The sandbox's session name in CloudTrail event history: `GetCallerIdentity`, `AssumeRole`, Bedrock `Converse`. Takes 1 to 15 minutes (the recorded run: under 2). | the events appear under the sandbox ID |
| `uv run python 06_attacks.py` | Credential vending (`sts assume-role` and friends, `ecr get-login-password`), a presigned URL, Host/SNI spoofing (five cases, including the upstream pin), the metadata services, a search of the sandbox for a temporary key, the sidecar's API, a Bearer header on a SigV4 host. | every attack ends without a credential |
| `uv run python 07_lifetime.py` | The sandbox works across a forced rotation (the role allows two hours, but the broker's AssumeRole is role chaining, which AWS caps at one hour: the script shows AWS saying so); a stale `expected_revision` conflicts; deleting the vault revokes. `--wait-loop` also waits for the broker's own timer (about 30 minutes). | all checks pass |
| `uv run python 08_kata.py` | The identity, S3 and Bedrock flow once under Kata (`kata-qemu`). Switches the server to Kata and **always switches it back to runc**. | all checks pass; the server ends on runc |
| `W=<work dir> ./console/capture.sh` | A read-only console sign-in, then masked screenshots and a clip of the IAM role `osb-sandbox` and the CloudTrail entry. For the video. | PNGs and an MP4 with the account ID masked |

Suggested order: `02`, `03`, `04`, `01`, `06`, `07`, `05` (its events need minutes, so run it last and look the
sandbox ID up again with `--session` if you like), then `08`. The test scripts print the test bucket as `example-bucket`
and the account as `123456789012`; the real values are read at run time and never printed. (`00_image.sh` shows the ECR registry
host, which contains the account ID, in the build output: redact it if you record that run.)

## The sandbox's permissions

Two files in `broker/` hold them, and both the role's policy (Terraform) and every session
policy (the broker) are built from them, so the two can't drift apart:

- `sandbox_permissions.json`: the Allow statements (the test bucket, the test table, one Bedrock
  model, short-term Bedrock keys), a template Terraform and the broker fill in;
- `vending_actions.json`: the explicit Deny on everything that hands out credentials (all of STS
  but `GetCallerIdentity`, `GetAccessKeyInfo` and `DecodeAuthorizationMessage`, all of IAM, and
  the token calls of ECR, CodeArtifact, S3 Express, Redshift and Secrets Manager).

## The broker API

Only reachable through a port-forward (the broker listens on 127.0.0.1 in its pod):

```bash
kubectl --context osb-eks -n opensandbox-broker port-forward svc/osb-broker 18090:80
```

| Call | Does |
|---|---|
| `POST /sandboxes` `{"image", "bedrock_bearer"?, "timeout_minutes"?, "metadata"?}` | Creates a sandbox (deny-by-default network policy allowing the AWS hosts, credential proxy on), assumes `osb-sandbox` for it, writes the vault, starts the rotation. Returns `sandbox_id`. Its `endpoints` are the in-cluster server-proxy URLs the broker itself uses: from a laptop, connect to the sandbox by `sandbox_id` through your own port-forward to the server. |
| `GET /sandboxes/{id}` | `state`, `vault`, `revision`, `akid_prefix` (just `ASIA…`), `expires_at`, `next_rotation_in_seconds` |
| `POST /sandboxes/{id}/refresh` `{"expected_revision"?}` | Rotates now. A stale `expected_revision` returns the vault's own conflict status. |
| `DELETE /sandboxes/{id}/vault` | Revokes: deletes the vault, stops the rotation. |
| `DELETE /sandboxes/{id}` | Stops the rotation and deletes the sandbox. |

## What the scripts will not print

The bucket name (`AWS_TEST_BUCKET`, read by `secrets_env.py` from the secrets file, never sourced), the account ID,
a temporary key or a session token. A shared filter in `common.py` rewrites them in every printed line, and an
AWS error is cut down to its error code: the scripts never show a raw AWS error body. Even so, the addon redacts
the real key ID and session token if AWS echoes them in a small error body.

## The placeholder key

The sandbox's `AWS_ACCESS_KEY_ID` is `AKIA_OSB_PLACEHOLDER`, with `AWS_SECRET_ACCESS_KEY=osb-placeholder-secret`. It is
not a valid key and, on purpose, does not match the access-key pattern `AKIA[0-9A-Z]{16}` that secret scanners look for
(the underscore breaks it). `common.py` asserts that at import, and `02_identity.py` checks it again against the
variable inside the sandbox. There is no `AWS_SESSION_TOKEN` and no `AWS_ACCOUNT_ID` in the sandbox (with an account ID
set, DynamoDB switches to an account-based endpoint outside the allowed hosts).

## The SigV4 addon: log lines, refusals and limits

The addon (`egress/sigv4_resign.py`, run by the sidecar's mitmproxy) writes one warn-level line per decision to the
sidecar's container log, flushed at once. The scripts match them with a plain substring (`credential proxy: [sigv4]`),
not anchored at the start of the line:

```text
credential proxy: [sigv4] re-signed <METHOD> <host><path> service=<s> region=<r> key=<4 chars>... payload=<p> hook=<requestheaders|request>
credential proxy: [sigv4] refused <status> <METHOD> <host><path> reason=<reason>
credential proxy: [sigv4] killed [<status>] <METHOD> <host><path> reason=<reason>
credential proxy: [sigv4] presigned <METHOD> <host><path> reason=query-auth-forwarded-unchanged
```

A refusal is an HTTP answer with the response header `x-osb-egress-refused: <reason>`: 403 for `credential-vending`,
`bearer-on-sigv4-binding`, `incomplete-carriers`, `duplicate-header`, `ambiguous-target`, `unsupported-authorization`,
`unsupported-payload-hash`; 501 for `signed-chunk-upload` and `sigv4a-not-supported`. A kill closes the connection: the
client sees no HTTP status, and the reason is only in the sidecar log (`host-sni-mismatch`,
`body-may-stream-without-payload-hash`, `error-body-...`, `redaction-error`, `streamed-before-signing`, or `error=<Type>`
for an exception).

**A refusal becomes a kill when the request may stream.** mitmproxy cannot send a local response once the body is
streaming, so a body that is chunked, or an HTTP/2 request without a Content-Length (which includes every HTTP/2 `GET`),
is killed instead of getting the 403. That is why `06_attacks.py` sends the Bearer-on-SigV4 test as a `POST`: the same
request as a bodyless `GET` ends in `curl: (92)`, with the reason `bearer-on-sigv4-binding` only in the log.

Limits to know about:

- **A non-S3 request body over about 1 MiB is killed**, not refused with a 403, because signing needs a hash of the whole
  body and the addon holds at most about 1 MiB. S3 is exempt when the client declares `x-amz-content-sha256` (the AWS
  CLI and boto3 do); an S3 request over 1 MiB without it (plain `curl`) is killed too. A signed-chunk upload gets 501.
- **A presigned URL is not refused**: it is forwarded unchanged with the carrier headers stripped and never re-signed,
  so a URL presigned with the sandbox's own placeholder key works only with a real key, which the sandbox does not have.
  A URL that someone else presigned with a real key for their own bucket is forwarded the same way, and it works: it is a
  way for data to leave the sandbox, so keep the sandbox's network policy to the hosts it needs (the allow list) and do not
  rely on the addon to stop it.
- **AWS error bodies are held and scrubbed** (the real key ID and session token are replaced if echoed). A body over
  1 MiB, or one the addon cannot decode, is killed. A signed `HEAD` answered with an error and an object-sized
  `Content-Length` is killed too. A scrubbed DynamoDB error body fails its `x-amz-crc32` check, so the client retries
  instead of reporting the AWS error code.
- **Credential-vending calls** (`sts:AssumeRole*`, `GetSessionToken`, `GetFederationToken`, `ecr:GetAuthorizationToken`,
  `s3express:CreateSession` and others) are refused by the addon and denied by the role and session policy. The list is
  not exhaustive: it does not cover, for example, `cognito-identity` or IAM login profiles, which is why the sandbox's
  permissions are an allow list too, and why the AWS hosts a sandbox can reach are an allow list.
- **SigV4a** (multi-region access points) is not supported.
- **Least privilege has consequences**: `aws s3 ls` with no bucket needs `s3:ListAllMyBuckets`, which the role does not
  have, so use `aws s3 ls s3://<bucket>/`.
- **CA trust**: the AWS CLI v2 trusts the proxy's CA through `REQUESTS_CA_BUNDLE`, which OpenSandbox's bootstrap sets, or
  through `AWS_CA_BUNDLE` (the broker sets it). It ignores `SSL_CERT_FILE` and the OS trust store.
- **HTTP 412 from the broker** can mean "vault not Ready" as well as a revision conflict; the broker's reply body says which.
- **Revoking is not STS revocation**: deleting the vault stops the sandbox from using the credentials, but the temporary
  credentials stay valid at AWS until they expire (at most an hour). Cutting them off everywhere takes an IAM Deny on
  `aws:TokenIssueTime`.

## Unit tests

No AWS account or cluster needed: boto3 and the OpenSandbox SDK are mocked.

```bash
uv run pytest -v                                            # the broker and the secrets parser (pyproject.toml)
uv run --project egress pytest egress/tests -v -rs         # the SigV4 addon (mitmproxy 11.0.2, the version in the image)
```

The addon tests use OpenSandbox's `system.py` for the end-to-end cases when the image or a source checkout is available
(set `OSB_EGRESS_SYSTEM_PY` to point at it); without it, one test skips and the rest run on a stand-in.

## Cleanup

Each test deletes its own sandboxes. When you are done with part 5:

```bash
kubectl --context osb-eks delete namespace opensandbox-broker
./01_iam.sh destroy                       # terraform destroy: the two roles, the association, the table
../opensandbox-lifecycle/switch_runtime.sh runc   # only if 08_kata.py was interrupted
```

Then put the server back on the base egress image with `../opensandbox-lifecycle/server_config.py --egress-image base`, and delete the ECR repositories `opensandbox-eks/broker` and `opensandbox-eks/aws-tools` if you
do not need them. Keep `opensandbox-eks/egress-sigv4` until part 6's SigV4 ladder rung has run, and switch the server to
`base` before deleting it; `../opensandbox-eks/99_teardown.sh` removes every `opensandbox-eks/*` repository. The roles and the
table cost nothing idle; the cluster is what costs.

## License

Apache 2.0, see [LICENSE](LICENSE).
