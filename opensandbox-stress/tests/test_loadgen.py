"""The programs that run inside sandboxes and the Job (loadgen/), run here against local stand-ins:
an echo server shaped like part 4's, and a fake lifecycle API that checks the key header."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

LOADGEN = Path(__file__).resolve().parent.parent / "loadgen"


class Echo(BaseHTTPRequestHandler):
    """Part 4's echo server, reduced: the request as JSON, headers included."""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        payload = json.dumps({"method": "GET", "path": self.path, "headers": dict(self.headers.items())}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class FakeApi(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        ok = self.headers.get("OPEN-SANDBOX-API-KEY") == "test-key"
        payload = b'{"items": []}' if ok else b'{"error": "no key"}'
        self.send_response(200 if ok else 401)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(request):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), request.param)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()


def run(name: str, *args: str, env: dict | None = None, timeout: float = 60) -> list[dict]:
    done = subprocess.run([sys.executable, str(LOADGEN / name), *args], capture_output=True, text=True,
                          timeout=timeout, env=env)
    assert done.returncode == 0, done.stderr
    return [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]


@pytest.mark.parametrize("server", [Echo], indirect=True)
def test_http_load(server):
    [result] = run("http_load.py", "--host", "localhost", "--port", str(server), "--warmup", "2", "--sequential", "20",
                   "--concurrency", "4", "--duration", "0.5", "--expect-header", "User-Agent")
    assert result["sequential"]["n"] == 20 and result["sequential"]["errors"] == 0
    assert len(result["sequential"]["dns_ms"]) == 20
    assert result["load"]["n"] > 0 and result["load"]["rps"] > 0 and result["load"]["errors"] == 0
    assert result["header_seen"] is True
    assert set(result["statuses"]) == {"200"}


@pytest.mark.parametrize("server", [Echo], indirect=True)
def test_http_load_header_not_seen(server):
    [result] = run("http_load.py", "--host", "localhost", "--port", str(server), "--warmup", "0", "--sequential", "1",
                   "--concurrency", "1", "--duration", "0.1", "--expect-header", "X-Stress-Key")
    assert result["header_seen"] is False


def test_http_load_counts_failures():
    [result] = run("http_load.py", "--host", "localhost", "--port", "9", "--warmup", "0", "--sequential", "3",
                   "--concurrency", "1", "--duration", "0.1", "--timeout", "1")
    assert result["sequential"]["errors"] == 3 and result["statuses"].get("0", 0) >= 3


@pytest.mark.parametrize("server", [Echo], indirect=True)
def test_http_load_resolve_once(server):
    [result] = run("http_load.py", "--host", "localhost", "--port", str(server), "--warmup", "2", "--sequential", "20",
                   "--concurrency", "4", "--duration", "0.5", "--resolve-once")
    assert result["resolve_once"] is True and result["resolve_once_ms"] >= 0
    assert result["sequential"]["n"] == 20 and result["sequential"]["dns_ms"] == []      # no lookup per request
    assert result["load"]["n"] > 0 and result["failures"]["count"] == 0
    assert result["failures"]["first_s"] is None and result["stopped_early"] == []
    assert result["failures"]["last_ok_s"] > 0.4                         # the load phase ran 0.5 s
    assert "127.0.0.1" not in json.dumps(result)                                        # no address printed


def test_http_load_resolve_once_reports_when_the_address_stops_answering():
    """The cached address stops answering partway: the failures are counted with their kind and the
    seconds since the lookup, the one-at-a-time phase ends after N failures in a row, nothing hangs."""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    proc = subprocess.Popen([sys.executable, str(LOADGEN / "http_load.py"), "--host", "localhost", "--port", str(port),
                             "--warmup", "0", "--sequential", "100000", "--concurrency", "2", "--duration", "0.5",
                             "--timeout", "1", "--resolve-once", "--max-consecutive-failures", "5"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    import time
    time.sleep(1.0)
    httpd.shutdown()
    httpd.server_close()
    out, err = proc.communicate(timeout=60)
    assert proc.returncode == 0, err
    result = json.loads(out.strip().splitlines()[-1])
    assert result["sequential"]["n"] > 0 and result["sequential"]["errors"] >= 5
    assert result["stopped_early"] == ["one at a time after 5 failures in a row"]
    assert result["failures"]["since"] == "the lookup" and result["failures"]["first_s"] >= 0.5
    assert sum(result["failures"]["kinds"].values()) == result["failures"]["count"]
    assert "refused" in result["failures"]["kinds"] and "OSError" not in result["failures"]["kinds"]
    assert result["load"]["errors"] > 0 and result["load"]["n"] == 0
    # Nothing succeeded after the first failure: what an expired address looks like.
    assert result["failures"]["last_ok_s"] <= result["failures"]["first_s"]


def test_victim():
    [result] = run("victim.py", "--iterations", "2", "--cpu-rounds", "5", "--io-mb", "2", "--fsyncs", "3",
                   "--dir", str(Path(__file__).parent))
    assert len(result["cpu_ms"]) == 2 and len(result["io_ms"]) == 2 and len(result["fsync_ms"]) == 6
    assert not list(Path(__file__).parent.glob("victim-*"))       # it removes its files


def test_hog_stops_by_itself(tmp_path):
    subprocess.run([sys.executable, str(LOADGEN / "hog.py"), "cpu", "--workers", "1", "--seconds", "0.5"], timeout=20,
                   check=True)
    subprocess.run([sys.executable, str(LOADGEN / "hog.py"), "disk", "--mb", "1", "--files", "2", "--seconds", "0.5",
                    "--dir", str(tmp_path)], timeout=20, check=True)
    assert len(list(tmp_path.glob("hog-*.bin"))) <= 2               # round and round over --files files


@pytest.mark.parametrize("server", [FakeApi], indirect=True)
def test_api_load(server):
    env = {"OSB_API_KEY": "test-key", "PATH": "/usr/bin:/bin"}
    lines = run("api_load.py", "--url", f"http://127.0.0.1:{server}", "--levels", "1,3", "--duration", "0.4",
                "--pause", "0", "--ids", "a,b", env=env)
    levels = [line for line in lines if "level" in line]
    assert [lv["level"] for lv in levels] == [1, 3]
    assert all(lv["ok"] > 0 and set(lv["statuses"]) == {"200"} for lv in levels)
    assert lines[-1] == {"done": True}
    assert "test-key" not in json.dumps(lines)


def test_api_load_needs_the_key():
    done = subprocess.run([sys.executable, str(LOADGEN / "api_load.py"), "--levels", "1"], capture_output=True,
                          text=True, env={"PATH": "/usr/bin:/bin"}, timeout=30)
    assert done.returncode != 0 and "OSB_API_KEY" in done.stderr


PYTHON_IMAGE = "public.ecr.aws/docker/library/python:3.12-slim"


def _image_here(image: str) -> bool:
    import shutil
    return shutil.which("docker") is not None and subprocess.run(
        ["docker", "image", "inspect", image], capture_output=True).returncode == 0


def _probe_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("hog_probe", LOADGEN / "hog_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hog_probe_parses_stat_lines_with_awkward_names():
    probe = _probe_module()
    # Field 2 may hold spaces and parentheses; utime (14) = 250 and stime (15) = 50.
    line = "4242 (python3 (hog) x) R 1 4242 4242 0 -1 4194304 100 0 0 0 250 50 0 0 20 0 1 0 1000 0 0\n"
    assert probe.parse_stat(line) == ("R", 300)


def test_hog_probe_cpu_and_writes_from_a_fake_proc(tmp_path):
    probe = _probe_module()
    proc = tmp_path / "proc"

    def process(pid: int, argv: list[str], state: str = "R", ticks: int = 0) -> None:
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        (proc / str(pid) / "stat").write_text(f"{pid} (python3) {state} 1 1 1 0 -1 0 0 0 0 0 {ticks} 0 0 0 20 0 1 0 0\n")

    process(10, ["/usr/local/bin/python3", "/tmp/hog.py", "cpu", "--workers", "2"])
    process(11, ["/usr/local/bin/python3", "/tmp/hog.py", "cpu", "--workers", "2"], ticks=100)
    process(12, ["/usr/local/bin/python3", "/tmp/hog.py", "cpu", "--workers", "2"], state="Z", ticks=90)
    process(13, ["sh", "-c", "python3 /tmp/hog.py cpu"])                   # a shell naming hog.py: not a hog
    process(14, ["/usr/local/bin/python3", "/tmp/victim.py"])               # another Python program
    assert probe.hog_pids(str(proc)) == [10, 11, 12]
    before = probe.read_ticks([10, 11, 12], str(proc))
    assert before == {10: 0, 11: 100}                                       # the zombie is not alive
    after = {10: 0, 11: 300}                                                # 200 ticks in 2 s at 100 Hz
    assert probe.cpu_milli(before, after, 2.0, 100) == 1000.0
    assert probe.cpu_milli(before, {}, 2.0, 100) is None                    # nothing left to measure
    assert probe.cpu_milli({10: 5}, {10: 5}, 2.0, 100) == 0.0               # alive, but not running

    folder = tmp_path / "root"
    folder.mkdir()
    assert probe.newest_write_s(str(folder), 1000.0) is None
    (folder / "hog-0.bin").write_bytes(b"x")
    (folder / "hog-1.bin").write_bytes(b"x")
    import os
    os.utime(folder / "hog-0.bin", (900.0, 900.0))
    os.utime(folder / "hog-1.bin", (990.0, 990.0))
    assert probe.newest_write_s(str(folder), 1000.0) == 10.0


def _docker_probe(tmp_path, script: str) -> dict:
    done = subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64", "-v", f"{LOADGEN}:/app:ro",
                           PYTHON_IMAGE, "sh", "-c", "cp /app/hog.py /app/hog_probe.py /tmp/; " + script],
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _image_here(PYTHON_IMAGE), reason="needs Docker and the python:3.12-slim image already pulled")
def test_hog_probe_sees_a_spinning_cpu_hog_and_a_writing_disk_hog(tmp_path):
    """In Linux (the sandbox's /proc): a CPU hog is its main process and 2 workers, a shell wrapper
    naming hog.py is not counted, the workers' CPU is measured, and a disk hog's latest write is seen."""
    from checks import hog_verdict
    cpu = _docker_probe(tmp_path, "sh -c 'python3 /tmp/hog.py cpu --workers 2 --seconds 20; true' & "
                                  "sleep 3; python3 /tmp/hog_probe.py --window 2")
    assert cpu["procs"] == 3                                   # main + 2 workers, not the shell
    assert cpu["cpu_m"] >= 300 and 1.9 <= cpu["window_s"] < 4
    assert hog_verdict("cpu", cpu) == (True, "")
    disk = _docker_probe(tmp_path, "python3 /tmp/hog.py disk --mb 1 --files 2 --seconds 20 & "
                                   "sleep 3; python3 /tmp/hog_probe.py --window 1")
    assert disk["procs"] == 2 and disk["newest_write_s"] is not None and disk["newest_write_s"] < 5
    assert hog_verdict("disk", disk) == (True, "")


@pytest.mark.skipif(not _image_here(PYTHON_IMAGE), reason="needs Docker and the python:3.12-slim image already pulled")
def test_hog_probe_fails_a_killed_or_stopped_hog(tmp_path):
    """What execd's timeout does to a background command (SIGKILL to its process group), and a hog
    that is alive but does not run (SIGSTOP), both make the scenario invalid."""
    from checks import hog_verdict
    # setsid: the hog leads its own process group, as execd starts a command; $! is that group.
    killed = _docker_probe(tmp_path, "setsid python3 /tmp/hog.py cpu --workers 2 --seconds 20 & "
                                     "sleep 2; kill -KILL -$!; sleep 1; python3 /tmp/hog_probe.py --window 1")
    assert killed["procs"] == 0 and killed["cpu_m"] is None
    assert hog_verdict("cpu", killed)[0] is False
    stopped = _docker_probe(tmp_path, "setsid python3 /tmp/hog.py cpu --workers 2 --seconds 20 & "
                                      "sleep 2; kill -STOP -$!; sleep 1; python3 /tmp/hog_probe.py --window 2")
    assert stopped["procs"] == 3 and stopped["cpu_m"] is not None and stopped["cpu_m"] < 50
    running, why = hog_verdict("cpu", stopped)
    assert running is False and "CPU" in why
