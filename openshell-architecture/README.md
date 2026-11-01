# OpenShell architecture: a local run

Companion code for part 1 of the my2cents.ai series "OpenShell on Amazon EKS": the OpenShell
gateway running locally on an Apple Silicon Mac, driving Docker Desktop, exercised with the CLI
and the Python SDK. Everything is pinned to OpenShell **0.1.6**.

Video walkthrough: https://youtu.be/NoI-68gMmtg

## Requirements

- An Apple Silicon Mac with [Homebrew](https://brew.sh) and Docker Desktop running
- [`uv`](https://docs.astral.sh/uv/) for the Python script
- No existing OpenShell 0.0.x install. 0.0.x can't be upgraded in place: delete its sandboxes,
  then `brew services stop nvidia/openshell/openshell && brew uninstall nvidia/openshell/openshell
  && rm -rf "$(brew --prefix)/var/openshell"` (the official upgrade notes say the same).
  `00_gateway.sh` stops with a message if it finds one.

On Linux, the same scripts work with the Debian/RPM packages that `install.sh` installs; the
gateway then runs as a systemd user service and reads `~/.config/openshell/gateway.env`.

## Scripts

| Script | What it shows |
|---|---|
| `00_gateway.sh` | Installs the CLI and gateway pinned to 0.1.6, telemetry off, Docker supervisor in bridge mode; prints two pinning traps; checks telemetry is off in the running gateway; builds `openshell-workload:0.1.6` |
| `01_first_sandbox.sh` | Test 1: create a sandbox from the CLI, inspect it, run commands, upload and download a file, read its logs, delete it |
| `02_sdk.py` | Test 2: the same lifecycle from the Python SDK, with phase changes timestamped |
| `03_network_policy.sh` | Test 3: deny by default. `curl` may reach `pypi.org`, `python3` may not, `httpbin.org` is blocked until a live policy update allows it for `curl`; the deny lines from the logs |
| `99_cleanup.sh` | Deletes every sandbox on the active gateway (`--stop` also stops the gateway) |

```bash
./00_gateway.sh
./01_first_sandbox.sh
uv run python 02_sdk.py
./03_network_policy.sh
./99_cleanup.sh
```

## Configuration files

| File | Why |
|---|---|
| `gateway.toml` | Installed at `~/.config/openshell/gateway.toml` if you don't have one. Sets the Docker driver's `supervisor_network_mode = "bridge"`. The default host mode points each supervisor container at `127.0.0.1:17670`, which on Docker Desktop is the Docker VM unless Docker Desktop's host networking setting is on; sandboxes then fail with "Startup configuration fetch failed" |
| `~/.config/openshell/gateway.env` | `00_gateway.sh` adds `OPENSHELL_TELEMETRY_ENABLED=false`. The Homebrew service sources this file before it starts the gateway, and the gateway passes the setting to every supervisor. Telemetry is on by default |
| `workload/Dockerfile` | The NVIDIA Ubuntu 24.04 base that 0.1.x uses by default, pinned by digest, plus `curl`, `python3`, `git`, `jq`. It ends with `USER 1000:1000`: OpenShell refuses a workload that would run as UID or GID 0 |
| `policies/curl-only.yaml` | One network rule: `/usr/bin/curl` may reach `pypi.org:443`. Filesystem and Landlock sections are locked at create; the network section can change live |

## Telemetry

OpenShell sends anonymous usage telemetry unless `OPENSHELL_TELEMETRY_ENABLED=false` is set on
the gateway. `00_gateway.sh` sets it and then reads the running gateway's environment to check:

```bash
ps eww -p "$(pgrep -f bin/openshell-gateway | head -1)" | tr ' ' '\n' | grep OPENSHELL_TELEMETRY
```

## Cleanup

```bash
./99_cleanup.sh          # delete every sandbox
./99_cleanup.sh --stop   # and stop the gateway
```

To remove OpenShell entirely, use the uninstall commands under Requirements.
