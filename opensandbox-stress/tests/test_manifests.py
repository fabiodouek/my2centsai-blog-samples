"""The manifests render with every placeholder filled, parse as YAML, and carry this directory's
labels; echo-stress runs part 4's echo program at the size the ladder needs; the VM sampler's
shell script works in busybox (when Docker and the busybox image are on this machine)."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from measure import render_template

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = ROOT / "manifests"
REGISTRY = {"ECR_REGISTRY": "123456789012.dkr.ecr.us-east-1.amazonaws.com", "ECR_PREFIX": "opensandbox-eks"}
VALUES = {
    "pool.yaml": {"POOL_NAME": "osb-stress-pool", "CPU": "250m", "MEMORY": "512Mi", "BUFFER_MIN": "40",
                  "BUFFER_MAX": "40", "POOL_MAX": "40"},
    "api-load.yaml": {"LEVELS": "1,4,16,64", "DURATION": "20", "IDS": "a,b"},
    "echo-stress.yaml": {},
    "vmm-sampler.yaml": {"NAME": "osb-stress-vmm-node-a", "NODE": "ip-10-0-0-7.ec2.internal", "EVERY": "5"},
    "latency-client.yaml": {},
}
BUSYBOX = "public.ecr.aws/docker/library/busybox:stable@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"


def docs(name: str) -> list[dict]:
    text = render_template((MANIFESTS / name).read_text(), {**REGISTRY, **VALUES[name]}, name)
    return [d for d in yaml.safe_load_all(text) if d]


@pytest.mark.parametrize("name", sorted(VALUES))
def test_renders_and_is_labelled(name):
    for doc in docs(name):
        labels = doc["metadata"].get("labels", {})
        assert labels.get("example") == "opensandbox-stress", f"{doc['kind']} {doc['metadata']['name']}"


def test_every_manifest_is_covered():
    assert sorted(p.name for p in MANIFESTS.glob("*.yaml")) == sorted(VALUES)


def test_echo_stress_size_and_names():
    by_kind = {d["kind"]: d for d in docs("echo-stress.yaml")}
    assert set(by_kind) == {"ConfigMap", "Deployment", "Service"}
    deployment = by_kind["Deployment"]
    assert deployment["spec"]["replicas"] == 3
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["resources"] == {"requests": {"cpu": "500m", "memory": "256Mi"},
                                      "limits": {"cpu": "1", "memory": "256Mi"}}
    assert container["command"] == ["python3", "-u", "/app/echo.py"]
    assert container["image"].endswith("/opensandbox-eks/python:3.12-slim")
    service = by_kind["Service"]
    assert service["metadata"]["name"] == "echo-stress" and service["spec"]["ports"][0]["port"] == 80
    for doc in by_kind.values():
        assert doc["metadata"]["labels"]["app"] == "echo-stress"


@pytest.mark.skipif(not (ROOT.parent / "opensandbox-egress" / "manifests" / "echo-server.yaml").is_file(),
                    reason="part 4's directory is not next to this one")
def test_echo_stress_runs_part_4s_program():
    part4 = [d for d in yaml.safe_load_all((ROOT.parent / "opensandbox-egress" / "manifests" / "echo-server.yaml").read_text()
                                           .replace("${ECR_REGISTRY}", "r").replace("${ECR_PREFIX}", "p")) if d]
    theirs = next(d for d in part4 if d["kind"] == "ConfigMap")["data"]["echo.py"]
    ours = next(d for d in docs("echo-stress.yaml") if d["kind"] == "ConfigMap")["data"]["echo.py"]
    assert ours == theirs


def test_vmm_sampler_pod():
    [pod] = docs("vmm-sampler.yaml")
    spec = pod["spec"]
    assert spec["hostPID"] is True and spec["nodeName"] == "ip-10-0-0-7.ec2.internal"
    container = spec["containers"][0]
    assert container["image"] == BUSYBOX and container["securityContext"] == {"privileged": True}
    assert container["volumeMounts"] == [{"name": "run", "mountPath": "/host-run", "readOnly": True}]
    assert pod["metadata"]["labels"] == {"app": "osb-stress-vmm", "example": "opensandbox-stress"}


def _busybox_here() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", BUSYBOX], capture_output=True).returncode == 0


@pytest.mark.skipif(not _busybox_here(), reason="needs Docker and the busybox image already pulled")
def test_vmm_sampler_script_in_busybox(tmp_path):
    """The sampler's own script, one pass, against a stand-in VM: a process whose executable path
    says qemu-system and whose command line carries a sandbox ID, and an OCI bundle naming its pod."""
    sid = "c" * 64
    bundle = tmp_path / "bundles" / "k8s.io" / sid
    bundle.mkdir(parents=True)
    (bundle / "config.json").write_text('{"annotations": {"io.kubernetes.cri.sandbox-name":"' +
                                        "11111111-1111-4111-8111-111111111111-0" + '"}}')
    [pod] = docs("vmm-sampler.yaml")
    script = pod["spec"]["containers"][0]["args"][0]
    one_pass = script.replace('sleep "$EVERY"', "exit 0")
    stand_in = (f"mkdir -p /tmp/qemu-system-x86_64/{sid} && cp /bin/busybox /tmp/qemu-system-x86_64/{sid}/sleep && "
                f"/tmp/qemu-system-x86_64/{sid}/sleep 30 & sleep 0.5; ")
    done = subprocess.run(["docker", "run", "--rm", "-e", "EVERY=1",
                           "-v", f"{tmp_path / 'bundles'}:/host-run/containerd/io.containerd.runtime.v2.task:ro",
                           BUSYBOX, "sh", "-c", stand_in + one_pass], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    lines = [line for line in done.stdout.splitlines() if line.startswith("vmm ")]
    assert len(lines) == 1, done.stdout
    _, t, kind, pid, rss, found_sid, found_pod = lines[0].split()
    assert kind == "qemu" and found_sid == sid and found_pod == "11111111-1111-4111-8111-111111111111-0"
    assert int(rss) > 0 and int(t) > 0 and int(pid) > 0


def test_latency_client_pod():
    [pod] = docs("latency-client.yaml")
    assert pod["metadata"]["namespace"] == "opensandbox-system"
    assert pod["metadata"]["labels"] == {"app": "osb-stress-client", "example": "opensandbox-stress"}
    container = pod["spec"]["containers"][0]
    assert "-r /app/client-requirements.txt" in container["args"][0]
    env = {e["name"]: e for e in container["env"]}
    assert env["OSB_SERVER_DOMAIN"]["value"] == "opensandbox-server.opensandbox-system.svc:80"
    assert env["OSB_API_KEY"]["valueFrom"]["secretKeyRef"] == {"name": "opensandbox-server-auth", "key": "api-key"}
    assert pod["spec"]["automountServiceAccountToken"] is False


def test_common_pins_the_same_busybox():
    assert BUSYBOX in (ROOT / "common.py").read_text()


def test_client_requirements_are_pinned_hashed_and_match_the_lock():
    """The client pod (it holds the API key) installs only exact versions with hashes, and the
    packages it shares with this directory's uv.lock are the lock's versions."""
    import re
    import tomllib

    lines = [line for line in (ROOT / "loadgen" / "client-requirements.txt").read_text().splitlines()
             if line.strip() and not line.startswith("#")]
    entries, hashes = [], {}
    for line in lines:
        match = re.match(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)", line)
        if match:
            entries.append((match.group(1), match.group(2)))
            hashes[match.group(1)] = 0
        elif "--hash=sha256:" in line:
            hashes[entries[-1][0]] += 1
    assert ("opensandbox", "1.2.0") in entries
    assert all(count >= 1 for count in hashes.values()), hashes
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked = {p["name"]: p["version"] for p in lock["package"]}
    for name, version in entries:
        if name in locked:
            assert locked[name] == version, f"{name}: client {version}, uv.lock {locked[name]}"
    command = docs("latency-client.yaml")[0]["spec"]["containers"][0]["args"][0]
    assert "--require-hashes" in command and "--no-deps" in command and "--only-binary=:all:" in command
