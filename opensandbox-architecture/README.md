# OpenSandbox architecture: a local server, the Python SDK and the `osb` CLI

Companion code for [this blog post](https://www.my2cents.ai/deep-dive/opensandbox-architecture/), a deep dive
on OpenSandbox's architecture. It runs the OpenSandbox 1.2.0 server on your machine with the Docker
runtime, then drives it three ways: the Python SDK, the `osb` command line, and a runtime egress policy.

Video walkthrough: https://youtu.be/DNwFKjH1xvY

This is part 1 of a six-part series, "OpenSandbox on Amazon EKS". Part 2 runs the same server on a
cluster.

## Requirements

- Docker (Docker Desktop on macOS is what these scripts were run on)
- [`uv`](https://docs.astral.sh/uv/) (it installs Python, the SDK and the CLI on first use)
- `curl` (the shell scripts use it to check the server)
- Network access to Docker Hub and PyPI, and about 1.1 GB of disk for the three images on arm64
- Nothing else: no API keys. The one key in play, `local-dev-key`, is the throwaway value in `sandbox.toml`.

## Scripts

| Script | What it shows |
|---|---|
| `sandbox.toml` | The server config: the packaged Docker example with seven lines changed (API key, `execd` and egress images pinned to `release-1.2.0`, egress mode `dns+nft`, a local SQLite path, `resolve_internal = false` for macOS, `publish_host = "127.0.0.1"`) |
| `00_server.sh` | Diffs the packaged example against `sandbox.toml` (its `execd` and egress image tags and egress mode are stale for 1.2.0), pulls the images, starts the server in the background and waits for `/health`. Run it twice and it says the server is already running |
| `01_first_sandbox.py` | Python SDK: create a sandbox from `python:3.12-slim`, run commands, write and read back a file, read its metrics, kill it. Prints the create time |
| `02_cli.sh` | The same server through `osb`: config, create, list, run a command, write and read a file, metrics, kill |
| `03_network_policy.py` | Default-deny egress with `pypi.org` allowed at creation and `github.com` allowed while the sandbox runs, the policy printed before and after. It checks the three results and exits 1 if one differs |
| `99_cleanup.sh` | Kills every sandbox (all pages of the list), stops the server, removes leftover sandbox containers. If a step fails it still does the rest, then exits 1 |
| `common.py` | Connection settings and two helpers used by the Python scripts |

## Run it

```bash
./00_server.sh
uv run python 01_first_sandbox.py
./02_cli.sh
uv run python 03_network_policy.py
./99_cleanup.sh
```

The Python scripts and `02_cli.sh` print one line, and exit 1, if the server is not running or rejects the API key.

`common.py` and the shell scripts read `OSB_DOMAIN` (default `127.0.0.1:8080`) and `OSB_API_KEY`
(default `local-dev-key`). If something else already uses port 8080, change `port` in `sandbox.toml`
and set `OSB_DOMAIN` to match.

## Notes

- The server listens on `127.0.0.1` only, and so do the ports Docker publishes for each sandbox (its
  `execd` agent and proxy). The packaged example leaves `publish_host` commented out, which publishes
  those ports on every network interface, so `sandbox.toml` here sets `publish_host = "127.0.0.1"`:
  nothing else on your network can reach a sandbox's command endpoint.
- `python:3.12-slim` has no `curl`, so the sandbox checks URLs with a small Python script.
- `osb.toml`, `server.log`, `server.pid` and `opensandbox.db` are created next to the scripts and
  are gitignored.
- `OSB_PAGE_SIZE` (default 100) sets how many sandboxes `99_cleanup.sh` lists per page; lower it to
  see it follow several pages.

## Cleanup

```bash
./99_cleanup.sh
```

It is safe to run when nothing is running. It removes every container that carries an
`opensandbox.io/id` or `opensandbox.io/egress-sidecar-for` label, so it also clears sandboxes left
behind by a server that crashed. The Docker images stay; remove them with `docker rmi` if you want
the disk back.
