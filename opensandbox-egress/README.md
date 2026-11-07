# OpenSandbox egress and Credential Vault on Amazon EKS

Companion code for part 4 of the my2cents.ai series "OpenSandbox on Amazon EKS". It runs on the cluster built by `../opensandbox-eks` and shows what the egress sidecar does: deny-by-default network rules, how clients trust the proxy's CA, and the Credential Vault, which injects real keys into requests so that the sandbox only ever holds placeholders. It also tries to get a credential out and records what happens.

Video walkthrough: https://youtu.be/w2Ihz6QEcp8

## Requirements

- Every script imports `../opensandbox-eks/common.py` (server address, API key, `env.sh`) **and** `../opensandbox-lifecycle/common.py` (the port-forward keeper, `ensure_port_forward()`), so both directories must sit next to this one.
- The cluster from `../opensandbox-eks`: run `01_cluster.sh` to `07_first_sandbox.py` there first. Its `env.sh` (account-specific names, written by `01_cluster.sh`) and its `common.py` are used from here, and the server is expected in `[egress] mode = "dns+nft"`, which `04_install.sh` sets.
- Docker with buildx, `envsubst`, `kubectl`, the AWS CLI v2 and [`uv`](https://docs.astral.sh/uv/).
- For `04_vault_services.py` only: a secrets file with `GITHUB_TOKEN` (fine-grained, push access to a private test repo), `GITHUB_REPO` (`owner/name` of that repo) and `ANTHROPIC_API_KEY`. The scripts read it with a line parser (it is never `source`d) from the path in `OSB_SECRETS_ENV`, default `~/.config/opensandbox-eks/secrets.env`. Only those three names are read. See `.env.example`.
- For `08_pause_wipe.py`: pause and resume configured in `../opensandbox-lifecycle` (`00_snapshot_config.sh`).

The scripts start the `kubectl port-forward` to the lifecycle server on `127.0.0.1:18080` themselves if it is not running (18080, not 8080: something else on a laptop often owns 8080 and answers 404). To run it yourself: `kubectl -n opensandbox-system port-forward svc/opensandbox-server 18080:80`.

## Run order

```bash
./00_image.sh      # build the workload image (python + curl, git, Node, AWS CLI v2) and push it to ECR
./00_echo.sh       # deploy the in-cluster echo server and probe it
uv run python 01_sidecar.py
uv run python 02_rules.py
uv run python 03_ca_trust.py
uv run python 04_vault_services.py   # real credentials: GitHub and Anthropic
uv run python 05_substitutions.py
uv run python 06_leaks.py
uv run python 07_rotation.py
uv run python 08_pause_wipe.py
```

Each script creates its own sandboxes, tags them with the metadata `example=opensandbox-egress`, and deletes them at the end (also when it fails). `uv run python 98_cleanup.py` deletes any left over from an interrupted run. Nothing here touches sandboxes from other directories. Exit code 1 means a check failed.

## The scripts

| Script | What it shows |
|---|---|
| `00_image.sh` | Builds `image/Dockerfile` for `linux/amd64` and pushes it as `<registry>/opensandbox-eks/egress-tools:1`. The base python image has no `curl`, `git`, Node or AWS CLI, which the CA-trust comparison needs. |
| `00_echo.sh` | Applies `manifests/echo-server.yaml`: a Python echo server in the `opensandbox` namespace that answers any request with its method, path, headers and body as JSON. Two Services (`echo`, `echo-alt`) on port 80 reach two listeners of the one pod. |
| `01_sidecar.py` | A sandbox without a `networkPolicy` has one container and no egress control. One with a policy and `credentialProxy` gets the egress sidecar. |
| `02_rules.py` | FQDN, wildcard (matches `api.github.com`, not `github.com`), CIDR and port-scoped rules; a runtime `patch_egress_rules`; and a connection that stays open after the policy no longer allows its host (#2329). |
| `03_ca_trust.py` | curl, Python `requests` and Node trust the proxy's CA out of the box. The AWS CLI v2 does too, but only because the bootstrap sets `REQUESTS_CA_BUNDLE`: it ignores the OS trust store and `SSL_CERT_FILE`, and honors `REQUESTS_CA_BUNDLE` and `AWS_CA_BUNDLE`. The script changes one setting at a time and prints each result and the exact failure. |
| `04_vault_services.py` | **Real credentials.** A `bearer` binding for the GitHub API, a `basic` binding for `git clone` and `git push` (to a throwaway branch `osb-test-<run id>`, deleted afterwards), an `apiKey` binding for the Anthropic Messages API. The sandbox holds placeholders only. |
| `05_substitutions.py` | The five auth types and placeholder substitution in the path, query, header and body, against the echo server with dummy values. |
| `06_leaks.py` | Tries to read a credential from env, `/proc` and files, reach the sidecar API, the instance metadata service and the Pod Identity agent, dial a bare IP, and spoof the `Host` header (#1758). Dummy credentials only. Prints the verdict `LEAK` or `no leak` for the spoof. |
| `07_rotation.py` | `patch` with the right and a stale `expectedRevision` (the script records the conflict status the sidecar returns), the new value taking effect mid-session, and `delete` as revocation. |
| `08_pause_wipe.py` | On Kubernetes pause and resume wipe the vault; the trusted caller re-creates it. Shows the sidecar's `applied binding=` audit lines (names only) and that they reach the container log in batches, not one by one. |

## Which credentials are real

Only `04_vault_services.py` uses real credentials, and the real values go only into the vault, never into a sandbox. The CA trust, leak, Host-spoof, substitution, rotation and pause tests (`03` and `05` to `08`) use made-up `dummy-...` values against the echo server. No script prints a credential value: output is passed through a scrubber that replaces the GitHub token and the Anthropic key, and their base64 forms, with `<redacted>`.

## Cleanup

```bash
uv run python 98_cleanup.py   # this directory's sandboxes only
./00_echo.sh delete                                          # the echo server
```

The workload image stays in ECR with the rest of the cluster's images; `../opensandbox-eks/99_teardown.sh` removes everything.
