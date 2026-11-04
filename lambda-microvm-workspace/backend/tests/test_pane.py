"""vitals?live=1 reports what runs in the terminal's tmux pane (for the Start Claude Code button)."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import Settings
from app.events import Hub
from app.main import PANE_PROBE, create_app, pane_info
from app.store import MemoryStore
from tests.fakes import FakeMicroVMs, FakeVerifier

H = {"X-Origin-Verify": "s3cret", "Authorization": "Bearer tok-alice"}


class FakeAgent:
    """Stands in for the in-VM agent: no HTTP at all."""

    def __init__(self, stdout="claude\nclaude \n", rc=0, stats_error=None, exec_error=None):
        self.stdout, self.rc, self.stats_error, self.exec_error = stdout, rc, stats_error, exec_error
        self.calls = []

    async def get(self, vm_id, endpoint, path):
        self.calls.append(("GET", path))
        if self.stats_error:
            raise HTTPException(502, self.stats_error)
        return {"cpu_pct": 1.0, "procs": 9}

    async def post(self, vm_id, endpoint, path, body):
        self.calls.append(("POST", path, body))
        if self.exec_error:
            raise HTTPException(504, self.exec_error)
        return {"rc": self.rc, "stdout": self.stdout, "stderr": ""}

    async def aclose(self):
        pass


def client(agent):
    s = Settings(origin_secret="s3cret", user_pool_id="p", client_id="c", table_name="t", image_arn="arn:img",
                 connector_arn="arn:conn", nlb_ips=["10.50.10.10"], placeholders={}, secret_arns={})
    mv = FakeMicroVMs()
    c = TestClient(create_app(s, microvms=mv, store=MemoryStore(), verifier=FakeVerifier(), hub=Hub(None), agent=agent))
    vid = c.post("/api/sandboxes", json={"name": "a"}, headers=H).json()["id"]
    mv.vms[vid]["state"] = "RUNNING"
    return c, mv, vid


@pytest.mark.parametrize("out,expect", [
    ("claude\nclaude \n", {"command": "claude", "claude_running": True, "at_shell": False}),
    ("node\nnode /usr/local/bin/claude --resume \n", {"command": "node", "claude_running": True, "at_shell": False}),
    ("bash\n-bash \n", {"command": "bash", "claude_running": False, "at_shell": True}),
    ("vim\nvim claude.md \n", {"command": "vim", "claude_running": False, "at_shell": False}),
    ("less\nless /root/work/claude \n", {"command": "less", "claude_running": False, "at_shell": False}),
    ("", None),
])
def test_pane_info(out, expect):
    assert pane_info(out) == expect


def test_vitals_live_reports_the_pane():
    agent = FakeAgent()
    c, mv, vid = client(agent)
    v = c.get(f"/api/sandboxes/{vid}/vitals?live=1", headers=H).json()
    assert v["stats"]["procs"] == 9 and v["stats_error"] is None
    assert v["pane"] == {"command": "claude", "claude_running": True, "at_shell": False}
    assert ("POST", "/exec", {"cmd": PANE_PROBE, "timeout": 5}) in agent.calls


def test_pane_is_null_without_live_or_when_the_probe_fails():
    agent = FakeAgent(exec_error="sandbox agent did not answer /exec in time")
    c, mv, vid = client(agent)
    assert c.get(f"/api/sandboxes/{vid}/vitals", headers=H).json()["pane"] is None
    assert agent.calls == []   # live=0 never touches the VM
    v = c.get(f"/api/sandboxes/{vid}/vitals?live=1", headers=H).json()
    assert v["pane"] is None and v["stats"]["procs"] == 9
    agent.exec_error, agent.rc = None, 1
    assert c.get(f"/api/sandboxes/{vid}/vitals?live=1", headers=H).json()["pane"] is None


def test_stats_error_still_reported_with_pane():
    c, mv, vid = client(FakeAgent(stdout="bash\n-bash \n", stats_error="sandbox agent unreachable: ConnectError"))
    v = c.get(f"/api/sandboxes/{vid}/vitals?live=1", headers=H).json()
    assert v["stats"] is None and "unreachable" in v["stats_error"]
    assert v["pane"]["at_shell"] is True


def test_suspended_sandbox_is_not_probed():
    agent = FakeAgent()
    c, mv, vid = client(agent)
    mv.vms[vid]["state"] = "SUSPENDED"
    v = c.get(f"/api/sandboxes/{vid}/vitals?live=1", headers=H).json()
    assert v["pane"] is None and agent.calls == []
