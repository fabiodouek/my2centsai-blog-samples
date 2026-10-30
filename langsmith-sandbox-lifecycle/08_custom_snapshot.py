"""Test 8: build a snapshot from a Dockerfile and from a public image, then compare time to a first
`import pandas` against installing the same packages at runtime on the default snapshot."""

import time

from langsmith.sandbox import SandboxClient

from common import GiB, PREFIX, banner, create, timed

# The default 10 s HTTP timeout is too short for the export call at the end of a Dockerfile build: the
# server creates the snapshot anyway, but the SDK raises ReadTimeout and you lose the handle.
client = SandboxClient(timeout=120)

PKGS = "pandas numpy matplotlib"  # same list as datasci/Dockerfile
IMPORT = "python -c 'import time; t=time.time(); import pandas, matplotlib; print(f\"{time.time()-t:.2f}\")'"
made = []

try:
    banner("Default snapshot: is pandas already there?")
    a = create("08a")
    made.append(a)
    print(a.run("python3 -c 'import pandas' 2>&1 | tail -1; pip3 list 2>/dev/null | wc -l").stdout.strip())

    banner("Path A: default snapshot + install at runtime")
    t0 = time.perf_counter()
    with timed("venv + pip install " + PKGS):
        r = a.run(f"python3 -m venv /root/v && /root/v/bin/pip install -q {PKGS}", timeout=600)
    print(f"  exit={r.exit_code} {r.stderr.strip()[-200:]}")
    first = a.run(IMPORT.replace("python ", "/root/v/bin/python ", 1)).stdout.strip()
    second = a.run(IMPORT.replace("python ", "/root/v/bin/python ", 1)).stdout.strip()
    print(f"  import: first {first}s, second {second}s | create-to-import total {time.perf_counter() - t0:.2f}s")

    banner("Build a snapshot from a Dockerfile (temporary BuildKit builder sandbox)")
    # context defaults to ".", and the SDK tars that whole directory (everything but .git, no .dockerignore)
    # into the builder. Point it at a folder holding only the Dockerfile so .env and .venv stay home.
    log_lines = []
    try:
        with timed("create_snapshot_from_dockerfile(vcpus=2)"):
            snap = client.create_snapshot_from_dockerfile(
                f"{PREFIX}-datasci", "Dockerfile", context="datasci", fs_capacity_bytes=16 * GiB, vcpus=2, mem_bytes=8 * GiB,
                on_build_log=log_lines.append, timeout=900)
        print(f"  snapshot {snap.id} status={snap.status} tags={snap.tags}")
        try:
            client.get_snapshot(f"{PREFIX}-datasci")
            print("  resolvable by bare name")
        except Exception as e:
            print(f"  by bare name: {type(e).__name__}: {e}")
    except Exception as e:
        snap = None
        print(f"  {type(e).__name__}: {e}")
        print("  last build log:", "".join(log_lines)[-800:])

    banner("Build a snapshot from a public image (platform pull)")
    with timed("create_snapshot(docker_image='python:3.12-slim')"):
        try:
            slim = client.create_snapshot(f"{PREFIX}-py312", docker_image="python:3.12-slim",
                                          fs_capacity_bytes=4 * GiB, timeout=600)
            print(f"  snapshot {slim.id} status={slim.status} tags={slim.tags}")
        except Exception as e:
            print(f"  {type(e).__name__}: {e}")

    if snap:
        banner("Path B: sandbox from the custom snapshot")
        t0 = time.perf_counter()
        with timed("create from snapshot"):
            b = create("08b", snapshot_id=snap.id)
        made.append(b)
        first = b.run(IMPORT).stdout.strip()
        second = b.run(IMPORT).stdout.strip()
        print(f"  import: first {first}s, second {second}s | create-to-import total {time.perf_counter() - t0:.2f}s")
        print(f"  {b.run('python --version; whoami; pwd').stdout.strip()!r}")
finally:
    for s in made:
        s.delete()
