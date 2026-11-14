"""Test 10d: does the Cloud NAT metric work at all? Totals of nat/sent_bytes_count and nat/received_bytes_count over
the whole cluster lifetime (default 6 h back), with the busiest minutes. Run it after the tests, even after the NAT
gateway is deleted: Cloud Monitoring keeps the series."""
import datetime as dt, json, sys, urllib.parse, urllib.request
from common import PROJECT_ID, banner, run, save

hours = float(sys.argv[1]) if len(sys.argv) > 1 else 6
token = run(["gcloud", "auth", "print-access-token"], quiet=True).stdout.strip()
end = dt.datetime.now(dt.timezone.utc); start = end - dt.timedelta(hours=hours)
out = {}
for metric in ("sent_bytes_count", "received_bytes_count"):
    params = {"filter": f'metric.type="router.googleapis.com/nat/{metric}"', "interval.startTime": start.isoformat(),
              "interval.endTime": end.isoformat(), "aggregation.alignmentPeriod": "60s", "aggregation.perSeriesAligner": "ALIGN_DELTA"}
    req = urllib.request.Request(f"https://monitoring.googleapis.com/v3/projects/{PROJECT_ID}/timeSeries?" + urllib.parse.urlencode(params),
                                 headers={"Authorization": f"Bearer {token}"})
    per_min = {}
    for s in json.load(urllib.request.urlopen(req)).get("timeSeries", []):
        for p in s["points"]:
            t = p["interval"]["endTime"][11:16]
            per_min[t] = per_min.get(t, 0) + float(p["value"].get("int64Value", 0))
    top = sorted(per_min.items(), key=lambda kv: -kv[1])[:5]
    out[metric] = {"total_mb": round(sum(per_min.values()) / 1e6, 1), "busiest_minutes_mb": [(t, round(v / 1e6, 1)) for t, v in top]}
    banner(metric); print(f"  total {out[metric]['total_mb']} MB over {hours:g} h; busiest minutes {out[metric]['busiest_minutes_mb']}")
save("nat_sanity", out)
