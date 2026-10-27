"""Test 8: time-to-first-pandas-import on the base template (pip install at runtime) vs the custom template.
Run `python template/build.py` once first. Creates and imports are timed separately, and the custom template
is run twice because the first sandbox from a fresh build can hit a cold cache."""

from e2b import Sandbox

from common import banner, tagged, timed

IMPORT = "python3 -c 'import pandas, numpy, matplotlib; print(pandas.__version__)'"


def run(label: str, template: str | None, install: bool) -> float:
    banner(label)
    r: dict[str, float] = {}
    with timed("create", r):
        sbx = Sandbox.create(template, timeout=300, metadata=tagged(test="08"))
    if install:
        with timed("pip install pandas numpy matplotlib", r):
            sbx.commands.run("pip install -q pandas numpy matplotlib", timeout=300)
    with timed("first import", r):
        print(f"  pandas {sbx.commands.run(IMPORT).stdout.strip()}")
    with timed("second import", r):
        sbx.commands.run(IMPORT)
    info = sbx.get_info()
    print(f"  {info.cpu_count} vCPU / {info.memory_mb} MiB")
    sbx.kill()
    total = r["create"] + r.get("pip install pandas numpy matplotlib", 0) + r["first import"]
    print(f"  time to first import: {total:.2f}s")
    return total


base = run("Base template + pip install at runtime", None, install=True)
custom1 = run("my2cents-datasci (run 1)", "my2cents-datasci", install=False)
custom2 = run("my2cents-datasci (run 2)", "my2cents-datasci", install=False)
print(f"\nspeedup vs base: run 1 {base / custom1:.1f}x, run 2 {base / custom2:.1f}x")
