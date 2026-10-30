"""Shared helpers for the AX credential-injection scripts.

Drives the `ax` CLI and Substrate's `kubectl-ate` built by ../google-ax-eks,
plus kubectl and docker. The cluster from the install post must be running.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent / "google-ax-eks"
AX_BIN = INSTALL / ".src" / "ax" / "bin" / "ax"
KATE_BIN = INSTALL / ".src" / "bin" / "kubectl-ate"
RECORDS = HERE / ".records"
RECORDS.mkdir(exist_ok=True)

# docker pushes to ECR through the credential helper from the install post.
os.environ["DOCKER_CONFIG"] = str(INSTALL / ".records" / "docker")
os.environ["PATH"] = f"{INSTALL / 'scripts'}:{os.environ['PATH']}"

ATESPACE = "creds"          # every task these scripts create lives here
SECRETS_NS = "ax-creds"     # Kubernetes namespace holding the real credentials
PLACEHOLDER = "placeholder-not-a-secret"


def _tf(name: str) -> str:
    return subprocess.run(["terraform", f"-chdir={INSTALL / 'terraform'}", "output", "-raw", name],
                          check=True, capture_output=True, text=True).stdout


CONTEXT = _tf("cluster_name")
IMAGE_REPO = _tf("image_repo")
SNAPSHOT_LOCATION = _tf("snapshot_location")
REGION = _tf("region")


def load_env() -> dict:
    """ANTHROPIC_API_KEY, GITHUB_TOKEN, GITHUB_REPO from .env. Never printed."""
    env = {}
    for line in (HERE / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"')
    missing = [k for k in ("ANTHROPIC_API_KEY", "GITHUB_TOKEN", "GITHUB_REPO") if not env.get(k)]
    if missing:
        sys.exit(f".env is missing {missing}")
    return env


def banner(text: str) -> None:
    print(f"\n\033[1m== {text}\033[0m", flush=True)


def run(cmd: list[str], check: bool = True, quiet: bool = False, input: str | None = None):
    p = subprocess.run(cmd, capture_output=True, text=True, input=input)
    if not quiet:
        out = (p.stdout + p.stderr).rstrip()
        if out:
            print(out, flush=True)
    if check and p.returncode != 0:
        sys.exit(f"command failed ({p.returncode}): {' '.join(cmd[:6])}")
    return p


def ax(*args, check=True, quiet=False, input=None):
    return run([str(AX_BIN), "--context", CONTEXT, "-n", "ax-system", "-a", ATESPACE, *args],
               check=check, quiet=quiet, input=input)


def kubectl(*args, check=True, quiet=False, input=None):
    return run(["kubectl", "--context", CONTEXT, *args], check=check, quiet=quiet, input=input)


def kate(*args, check=True, quiet=False, input=None):
    """Substrate's own CLI. AX creates no egress policy, so this is how a task gets one."""
    return run([str(KATE_BIN), "--context", CONTEXT, *args], check=check, quiet=quiet, input=input)


def image() -> str:
    return (RECORDS / "image.txt").read_text().strip()


def egress_ca() -> str:
    return (RECORDS / "egress-ca.pem").read_text()


def task_manifest(name: str, env: dict | None = None, trust_gateway: bool = True) -> str:
    """A task on our image. Its command is the CA wrapper around `sleep infinity`;
    the work happens over `ax ssh`. Only placeholders go into spec.env."""
    entries = dict(env or {})
    if trust_gateway:
        entries["EGRESS_CA_PEM"] = egress_ca()
    spec = {
        "apiVersion": "ax.io/v1alpha1", "kind": "Task",
        "metadata": {"name": name, "atespace": ATESPACE},
        "spec": {
            "image": image(),
            "debug": True,
            "command": ["/usr/local/bin/egress-ca", "sleep", "infinity"],
            "resources": {"limits": {"cpu": "1", "memory": "2Gi"}},
            "env": [{"name": k, "value": v} for k, v in entries.items()],
        },
    }
    return json.dumps(spec)  # JSON is valid YAML


def create_task(name: str, env: dict | None = None, trust_gateway: bool = True) -> None:
    ax("delete", "task", name, check=False, quiet=True)
    ax("apply", "-f", "-", input=task_manifest(name, env, trust_gateway), quiet=True)
    ax("resume", "task", name, quiet=True)


def set_policy(name: str, rules: list[dict]) -> None:
    """Create or replace the task's EgressPolicy (the actor is named after the task)."""
    doc = json.dumps({"rules": rules})
    kate("delete", "egress-policy", name, "-a", ATESPACE, check=False, quiet=True)
    if rules:
        kate("create", "egress-policy", name, "-a", ATESPACE, "-f", "-", input=doc, quiet=True)
    time.sleep(6)  # DNS allow-lists follow a policy change within about 5 s


def inject(host: str, header: str, secret: str, key: str, prefix: str = "") -> dict:
    """An https rule for one host that swaps one header for a Kubernetes Secret value."""
    entry = {"header": header, "credentialUri": f"ate-secret://k8s.io/default/{SECRETS_NS}/{secret}/{key}"}
    if prefix:
        entry["prefix"] = prefix
    return {"https": {"hostnames": [host], "effects": {"replaceHeaders": [entry]}}}


def https(*hosts: str) -> dict:
    return {"https": {"hostnames": list(hosts)}}


def sh(name: str, script: str, check: bool = False) -> str:
    p = ax("ssh", name, "--", "bash", "-lc", script, check=check, quiet=True)
    return (p.stdout + p.stderr).rstrip()


def show(name: str, script: str) -> None:
    print(f"$ {script}")
    print(sh(name, script))


def gateway_denials(since: str = "2m") -> list[str]:
    log = kubectl("-n", "ate-system", "logs", "deployment/atenet-egress", "-c", "ext-proc",
                  f"--since={since}", quiet=True, check=False).stdout
    return [l for l in log.splitlines() if "denied" in l.lower()]


def redact(text: str, *secrets: str) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "<REAL SECRET LEAKED>")
    return text


def stop_tunnel() -> None:
    run([str(AX_BIN), "tunnel", "stop", CONTEXT], check=False, quiet=True)
