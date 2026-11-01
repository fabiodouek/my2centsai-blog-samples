# OpenShell policy, egress and credentials on Amazon EKS

Companion code for part 4 of the my2cents.ai series "OpenShell on Amazon EKS": what OpenShell 0.1.6
lets a sandbox reach, how it decides, and how an agent uses credentials it gets only as placeholders. Every test
runs on the cluster from part 2, against an in-cluster echo server that prints what it receives.

Video walkthrough: [youtu.be/OT9lLu1p9-U](https://youtu.be/OT9lLu1p9-U)

## Requirements

- The `osh-eks` cluster and gateway from [`../openshell-eks`](../openshell-eks) (part 2): scripts
  01 to 05 done, the port-forward running (`../openshell-eks/09_scale.sh up` restarts it)
- The OpenShell CLI and `openshell-prover` 0.1.6, `kubectl`, `helm`, `jq`, `python3`
- For `06_credentials.sh` only, in your environment (never on a command line):
  - `GITHUB_TOKEN`: a fine-grained token with contents read/write on one repository you own
  - `GITHUB_REPO`: that repository, as `owner/name`
  - `ANTHROPIC_API_KEY`

## Scripts

| Script | What it shows |
|---|---|
| `00_echo.sh` | Deploys the echo server (`manifests/echo.yaml`) as `http://echo.echo.svc.cluster.local/` |
| `01_deny_and_dns.sh` | Policy DNS answers, the two denial shapes (`EACCES` on connect, HTTP 403 with a JSON body), what `openshell logs --level warn` leaves out |
| `02_binaries.sh` | Rules scoped to a binary: curl allowed, python3 and a copy of curl denied; a pinned binary denied after one byte changes |
| `03_l7_rules.sh` | REST rules: audit (the default) vs enforce, method and path rules, path traversal, and an encoded `;` in a path (NVIDIA/OpenShell#3902) |
| `04_live_updates.sh` | Time for a new rule to reach a sandbox with poll and with push delivery; an open connection cut by a new policy revision. Restarts the gateway twice and puts the default back |
| `05_tls.sh` | The per-sandbox CA, the variables that point at it, and which tools trust it (curl, Python, git, `aws s3 ls`; `aws s3 cp` also needs `AWS_CA_BUNDLE`, see part 5) |
| `06_credentials.sh` | GitHub, Anthropic and a dummy echo-server provider: placeholders in the environment, a push to a throwaway branch, credential binding per endpoint, `credential_endpoint_mismatch` and `request_authority_mismatch` |
| `07_getting_out.sh` | Instance metadata, the Pod Identity agent, the kubelet, the Kubernetes API, direct IPs, other node ports, UDP: which layer stops each |
| `08_rotation.sh` | Provider update while processes run, detach, delete |
| `09_prover.sh` | `openshell-prover check` against a boundary policy, as in CI (no cluster needed) |
| `10_logs.sh` | OCSF events as JSON from the supervisor Pod's log |
| `99_cleanup.sh` | Deletes the sandboxes, providers and profiles these scripts create, the push override, and the echo server |

```bash
./00_echo.sh
./01_deny_and_dns.sh
./02_binaries.sh
./03_l7_rules.sh
./04_live_updates.sh
./05_tls.sh
(set -a; . ./.env; set +a; ./06_credentials.sh)   # GITHUB_TOKEN, GITHUB_REPO, ANTHROPIC_API_KEY
./07_getting_out.sh
./08_rotation.sh
./09_prover.sh
./10_logs.sh
./99_cleanup.sh
```

`06_credentials.sh` prints placeholders, HTTP codes and the echo server's dummy token, never a
credential, and masks your repository's name. It pushes a branch named `openshell-demo-<timestamp>`
to `GITHUB_REPO` and deletes it again (also on exit, if interrupted).

## Files

- `policies/`: one full policy per test. Each keeps the default policy's filesystem and Landlock
  sections, because a running sandbox refuses a policy that removes a path or changes Landlock.
  `github-push.yaml.tmpl` is rendered with your `GITHUB_REPO` into `logs/`.
- `profiles/`: provider profiles. The gateway ships none; the scripts import these.
  `github.yaml` and `anthropic.yaml` are copies of OpenShell v0.1.6's examples with `binaries`
  adjusted to the series' workload image; `echo-demo.yaml` binds a dummy token to the echo server.
- `prover/`: a boundary policy and four candidates for `09_prover.sh`.

## What I measured

Results from the recorded runs are in the blog post.
