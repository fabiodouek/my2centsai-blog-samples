"""Offline tests for server_config.py: no cluster, no AWS. Run from this directory:

    python3 -m unittest discover -s tests -v

kubectl, helm (except `helm template` and `helm show chart`, which run for real against part 2's
chart checkout, offline) and the upgrade itself are replaced by a fake release kept in memory.
The fake behaves like Helm here: a failed upgrade leaves the previous revision `deployed` and adds
a `failed` one; the server pod is replaced when configToml changes; the pod's mounted config.toml
is configToml plus the [ingress] section the chart appends.
"""

import fcntl
import importlib.util
import io
import json
import os
import shutil
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
REAL_EKS = HERE.parent / "opensandbox-eks"
REAL_CHART = REAL_EKS / ".src/OpenSandbox/manifests/charts/server"
REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
BASE_IMAGE = f"{REGISTRY}/opensandbox-eks/egress:release-1.2.0"
SIGV4_IMAGE = f"{REGISTRY}/opensandbox-eks/egress-sigv4@sha256:" + "ab" * 32
GW_HOST = "osb-gateway-0123456789abcdef.elb.us-east-1.amazonaws.com"

FIXTURE = Path(tempfile.mkdtemp(prefix="osb-sc-test-"))
(FIXTURE / "eks/values").mkdir(parents=True)
(FIXTURE / "eks/.src/OpenSandbox/manifests/charts").mkdir(parents=True)
(FIXTURE / "eks/.src/OpenSandbox/manifests/charts/server").symlink_to(REAL_CHART)
(FIXTURE / "eks/env.sh").write_text(
    f"export CLUSTER_NAME='osb-eks'\nexport AWS_REGION='us-east-1'\nexport ECR_REGISTRY='{REGISTRY}'\n"
    "export ECR_PREFIX='opensandbox-eks'\nexport KUBE_CONTEXT='test-ctx'\n")
shutil.copy(REAL_EKS / "values/server.yaml", FIXTURE / "eks/values/server.yaml")
GATEWAY_FILE = FIXTURE / "eks/values/server-gateway.yaml"
GATEWAY_TEXT = f"server:\n  gateway:\n    enabled: true\n    host: {GW_HOST}\n    gatewayRouteMode: header\n"
os.environ["OSB_EKS_DIR"] = str(FIXTURE / "eks")
os.environ["OSB_LIFECYCLE_LOGS"] = str(FIXTURE / "logs")

_spec = importlib.util.spec_from_file_location("server_config", HERE / "server_config.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)
REAL_RUN = sc.run


def base_toml(image: str = BASE_IMAGE, mode: str = "dns+nft", secure: str | None = None, drop: str | None = None) -> str:
    sc.load_env()
    text = sc.base_config_toml(sc.rendered_server_yaml())
    text = text.replace(f'image = "{BASE_IMAGE}"', f'image = "{image}"').replace('mode = "dns+nft"', f'mode = "{mode}"')
    if drop:
        text = "\n".join(line for line in text.splitlines() if not line.startswith(drop + " =")) + "\n"
    if secure:
        text += f'\n[secure_runtime]\ntype = "kata"\nk8s_runtime_class = "{secure}"\n'
    return text


def values(config: str, gateway: bool = True) -> dict:
    out = {"configToml": config, "server": {"image": {"tag": "release-1.2.0"}}}
    if gateway:
        out["server"]["gateway"] = {"enabled": True, "host": GW_HOST, "gatewayRouteMode": "header"}
    return out


class FakeRelease:
    """An in-memory Helm release plus the kubectl answers server_config.py asks for."""

    def __init__(self, first: dict):
        self.history = [{"revision": 1, "status": "deployed", "values": first}]
        self.pod_uid = "uid-1"
        self.upgrades: list[list[str]] = []
        self.fail_next_upgrade = False
        self.on_history_call = None          # hook: called on each `helm history`
        self.mounted_override = None         # a config.toml string the "pod" reports instead
        self.history_calls = 0

    def deployed(self) -> dict:
        return [h for h in self.history if h["status"] == "deployed"][-1]

    def mounted(self) -> str:
        if self.mounted_override is not None:
            return self.mounted_override
        current = self.deployed()["values"]
        gateway = (current.get("server") or {}).get("gateway") or {}
        ingress = (f'[ingress]\nmode = "gateway"\n\ngateway.address = "{gateway["host"]}"\ngateway.route.mode = "header"\n'
                   if gateway.get("enabled") else '[ingress]\nmode = "direct"\n')
        return current["configToml"] + "\n" + ingress

    def run(self, cmd, *, check=True, stdin=None, timeout=120):
        if cmd[0] == "helm" and ("template" in cmd or ("show" in cmd and "chart" in cmd)):
            return REAL_RUN(cmd, check=check, stdin=stdin, timeout=timeout)
        if cmd[0] == "jq":
            return '{"batchsandbox-template.yaml":"x"}\n'
        joined = " ".join(cmd)
        if cmd[0] == "helm":
            if " history " in f" {joined} ":
                self.history_calls += 1
                if self.on_history_call:
                    self.on_history_call(self)
                return json.dumps([{"revision": h["revision"], "status": h["status"]} for h in self.history])
            if " get values " in f" {joined} ":
                revision = int(cmd[cmd.index("--revision") + 1])
                return json.dumps(next(h["values"] for h in self.history if h["revision"] == revision))
            if " get manifest " in f" {joined} ":
                revision = int(cmd[cmd.index("--revision") + 1])
                return json.dumps(next(h["values"] for h in self.history if h["revision"] == revision), sort_keys=True)
        if cmd[0] == "kubectl":
            if "/readyz" in cmd:
                return "ok"
            if "runtimeclass" in cmd:
                return f"runtimeclass.node.k8s.io/{cmd[cmd.index('runtimeclass') + 1]}"
            if "secret" in cmd:
                return "53783"
            if "configmap" in cmd:
                return '{"data": {"batchsandbox-template.yaml": "x"}}'
            if "pods" in cmd and "-o" in cmd and "json" in cmd:
                return json.dumps({"items": [{"metadata": {"name": "opensandbox-server-" + self.pod_uid,
                                                           "uid": self.pod_uid},
                                              "status": {"containerStatuses": [{"ready": True}]}}]})
            if "exec" in cmd and "cat" in cmd:
                return self.mounted()
            if "exec" in cmd:
                return '{"status":"healthy"}\n{"version":"1.2.0"}\n'
            return ""
        raise AssertionError(f"unexpected command {cmd}")

    def upgrade(self, cmd: list[str]) -> int:
        self.upgrades.append(cmd)
        files = [Path(cmd[i + 1]) for i, arg in enumerate(cmd) if arg == "-f"]
        config = json.loads(next(f for f in files if f.name == "config.json").read_text())["configToml"]
        new = values(config, gateway=any(f.name == "server-gateway.yaml" for f in files))
        revision = self.history[-1]["revision"] + 1
        if self.fail_next_upgrade:
            self.history.append({"revision": revision, "status": "failed", "values": new})
            return 1
        old = self.deployed()
        old["status"] = "superseded"
        if old["values"]["configToml"] != config:
            self.pod_uid = f"uid-{revision}"
        self.history.append({"revision": revision, "status": "deployed", "values": new})
        return 0


class ServerConfigTest(unittest.TestCase):
    def setUp(self):
        GATEWAY_FILE.write_text(GATEWAY_TEXT)
        self.release = FakeRelease(values(base_toml()))
        patches = [mock.patch.object(sc, "run", self.release.run),
                   mock.patch.object(sc, "run_upgrade", self.release.upgrade),
                   mock.patch.object(sc, "check_image", lambda image: None),
                   mock.patch.object(sc.shutil, "which", lambda tool: "/usr/bin/" + tool),
                   mock.patch.object(sc.time, "sleep", lambda seconds: None)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def main(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            try:
                code = sc.main(list(argv))
            except SystemExit as stop:
                code = stop.code if isinstance(stop.code, int) else 1
        return code, out.getvalue()

    def deployed_config(self) -> dict:
        return tomllib.loads(self.release.deployed()["values"]["configToml"])

    # --- composition ---------------------------------------------------------------------------------

    def test_noop_rebuild_keeps_everything(self):
        code, out = self.main("--runtime", "runc")
        self.assertEqual(code, 0, out)
        self.assertIn("manifests identical", out)
        self.assertIn("server pod opensandbox-server-uid-1 unchanged", out)
        self.assertTrue(any(str(GATEWAY_FILE) in cmd for cmd in self.release.upgrades[0]))

    def test_runtime_change_keeps_a_non_base_egress_image(self):
        self.release = FakeRelease(values(base_toml(image=SIGV4_IMAGE)))
        with mock.patch.object(sc, "run", self.release.run), mock.patch.object(sc, "run_upgrade", self.release.upgrade):
            code, out = self.main("--runtime", "kata-qemu")
        self.assertEqual(code, 0, out)
        config = self.deployed_config()
        self.assertEqual(config["egress"]["image"], SIGV4_IMAGE)
        self.assertEqual(config["secure_runtime"]["k8s_runtime_class"], "kata-qemu")
        self.assertIn("replaced", out)

    def test_baseline_restores_part_2(self):
        self.release.history[0]["values"] = values(base_toml(image=SIGV4_IMAGE, mode="dns", secure="kata-clh"))
        code, out = self.main("--baseline")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.release.deployed()["values"]["configToml"], base_toml())

    def test_dry_run_upgrades_nothing(self):
        code, out = self.main("--egress-mode", "dns", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn('+mode = "dns"', out)
        self.assertIn("composed values checked with helm template", out)
        self.assertEqual(self.release.upgrades, [])

    # --- I1: state from the last deployed revision; missing keys fail loudly -----------------------

    def test_failed_revision_is_not_taken_as_live_state(self):
        # Revision 2 tried a broken image and failed: Helm leaves revision 1 deployed.
        self.release.history.append({"revision": 2, "status": "failed",
                                     "values": values(base_toml(image=f"{REGISTRY}/opensandbox-eks/egress:broken"))})
        code, out = self.main("--runtime", "kata-qemu")
        self.assertEqual(code, 0, out)
        self.assertIn("revision 2 is failed", out)
        self.assertEqual(self.deployed_config()["egress"]["image"], BASE_IMAGE)

    def test_show_reads_the_deployed_revision(self):
        self.release.history.append({"revision": 2, "status": "failed",
                                     "values": values(base_toml(secure="kata-qemu"))})
        code, out = self.main("--show", "--json")
        self.assertEqual(code, 0, out)
        shown = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((shown["runtime"], shown["revision"], shown["ingress"]), ("runc", 1, "gateway"))

    def test_missing_egress_key_is_not_carried_forward_as_empty(self):
        self.release.history[0]["values"] = values(base_toml(drop="mode"))
        code, out = self.main("--runtime", "kata-qemu")
        self.assertEqual(code, 1, out)
        self.assertIn("has no [egress] mode to keep: pass --egress-mode", out)
        self.assertEqual(self.release.upgrades, [])

    def test_pending_release_is_refused(self):
        self.release.history.append({"revision": 2, "status": "pending-upgrade", "values": values(base_toml())})
        code, out = self.main("--runtime", "runc")
        self.assertEqual(code, 1, out)
        self.assertIn("stuck in pending-upgrade", out)
        self.assertEqual(self.release.upgrades, [])

    def test_failed_upgrade_leaves_the_deployed_revision_as_the_state(self):
        self.release.fail_next_upgrade = True
        code, out = self.main("--egress-image", SIGV4_IMAGE)
        self.assertEqual(code, 1, out)
        self.assertIn("last deployed revision is still 1", out)
        self.release.fail_next_upgrade = False
        code, out = self.main("--runtime", "kata-qemu")       # a later call must not pick up the failed image
        self.assertEqual(code, 0, out)
        self.assertEqual(self.deployed_config()["egress"]["image"], BASE_IMAGE)

    # --- I2: a lock, and a re-read before the upgrade --------------------------------------------------

    def test_a_second_writer_waits_for_the_lock(self):
        (FIXTURE / "logs").mkdir(exist_ok=True)
        holder = open(FIXTURE / "logs/server_config.lock", "a+")
        fcntl.flock(holder, fcntl.LOCK_EX)
        holder.write("pid 99999, since 00:00:00\n")
        holder.flush()
        try:
            with mock.patch.object(sc, "LOCK_WAIT", 0.0):
                code, out = self.main("--runtime", "kata-qemu")
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        self.assertEqual(code, 1, out)
        self.assertIn("still holds", out)
        self.assertEqual(self.release.upgrades, [])

    def test_a_change_by_another_client_meanwhile_aborts_the_upgrade(self):
        def someone_else_upgrades(release):
            if release.history_calls == 2:   # after our first read, before our upgrade
                release.deployed()["status"] = "superseded"
                release.history.append({"revision": 2, "status": "deployed",
                                        "values": values(base_toml(image=SIGV4_IMAGE))})
        self.release.on_history_call = someone_else_upgrades
        code, out = self.main("--runtime", "kata-qemu")
        self.assertEqual(code, 1, out)
        self.assertIn("the release changed while this script ran (revision 1 -> 2", out)
        self.assertEqual(self.release.upgrades, [])
        self.assertEqual(self.deployed_config()["egress"]["image"], SIGV4_IMAGE)   # their change survives

    # --- I3: the gateway cannot be dropped by accident -------------------------------------------------

    def test_missing_gateway_file_with_a_live_gateway_is_refused(self):
        GATEWAY_FILE.unlink()
        code, out = self.main("--runtime", "kata-qemu")
        self.assertEqual(code, 1, out)
        self.assertIn("values/server-gateway.yaml is missing", out)
        self.assertEqual(self.release.upgrades, [])

    def test_drop_gateway_removes_it_on_purpose(self):
        GATEWAY_FILE.unlink()
        code, out = self.main("--runtime", "runc", "--drop-gateway")
        self.assertEqual(code, 0, out)
        self.assertNotIn("gateway", self.release.deployed()["values"]["server"])
        self.assertIn("ok    gateway removed (--drop-gateway)", out)

    def test_composed_values_without_the_gateway_are_refused(self):
        GATEWAY_FILE.write_text("server:\n  gateway:\n    enabled: false\n")   # 05_gateway.sh output gone wrong
        code, out = self.main("--runtime", "runc")
        self.assertEqual(code, 1, out)
        self.assertIn("refusing to upgrade", out)
        self.assertIn("does not enable one with a host", out)
        self.assertEqual(self.release.upgrades, [])

    def test_composed_values_are_rendered_and_checked_before_the_upgrade(self):
        # A gateway file whose host the chart does not render as given: caught by helm template, not after.
        with mock.patch.object(sc, "problems_with", side_effect=[["composed values: [ingress] mode is direct, wanted gateway"]]):
            code, out = self.main("--runtime", "runc")
        self.assertEqual(code, 1, out)
        self.assertIn("refusing to upgrade:\n  composed values: [ingress] mode is direct, wanted gateway", out)
        self.assertEqual(self.release.upgrades, [])

    def test_a_server_running_without_the_gateway_after_the_upgrade_fails(self):
        self.release.mounted_override = base_toml() + '\n[ingress]\nmode = "direct"\n'
        code, out = self.main("--runtime", "runc")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL  the server pod's config.toml: [ingress] mode is direct, wanted gateway", out)

    def test_a_server_running_another_egress_image_fails(self):
        ingress = f'\n[ingress]\nmode = "gateway"\n\ngateway.address = "{GW_HOST}"\n'
        self.release.mounted_override = base_toml(image=SIGV4_IMAGE) + ingress
        code, out = self.main("--runtime", "runc")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL  the server pod's config.toml: [egress] image is", out)

    def test_problems_with_reports_a_missing_ingress(self):
        sc.load_env()
        want = {"runtime": "runc", "egress_image": BASE_IMAGE, "egress_mode": "dns+nft",
                "gateway": {"enabled": True, "host": GW_HOST, "route": "header"}}
        self.assertEqual(sc.problems_with(tomllib.loads(base_toml()), want, "x"),
                         ["x: [ingress] mode is None, wanted gateway (the gateway would be lost)"])


if __name__ == "__main__":
    unittest.main()
