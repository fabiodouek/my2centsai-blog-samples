"""Estimate the monthly cost of an AWS Lambda MicroVMs workload, line by line, the way the AWS pricing page
works its two examples. Standard library only.

    python microvm_cost.py scenarios/coding-sandboxes.json            # AWS Pricing Example 1: $1,241.22
    python microvm_cost.py scenarios/ci-jobs.json                     # AWS Pricing Example 2: $1,124.03
    python microvm_cost.py scenarios/coding-sandboxes.json --steps    # build it up from one MicroVM
    python microvm_cost.py scenarios/ci-jobs.json --set sessions=2500 --set peak_share=0.3
    python microvm_cost.py scenarios/coding-sandboxes.json --rates price-list   # unrounded rates

A scenario is one kind of MicroVM session (a developer's day, a CI job) repeated `sessions` times a month.
Every session launches once from an image, runs `active_seconds` (a share of it at peak), and may suspend
`cycles` times, spending `suspended_seconds` suspended in total. See scenarios/*.json for the fields.
"""

import argparse
import json
import sys
from pathlib import Path

# us-east-1, ARM (Graviton). "page" is what the AWS pricing page uses in its examples (rounded);
# "price-list" is the AWS price list and what the bill charges (Cost Explorer shows the same values).
RATES = {
    "page": {
        "vcpu_second": 0.0000276944,      # $ per vCPU-second
        "gb_second": 0.0000036667,        # $ per GB-second of memory
        "snapshot_write_gb": 0.0038,      # $ per GB written on suspend
        "snapshot_read_gb": 0.00155,      # $ per GB read on launch or resume
        "storage_gb_month": 0.08,         # $ per GB-month: images and suspended MicroVMs
    },
    "price-list": {
        "vcpu_second": 0.0000276944,
        "gb_second": 0.0000036667,
        "snapshot_write_gb": 0.0037977138,
        "snapshot_read_gb": 0.0015467699,
        "storage_gb_month": 0.08,         # billed per GB-hour: $0.0001111111 = 0.08 / 720
    },
}
HOURS_PER_MONTH = 720  # the pricing page's month for snapshot storage
GB_PER_VCPU = 2        # MicroVMs allocate 1 vCPU per 2 GB of memory
MAX_BURST = 4          # a MicroVM can scale up to 4x its baseline
MAX_LIFETIME_S = 8 * 3600  # running and suspended time both count


def vcpus(gb: float) -> float:
    return gb / GB_PER_VCPU


def per_second(gb: float, r: dict) -> float:
    """Compute price of one second at a given size (vCPU follows memory at 1 per 2 GB)."""
    return vcpus(gb) * r["vcpu_second"] + gb * r["gb_second"]


def money(x: float) -> str:
    """Cents, or four decimals for amounts under a dollar (a single session, an hour)."""
    return f"${x:,.2f}" if abs(x) >= 1 or x == 0 else f"${x:,.4f}"


def rate(x: float) -> str:
    return f"${x:.10f}".rstrip("0")


def hours(s: float) -> str:
    return f"{s / 3600:g} h" if s >= 3600 else f"{s / 60:g} min"


def secs(x: float) -> str:
    return f"{x / 1e6:,.2f}M s" if x >= 1e6 else f"{x:,.0f} s"


def check(sc: dict) -> list[str]:
    warn = []
    if sc["peak_gb"] > sc["baseline_gb"] * MAX_BURST:
        warn.append(f"peak_gb {sc['peak_gb']} is above 4x the {sc['baseline_gb']} GB baseline; MicroVMs can't burst that far")
    if not 0 <= sc["peak_share"] <= 1:
        warn.append("peak_share must be between 0 and 1")
    if sc["active_seconds"] + sc["suspended_seconds"] > MAX_LIFETIME_S:
        warn.append("active + suspended time is over 8 hours, the MicroVM lifetime cap (suspended time counts); "
                    "split it into more sessions")
    if sc["cycles"] and not sc["suspended_seconds"]:
        warn.append("cycles > 0 but suspended_seconds is 0")
    return warn


def estimate(sc: dict, r: dict) -> dict:
    """Line items for one month, in the order of the AWS examples. Values are unrounded."""
    n, act = sc["sessions"], sc["active_seconds"]
    peak_s, base_s = n * act * sc["peak_share"], n * act * (1 - sc["peak_share"])
    pv, pg, bv, bg = vcpus(sc["peak_gb"]), sc["peak_gb"], vcpus(sc["baseline_gb"]), sc["baseline_gb"]
    lines = {
        "peak_seconds": peak_s,
        "peak_vcpu": peak_s * pv * r["vcpu_second"],
        "peak_memory": peak_s * pg * r["gb_second"],
        "baseline_seconds": base_s,
        "baseline_vcpu": base_s * bv * r["vcpu_second"],
        "baseline_memory": base_s * bg * r["gb_second"],
        "cycles": n * sc["cycles"],
        "write_suspend": n * sc["cycles"] * sc["state_gb"] * r["snapshot_write_gb"],
        "read_resume": n * sc["cycles"] * sc["state_gb"] * r["snapshot_read_gb"],
        "launches": n * sc["launches_per_session"],
        "read_launch": n * sc["launches_per_session"] * sc["image_gb"] * r["snapshot_read_gb"],
        "image_storage": sum(i["count"] * i["gb"] for i in sc["images"]) * r["storage_gb_month"],
        "suspended_storage": n * sc["state_gb"] * sc["suspended_seconds"] / 3600 / HOURS_PER_MONTH * r["storage_gb_month"],
    }
    lines["peak_total"] = lines["peak_vcpu"] + lines["peak_memory"]
    lines["baseline_total"] = lines["baseline_vcpu"] + lines["baseline_memory"]
    lines["compute"] = lines["peak_total"] + lines["baseline_total"]
    lines["read_write"] = lines["write_suspend"] + lines["read_resume"] + lines["read_launch"]
    lines["storage"] = lines["image_storage"] + lines["suspended_storage"]
    lines["total"] = lines["compute"] + lines["read_write"] + lines["storage"]
    lines["per_unit"] = lines["total"] / sc["per"]["divisor"]
    return lines


def report(sc: dict, e: dict) -> None:
    b, p = sc["baseline_gb"], sc["peak_gb"]
    rows = [
        ("Compute", None, None),
        ("  Peak", f"{secs(e['peak_seconds'])} at {vcpus(p):g} vCPU / {p:g} GB", e["peak_total"]),
        ("    vCPU", f"{secs(e['peak_seconds'])} x {vcpus(p):g} vCPU", e["peak_vcpu"]),
        ("    memory", f"{secs(e['peak_seconds'])} x {p:g} GB", e["peak_memory"]),
        ("  Baseline", f"{secs(e['baseline_seconds'])} at {vcpus(b):g} vCPU / {b:g} GB", e["baseline_total"]),
        ("    vCPU", f"{secs(e['baseline_seconds'])} x {vcpus(b):g} vCPU", e["baseline_vcpu"]),
        ("    memory", f"{secs(e['baseline_seconds'])} x {b:g} GB", e["baseline_memory"]),
        ("  Compute total", "", e["compute"]),
        ("Snapshot read/write", None, None),
        ("  Write on suspend", f"{e['cycles']:,.0f} cycles x {sc['state_gb']:g} GB", e["write_suspend"]),
        ("  Read on resume", f"{e['cycles']:,.0f} cycles x {sc['state_gb']:g} GB", e["read_resume"]),
        ("  Read on launch", f"{e['launches']:,.0f} launches x {sc['image_gb']:g} GB", e["read_launch"]),
        ("  Read/write total", "", e["read_write"]),
        ("Snapshot storage", None, None),
        ("  Images", " + ".join(f"{i['count']} x {i['gb']:g} GB" for i in sc["images"]) + " for a month", e["image_storage"]),
        ("  Suspended MicroVMs", f"{sc['sessions']:,} x {sc['state_gb']:g} GB x {sc['suspended_seconds'] / 3600:g} h / 720 h",
         e["suspended_storage"]),
        ("  Storage total", "", e["storage"]),
    ]
    print(f"\n{sc['name']}\n{'=' * len(sc['name'])}")
    if sc.get("source"):
        print(sc["source"])
    print()
    for label, how, cost in rows:
        if cost is None:
            print(label)
        elif cost or not how:  # skip lines that don't apply (no suspends, nothing suspended)
            print(f"{label:24}{how:46}{f'${cost:,.2f}':>12}")
    print("-" * 82)
    print(f"{'Monthly total':70}{money(e['total']):>12}")
    print(f"{'Per ' + sc['per']['label']:70}{money(e['per_unit']):>12}")
    share = {k: e[k] / e["total"] for k in ("compute", "read_write", "storage")}
    print(f"\nShare: compute {share['compute']:.1%}, snapshot read/write {share['read_write']:.1%}, "
          f"snapshot storage {share['storage']:.1%}")


def steps(sc: dict, r: dict) -> None:
    """Build the scenario up from one MicroVM, the way the post and the video explain it."""
    b, p = sc["baseline_gb"], sc["peak_gb"]
    base_s, peak_s = per_second(b, r), per_second(p, r)
    print(f"\nStep 1: one MicroVM at its {vcpus(b):g} vCPU / {b:g} GB baseline")
    print(f"  per second  {vcpus(b):g} x {rate(r['vcpu_second'])} + {b:g} x {rate(r['gb_second'])} = {rate(base_s)}")
    print(f"  per hour    {money(base_s * 3600)}   (vCPU {money(vcpus(b) * r['vcpu_second'] * 3600)}, "
          f"memory {money(b * r['gb_second'] * 3600)})")

    print(f"\nStep 2: bursting to {vcpus(p):g} vCPU / {p:g} GB")
    print(f"  per second at peak  {rate(peak_s)}  ({peak_s / base_s:g}x the baseline)")
    act, share = sc["active_seconds"], sc["peak_share"]
    a = act * (1 - share) * base_s + act * share * peak_s
    floor, bump = act * base_s, act * share * (peak_s - base_s)
    print(f"  {hours(act)} active, {share:.0%} at peak:")
    print(f"    baseline {act * (1 - share):,.0f} s x {rate(base_s)} + peak {act * share:,.0f} s x {rate(peak_s)} = {money(a)}")
    print(f"    same as the floor for all {act:,.0f} s ({money(floor)}) + the extra above it while used ({money(bump)})")

    cyc = sc["state_gb"] * (r["snapshot_write_gb"] + r["snapshot_read_gb"])
    store_h = sc["state_gb"] * r["storage_gb_month"] / HOURS_PER_MONTH
    print(f"\nStep 3: suspend and resume ({sc['state_gb']:g} GB of state)")
    if sc["cycles"]:
        print(f"  one cycle   write {money(sc['state_gb'] * r['snapshot_write_gb'])} + read "
              f"{money(sc['state_gb'] * r['snapshot_read_gb'])} = {money(cyc)}")
        print(f"  suspended   {rate(store_h)} per hour of storage, vs {money(base_s * 3600)} per hour running")
        print(f"  break-even  suspending pays off after about {cyc / (base_s - store_h / 3600) / 60:.1f} idle minutes")
    else:
        print("  this scenario never suspends: every session runs once and ends")

    launch = sc["launches_per_session"] * sc["image_gb"] * r["snapshot_read_gb"]
    cycles = sc["cycles"] * cyc
    susp = sc["suspended_seconds"] / 3600 * store_h
    one = launch + a + cycles + susp
    print("\nStep 4: one session")
    print(f"  {'launch: read the ' + format(sc['image_gb'], 'g') + ' GB image':32}{money(launch):>9}")
    print(f"  {'compute':32}{money(a):>9}")
    if sc["cycles"]:
        print(f"  {format(sc['cycles'], 'g') + ' suspend/resume cycles':32}{money(cycles):>9}")
        print(f"  {hours(sc['suspended_seconds']) + ' of suspended storage':32}{money(susp):>9}")
    print(f"  {'session total':32}{money(one):>9}")
    if sc["suspended_seconds"]:
        never = launch + (act + sc["suspended_seconds"]) * base_s + bump
        print(f"  without suspending (idle at baseline instead): {money(never)}")

    imgs = sum(i["count"] * i["gb"] for i in sc["images"]) * r["storage_gb_month"]
    print(f"\nStep 5: {sc['sessions']:,} sessions a month, plus images")
    print(f"  {sc['sessions']:,} x {money(one)} = {money(sc['sessions'] * one)}   + images {money(imgs)}"
          f" = {money(sc['sessions'] * one + imgs)}")
    print(f"  per {sc['per']['label']}: {money((sc['sessions'] * one + imgs) / sc['per']['divisor'])}")


def coerce(v: str):
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return v


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", type=Path, help="scenario JSON file")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a scenario field")
    ap.add_argument("--rates", choices=RATES, default="page", help="page (default) or price-list")
    ap.add_argument("--steps", action="store_true", help="also show the build-up from one MicroVM")
    ap.add_argument("--json", action="store_true", help="print the line items as JSON")
    a = ap.parse_args()
    sc = json.loads(a.scenario.read_text())
    for kv in a.set:
        k, _, v = kv.partition("=")
        if k not in sc:
            sys.exit(f"unknown field {k!r} (have: {', '.join(sc)})")
        sc[k] = coerce(v)
    for w in check(sc):
        print(f"warning: {w}", file=sys.stderr)
    r = RATES[a.rates]
    e = estimate(sc, r)
    if a.json:
        print(json.dumps({k: round(v, 6) for k, v in e.items()}, indent=1))
        return
    report(sc, e)
    if a.steps:
        steps(sc, r)


if __name__ == "__main__":
    main()
