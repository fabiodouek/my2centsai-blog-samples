"""Your actual Lambda MicroVMs bill from Cost Explorer, by day and usage type, with the effective rate of each.
Read-only. Each run makes two Cost Explorer API calls ($0.01 each).

    python bill.py                          # the last 30 days
    python bill.py --start 2026-11-01 --end 2026-11-08
    python bill.py --profile myprofile

Needs ce:GetDimensionValues and ce:GetCostAndUsage. Cost Explorer lags by up to a day, and only the
payer account can turn on hourly granularity, so this reports days.
"""

import argparse
import datetime as dt

import boto3

LABELS = {
    "vCPU-Second": "vCPU-s",
    "Memory-GB-Second": "GB-s",
    "Snapshot-Read-GB": "read GB",
    "Snapshot-Write-GB": "write GB",
    "Snapshot-Storage-GB-Hour": "storage GB-h",
}


def short(usage_type: str) -> str:
    for k, v in LABELS.items():
        if k in usage_type:
            return v
    return usage_type


def main() -> None:
    today = dt.date.today()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default=str(today - dt.timedelta(days=30)), help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--end", default=str(today + dt.timedelta(days=1)), help="YYYY-MM-DD (exclusive)")
    ap.add_argument("--profile")
    a = ap.parse_args()
    ce = boto3.Session(profile_name=a.profile).client("ce", region_name="us-east-1")
    period = {"Start": a.start, "End": a.end}

    # Usage types are prefixed by Region outside us-east-1 (e.g. USW2-Lambda-MicroVM-...), so search for them.
    types = [d["Value"] for d in ce.get_dimension_values(TimePeriod=period, Dimension="USAGE_TYPE",
                                                         SearchString="Lambda-MicroVM")["DimensionValues"]]
    if not types:
        print(f"No Lambda MicroVM usage between {a.start} and {a.end}.")
        return
    rows, kw = [], {}
    while True:
        r = ce.get_cost_and_usage(TimePeriod=period, Granularity="DAILY", Metrics=["UsageQuantity", "UnblendedCost"],
                                  Filter={"Dimensions": {"Key": "USAGE_TYPE", "Values": types}},
                                  GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}], **kw)
        for day in r["ResultsByTime"]:
            for g in day["Groups"]:
                m = g["Metrics"]
                rows.append((day["TimePeriod"]["Start"], g["Keys"][0], float(m["UsageQuantity"]["Amount"]),
                             float(m["UnblendedCost"]["Amount"])))
        if not r.get("NextPageToken"):
            break
        kw = {"NextPageToken": r["NextPageToken"]}

    print(f"{'day':12}{'usage type':44}{'quantity':>14}{'cost':>11}")
    for day, ut, q, c in rows:
        print(f"{day:12}{ut:44}{q:>14,.3f}{c:>11.4f}")
    print()
    totals = {}
    for _, ut, q, c in rows:
        t = totals.setdefault(ut, [0.0, 0.0])
        t[0] += q
        t[1] += c
    print(f"{'usage type':44}{'quantity':>14}{'cost':>11}{'rate':>16}")
    for ut, (q, c) in sorted(totals.items(), key=lambda kv: -kv[1][1]):
        print(f"{ut:44}{q:>14,.3f}{c:>11.4f}{(c / q if q else 0):>16.10f}  per {short(ut).split()[-1]}")
    total = sum(c for _, c in totals.values())
    print(f"{'total':44}{'':>14}{total:>11.4f}")


if __name__ == "__main__":
    main()
