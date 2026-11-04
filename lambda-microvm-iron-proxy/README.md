# Lambda MicroVM Credentials with iron-proxy

AWS Lambda MicroVMs have no egress proxy and no credential injection. This repo builds both, with no proxy settings in the sandboxes. [iron-proxy](https://github.com/paradigmxyz/iron-proxy) v0.55.0 runs on ECS Fargate behind an internal NLB and does three things:

- denies by default
- swaps placeholder tokens for real Anthropic and GitHub credentials held in Secrets Manager
- re-signs AWS SigV4 requests with its own task role

The MicroVMs run in a separate VPC where the only reachable address is that NLB, and where every DNS name resolves to it.

Companion code for [Lambda MicroVM: Hands-On Egress Credential Injection with iron-proxy](https://my2cents.ai/deep-dive/lambda-microvms-iron-proxy), part 2 of the AWS Lambda MicroVMs series.

Video walkthrough: https://youtu.be/An3nIGVDxs4

## Two VPCs, and why

| VPC | CIDR | What's in it | DNS |
|---|---|---|---|
| proxy | 10.50.0.0/16 | NAT gateway, iron-proxy on Fargate, the internal NLB (10.50.10.10, 10.50.11.10) | normal Route 53 |
| sandbox | 10.51.0.0/16 | only the MicroVM egress connector: no internet gateway, no NAT | every name rewritten to the NLB |

A MicroVM's network is chosen at `run-microvm` time: here, a VPC egress connector whose network interfaces sit in the sandbox VPC. From there it's ordinary VPC traffic.

**Why two VPCs?** DNS Firewall rules apply to a whole VPC. The sandboxes need DNS that sends every name to the proxy; iron-proxy, ECS and Fargate need DNS that tells the truth. Two VPCs give each its own resolver. The split also takes the internet route away from the sandboxes entirely, instead of relying on a security group alone.

**Peering joins them.** A VPC peering connection (same account, same region) carries the sandboxes' traffic to the NLB:

- **Routes.** The sandbox VPC's route table has `local` and the NLB's two /32s, pointing at the peering connection. Nothing else. Peering isn't transitive, so the sandbox VPC can't use the proxy VPC's NAT or internet gateway even in principle. The proxy subnets have a return route to the two connector subnets.
- **Security groups.** They reference each other across the peering. The connector's group may send TCP 80, 443 and 8080 to the NLB's group, and the NLB's group accepts those ports from the connector's group. Nothing else in the proxy VPC is reachable: not the NAT, not the iron-proxy task directly, not the proxy VPC's resolver.

**How DNS Firewall turns every name into the proxy.** The sandbox VPC has three pieces of DNS:

1. A private hosted zone `sandbox.internal`, visible only in the sandbox VPC, with `proxy.sandbox.internal` → both NLB IPs.
2. A Route 53 Resolver DNS Firewall rule group, associated with the sandbox VPC:
   - priority 100: ALLOW `proxy.sandbox.internal.`
   - priority 200: BLOCK `*.` with an `OVERRIDE` response, a CNAME to `proxy.sandbox.internal.`
3. Fail-closed mode, and Resolver query logging with the firewall's action on each query.

A MicroVM asks for `api.github.com`; the Resolver gets the override CNAME, follows it itself, and answers with the CNAME **and** both NLB addresses in one response. curl, git, gh, the AWS CLI, Python and Node then connect to the NLB on 443 believing it's GitHub. iron-proxy reads the host from the TLS handshake (SNI), presents a certificate for it signed by its own CA (baked into the image), and sees the real request, so path rules and `require` work. No name ever recurses to the internet, so a lookup can't carry data out. iron-proxy itself is unchanged; its own DNS server stays off.

**What changes for clients.** Nothing to configure: no `HTTPS_PROXY`, and clients that ignore proxy settings (Node's `fetch`, Python's `http.client`) go through iron-proxy like everything else.

## Trade-offs

- **Non-HTTP fails by timeout.** git over SSH, raw TCP, other ports and QUIC all resolve to the NLB, which has no listener there, and the security group drops the packets. Clients wait out their connect timeout instead of getting a refusal.
- **Hosts off the allowlist** resolve to the proxy, complete TLS with a certificate from the proxy's CA, and then get HTTP 403 with an empty body. curl exits 0. The same happens for names that don't exist.
- **IPv6 is present but dead.** Each MicroVM gets a global IPv6 address and a default IPv6 route from Lambda, though neither VPC has IPv6. Every IPv6 connection attempt timed out (`02_fence.py`). It's an interface the VPCs' controls don't govern, so it's tested rather than assumed.
- **The DNS control rests on a few Resolver settings, and only one is protected.** Remove the firewall association and the sandbox VPC goes back to recursive DNS within about a second (`12_dns_firewall.py`). The association has **mutation protection** on, so that call is refused until someone switches protection off first. Protection covers only the association, though: one call to the unprotected rules, domain lists or a new rule group (an ALLOW rule, `*.` imported into the allow list, an ALLOW-all group at a higher priority) would reopen recursion too. An EventBridge rule logs every DNS Firewall write, refused attempts included (alarm log group plus an SNS topic to wire to your pager). The routes still block direct connections while DNS is open.
- **Mutation protection blocks `terraform destroy`** too. Use `scripts/destroy.sh`, which switches it off first.
- **The proxy can't tell MicroVMs apart.** Every connection reaches iron-proxy from the NLB's addresses (`10_shared_proxy.py`).

## Prerequisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/), Terraform 1.6+, AWS CLI v2.
- AWS credentials allowed to manage, in a Region with MicroVMs (default `us-east-1`):
  - VPC (including peering), ELB, ECS, IAM, Secrets Manager, S3, CloudWatch Logs;
  - Route 53: private hosted zones, Resolver DNS Firewall and query logging;
  - EventBridge and SNS;
  - Lambda MicroVMs and network connectors.
- Room for two VPCs (the default quota is five per Region).
- CloudTrail logging management events in the Region (any trail), for the EventBridge alarm.
- An Anthropic API key, and a fine-grained GitHub token scoped to one test repository (Contents and Pull requests: read and write).
- MicroVM quota for at least two 2 GB MicroVMs at once.
- `dig` on the machine running the scripts (some tests compare its answers with the MicroVM's).

The AZs are set by ID (`az_ids`, default `use1-az4` and `use1-az6`). Change them if you change the Region.

## Setup

```bash
cp .env.example .env            # ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_REPO
uv venv && uv pip install -e . && source .venv/bin/activate
terraform -chdir=infra init
terraform -chdir=infra apply -var github_repo=your-user/your-test-repo   # ~7 min from nothing; ~4.5 of it is the connector, which waits for the DNS Firewall association
python 01_setup.py              # secrets into Secrets Manager, proxy health, sandbox image (~3 min)
```

Put any variables you change in `infra/terraform.tfvars`, so the scripts that run `terraform apply` (`11` and `12`) keep them.

The real credentials go from `.env` straight to Secrets Manager. Terraform only creates empty secrets, so they never reach its state, and they never enter a MicroVM. A MicroVM gets its settings in the run hook payload, and the in-VM agent writes them to `/etc/sandbox.env`, which every command sources:

- placeholders: `ANTHROPIC_API_KEY`, `GH_TOKEN`, `DEMO_TOKEN`;
- placeholder AWS keys plus `AWS_EC2_METADATA_DISABLED=true` (without keys the AWS CLI refuses to send anything, and `aws_auth` has nothing to re-sign);
- the CA settings: `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `AWS_CA_BUNDLE`;
- no proxy variables. `run(mode="explicit")` adds `HTTPS_PROXY` to the NLB on 8080, only for the comparisons in `11` and `14`.

## Layout

| Path | What |
|---|---|
| `infra/main.tf` | Variables, provider, CIDRs, the NLB's fixed IPs |
| `infra/proxy_vpc.tf` | Proxy VPC: public subnet + NAT, proxy subnets and their route table (NAT, return routes over the peering), NLB and proxy security groups |
| `infra/sandbox_vpc.tf` | Sandbox VPC: connector subnets, route table (NLB /32s → peering only), the peering, the connector's security group, operator role, the egress connector |
| `infra/dns.tf` | `sandbox.internal`, DNS Firewall (allow the proxy name, override everything else), mutation protection, fail-closed config, query logging |
| `infra/alarm.tf` | EventBridge rule on every DNS Firewall write in CloudTrail (rules, rule groups, domain lists, associations, the fail-open setting) and the query-log disassociation → log group + SNS topic |
| `infra/proxy.tf` | Demo CA, secrets, ECS cluster, iron-proxy task (pinned by digest) and service, internal NLB (80, 443, 8080; 3600 s idle timeout; its fixed IPs inside an explicit subnet CIDR reservation, .8/29, so nothing else is auto-assigned them) |
| `infra/iron-proxy.yaml.tftpl` | iron-proxy config: listeners, `upstream_deny_cidrs`, allowlist, secrets (`aws_sm`, 30 s ttl), `aws_auth` with workload identity; `strict` adds path rules and `require` |
| `infra/sandbox.tf` | Artifact bucket and build role for the image, the demo bucket, a logs-only execution role (`08_leaks.py`'s contrast) |
| `images/sandbox/` | Claude Code, gh, git, AWS CLI, the demo CA, and the in-VM agent |
| `common.py` | `run()` on the connector, the VM wrapper, `claude()`, audit and query-log readers, `digs()`, rollout-aware `apply_infra()`, redaction |
| `infra/control.tf`, `infra/roles.tf`, `infra/control.sh` | `control_plane=true` only: the config bucket, the management token, the four sandbox roles, and the sidecar script |
| `scripts/destroy.sh` | Checks part 3 is gone, switches off mutation protection, runs `terraform destroy`, removes part 3's leftover credential secrets |

## Scripts

Each writes `results/NN_name.log`, with the account ID, bucket names, secret ARNs and the NAT's public IP redacted, and ends with its checks (exit code 1 if any failed).

| Script | What it shows |
|---|---|
| `01_setup.py` | Secrets, proxy health behind the NLB, the sandbox VPC's routes and firewall association, the image build |
| `02_fence.py` | Routes, gateways and security groups; NLB 80/443/8080 open; direct egress by real IP, other ports, DNS to public resolvers, the rest of the proxy VPC and IPv6 all time out; the proxy path works, egress IP is the NAT gateway |
| `03_injection.py` | How a request reaches the proxy with no proxy settings (NLB IP, demo-CA certificate); where a placeholder is swapped (header, query, body, plain HTTP) and where it isn't (other hosts, split, base64); allowlist denials after TLS |
| `04_claude_code.py` | Claude Code with a placeholder key; a tool-using run; streaming; audit by path |
| `05_github.py` | gh and git (clone, push) with no token; what GitHub's token scoping refuses |
| `06_agent_pr.py` | Claude Code clones, commits, pushes and opens a pull request; requests by host; then closes the PR and deletes the branch |
| `07_aws.py` | STS, S3 and ECS via `aws_auth`; denied service and region; keys needing encoding; uploads; presigned URLs. Bucket and cluster names anonymized |
| `08_leaks.py` | What's in the VM, reflection, routes around the proxy (real IPs, DNS over HTTPS), SSRF, DNS as a side channel, Claude Code asked for the keys, and a contrast run with an execution role |
| `09_rotation.py` | Rotation within the ttl, revocation at the proxy (the cached value keeps working), fail-open after a restart, rotation across suspend/resume. Restarts the proxy |
| `10_shared_proxy.py` | A second MicroVM with no placeholders gets the same access; the audit log can't tell them apart |
| `11_path_rules.py [--keep]` | Applies `strict=true` (one repo, path rules, `require`) and runs the same probes in a transparent MicroVM, where they work (git with `http.extraHeader`), and an explicit one, where every GitHub CONNECT is refused. Reverts unless `--keep` |
| `12_dns_firewall.py [--no-misconfig]` | One answer with the CNAME and both NLB IPs; every record type and resolver address, UDP and TCP; nip.io, sslip.io and TXT records prove nothing recursed; the query log. Then the misconfiguration: removal refused under mutation protection; with protection off, recursion returns within about a second and the alarm fires; restored with `terraform apply` |
| `13_cleanup.py` | Terminate this repo's MicroVMs, delete the image and its build log group |
| `14_latency.py` | Transparent vs explicit (CONNECT on 8080) vs transparent without DNS, 10 rounds each; DNS lookup time through the override |

`11` and `12` change the infrastructure and put it back, `12` inside a `finally`. If one is interrupted, `terraform -chdir=infra apply` restores the defaults.

## Variables

| Variable | Default | What it does |
|---|---|---|
| `strict` | `false` | GitHub limited to `github_repo` with path rules, and the GitHub secret marked `require: true` |
| `github_repo` | `fabiodouek/my2centsai-sandbox-test` | The repository `strict` allows |
| `firewall_mutation_protection` | `ENABLED` | Protects the DNS Firewall association from removal and changes |
| `az_ids` | `["use1-az4", "use1-az6"]` | The two AZs, by ID |
| `proxy_generation` | `1` | Bump to force a new iron-proxy task |
| `control_plane` | `false` | Part 3's control plane (below) |
| `user_secret_prefix` | `my2cents-ip/user/` | With `control_plane`: the prefix of part 3's added credentials, the only other secrets the sandbox roles may read |

The outputs include what [part 3's workspace](../lambda-microvm-workspace) builds on: the proxy VPC and its subnets and route tables, the NAT gateway, the ECS cluster, the NLB's security group, iron-proxy's task and execution roles, the CA secret, the injected secrets, and the sandbox VPC.

## Control plane (for part 3)

`control_plane=true` exists for [part 3's workspace](../lambda-microvm-workspace), which edits the proxy's policy from its web UI. It's off by default; part 2's post and every script here assume it's off (with it on, `07_aws.py` sees a sandbox role instead of the task role, and edits from part 3 change what the other tests expect).

- **Config in S3.** Terraform seeds `iron-proxy.yaml` (this template plus a management API on loopback) and `aws-scope.json` into a private, versioned bucket once, then leaves them alone: part 3 changes the allowlist, the secrets and the AWS access there.
- **A `control` sidecar** in the iron-proxy task ([`infra/control.sh`](infra/control.sh), AWS's aws-cli image) polls the bucket every 2 s. On a change it swaps the file in and calls iron-proxy's `POST /v1/reload` on `127.0.0.1:9092` with a token from Secrets Manager. It reports what it applied in `status.json`, including the session's `SessionTokenUtilization` and `SessionTokenSize`. iron-proxy keeps its old pipeline if a new config doesn't validate.
- **A different AWS identity.** Sandbox AWS calls are signed as a `my2cents-ip-<role>` session (`proxy-<role>-v<scope version>`), not the task role. The four roles trust only the task role:
  - `view-only` (the default): AWS's ViewOnlyAccess;
  - `read-only`: ReadOnlyAccess;
  - `data-reader`: the demo bucket and the `my2cents-demo-*` DynamoDB tables (`my2cents-demo-items` has 3 items);
  - `builder`: PowerUserAccess in this Region, denied any change to this stack (both VPCs, the peering, the connector, DNS Firewall, the private zone, the NLB, ECS, the buckets, logs, the alarm) and to part 3's `my2cents-ws*` resources.

  Each role may read only the proxy's own secrets (the three here and part 3's under `user_secret_prefix`), and no SSM parameter values. Part 3 can narrow a role further with a session policy. Sessions are renewed every 40 minutes, since role chaining caps them at an hour.
- **Secrets Manager stays out of reach.** iron-proxy fetches its secrets with the same session it signs sandbox calls with, so the sidecar refuses a config that mentions `secretsmanager` outside a `secret_id`, has an empty or missing `aws_auth` `allowed_services` (iron-proxy reads an empty list as every service), or allowlists a host that matches a Secrets Manager endpoint (`*.amazonaws.com` included). The last good config stays in place, and `status.json` gives the reason.

```bash
echo 'control_plane = true' >> infra/terraform.tfvars   # so later applies (11 and 12 included) keep it
terraform -chdir=infra apply                            # adds the bucket, the token, the roles, and the sidecar
```

The first apply replaces the iron-proxy task: about 3 minutes until the new one (iron-proxy plus the sidecar) serves. Switching it off again removes all of it and puts the task back to the plain config.

**Cost.** The sidecar shares the existing Fargate task (0.5 vCPU, 1 GB), so compute doesn't change. What it adds is about $1.90/month: S3 requests (two conditional GETs every 2 s, about $1.05, and a status PUT every 30 s, about $0.45) and the management token in Secrets Manager ($0.40). The roles, STS and the on-demand demo table cost nothing at this scale; the polls through the NAT gateway are a few MB a day.

## Gotchas

- **Routes are `aws_route` resources only.** A route table with inline `route` blocks owns all its routes, so mixing the two makes every apply delete the peering routes.
- **`"*."`, not `"*"`.** AWS stores the wildcard domain as `*.`; writing `*` leaves a permanent diff.
- **The ECS endpoint.** `upstream_deny_cidrs` replaces iron-proxy's defaults. The defaults don't include `169.254.170.2`, where Fargate serves the task role's credentials, so the config lists it, along with both VPCs.
- **Waiting for a deploy.** After a config change, ECS's `services-stable` waiter can return while the old task is still serving the old config. `apply_infra()` waits for one deployment with `rolloutState=COMPLETED` and healthy targets.
- **API limits.** `RunMicrovm` is about 1 TPS per account, so `run()` backs off on its own; every other MicroVM call uses a retrying client.
- **httpbin's rate limit.** httpbin.org allows 2 echo requests per second per client IP. The scripts pause between echoes.
- **No retries after 403.** With `require: true`, git's first unauthenticated request gets a 403 instead of a 401, so git never retries with credentials. Send the placeholder up front with `http.extraHeader`.

## Cost and cleanup

The fixed part costs about $0.095/h whether or not sandboxes run: NAT gateway ($0.045), NLB ($0.0225), a Fargate task ($0.020), a public IPv4 address ($0.005), Secrets Manager (4 secrets) and the private hosted zone. Peering has no hourly charge. Per use: DNS Firewall ($0.60 per million queries), query logs, data across AZs over the peering ($0.01/GB each way), and MicroVMs at part 1's rates.

When you're done:

```bash
python 13_cleanup.py            # terminate MicroVMs, delete the image (it bills snapshot storage)
scripts/destroy.sh              # then terraform destroy (~5 min)
```

`scripts/destroy.sh`:
1. stops if anything but iron-proxy runs in the ECS cluster (part 3's API does when part 3 is deployed on this stack; destroy part 3 first);
2. switches off the DNS Firewall association's mutation protection, which `terraform destroy` can't get past;
3. runs `terraform destroy` (extra arguments, such as `-auto-approve`, go to it);
4. with `control_plane=true`, force-deletes the credentials part 3 added under `user_secret_prefix`. Terraform doesn't manage them, and a deleted secret's name stays taken for its recovery window.

## License

Apache-2.0
