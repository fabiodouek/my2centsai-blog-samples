"""Test 8: a custom snapshot built with the declarative Image builder vs pip install at runtime.
The default snapshot already ships pandas, numpy, matplotlib, scikit-learn and torch, so this uses
packages it doesn't have: polars, duckdb and pyarrow."""

from daytona import CreateSandboxFromSnapshotParams, CreateSnapshotParams, Daytona, Image

from common import SAFETY_TTL, banner, labels, timed

SNAP = "my2cents-lifecycle-tpl"
PKGS = ["polars==1.35.2", "duckdb==1.4.2", "pyarrow==22.0.0"]
CHECK = "python3 -c 'import polars, duckdb, pyarrow; print(polars.__version__, duckdb.__version__, pyarrow.__version__)'"
daytona = Daytona()

banner("Build the snapshot")
image = Image.debian_slim("3.12").pip_install(PKGS).workdir("/home/daytona")
print(image.dockerfile())
try:
    daytona.snapshot.delete(daytona.snapshot.get(SNAP))
except Exception:
    pass
with timed("snapshot.create()"):
    daytona.snapshot.create(CreateSnapshotParams(name=SNAP, image=image), on_logs=lambda line: None, timeout=0)

banner("Path A: sandbox from the custom snapshot")
with timed("create + first import"):
    a = daytona.create(CreateSandboxFromSnapshotParams(snapshot=SNAP, labels=labels(test="08", path="snapshot"),
                                                        ttl_minutes=SAFETY_TTL))
    print("  " + a.process.exec(CHECK).result.strip())

banner("Path B: default sandbox + pip install")
with timed("create + pip install + first import"):
    b = daytona.create(CreateSandboxFromSnapshotParams(labels=labels(test="08", path="runtime"), ttl_minutes=SAFETY_TTL))
    b.process.exec(f"pip install -q {' '.join(PKGS)}", timeout=600)
    print("  " + b.process.exec(CHECK).result.strip())

a.delete()
b.delete()
print(f"\nkept snapshot {SNAP} for reuse; 09_cleanup.py deletes it")
