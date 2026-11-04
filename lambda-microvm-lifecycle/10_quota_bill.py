"""Test 10: quotas and the bill. Applied vs AWS-default quotas, what happens at the memory quota
(and whether SUSPENDED MicroVMs count), RunMicrovm throttling, and what Cost Explorer shows."""

import concurrent.futures as cf
import datetime as dt
import time

import boto3

from common import REGION, banner, err, image_arn, mvm, our_microvms, run

sq = boto3.client("service-quotas", region_name=REGION)

banner("Service Quotas: applied vs AWS default")
quotas = []
kw = {"ServiceCode": "lambda"}
while True:
    r = sq.list_service_quotas(**kw)
    quotas += [q for q in r["Quotas"] if "icro" in q["QuotaName"]]
    if not r.get("NextToken"):
        break
    kw["NextToken"] = r["NextToken"]
defaults = {}
for q in quotas:
    try:
        defaults[q["QuotaCode"]] = sq.get_aws_default_service_quota(ServiceCode="lambda", QuotaCode=q["QuotaCode"])["Quota"]["Value"]
    except Exception:
        defaults[q["QuotaCode"]] = None
for q in sorted(quotas, key=lambda q: q["QuotaName"]):
    d = defaults[q["QuotaCode"]]
    flag = "  <- below default" if d is not None and q["Value"] < d else ""
    print(f"{q['QuotaName']:58} applied {q['Value']:>7}  default {d!s:>7}{flag}")
for r in sq.list_requested_service_quota_change_history(ServiceCode="lambda").get("RequestedQuotas", []):
    if "icro" in r["QuotaName"]:
        print(f"request: {r['QuotaName']} -> {r['DesiredValue']} {r['Status']} ({r['Created']:%Y-%m-%d})")

banner("Fill the memory quota with 2 GB MicroVMs")
live, n = [], 0
while n < 12:
    try:
        vm = run("sandbox", "10-fill", idle={"autoResumeEnabled": False, "maxIdleDurationSeconds": 3600,
                                             "suspendedDurationSeconds": 900}, max_duration=900)
        live.append(vm)
        n += 1
        print(f"  #{n} {vm.id} ok")
    except Exception as e:
        print(f"  #{n + 1}: {err(e)}")
        break
for vm in live:
    vm.wait("RUNNING")
print(f"{len(live)} running x 2 GB = {len(live) * 2} GB")

banner("Do SUSPENDED MicroVMs count against the quota?")
for vm in live:
    mvm.suspend_microvm(microvmIdentifier=vm.id)
    time.sleep(0.6)  # SuspendMicrovm: 2/s
for vm in live:
    vm.wait("SUSPENDED")
try:
    extra = run("sandbox", "10-extra", max_duration=300)
    print("with all suspended, one more run: accepted", extra.id)
    extra.terminate()
except Exception as e:
    print("with all suspended, one more run:", err(e))
for vm in live:
    vm.terminate()
time.sleep(2)
t = time.perf_counter()
while any(m["state"] != "TERMINATED" for m in our_microvms()) and time.perf_counter() - t < 180:
    time.sleep(3)
print(f"all terminated after {time.perf_counter() - t:.0f}s")

banner("RunMicrovm throttling: 4 concurrent calls, no client retries")


def one(i):
    t0 = time.perf_counter()
    try:
        r = mvm.run_microvm(imageIdentifier=image_arn("sandbox"), maximumDurationInSeconds=300)
        return ("ok", round(time.perf_counter() - t0, 2), r["microvmId"])
    except Exception as e:
        return (err(e).split(":")[0], round(time.perf_counter() - t0, 2), None)

with cf.ThreadPoolExecutor(4) as ex:
    results = list(ex.map(one, range(4)))
for r in results:
    print("  ", r[:2])
for r in results:
    if r[2]:
        mvm.terminate_microvm(microvmIdentifier=r[2])

banner("Cost Explorer (lags by up to a day)")
ce = boto3.client("ce", region_name="us-east-1")
today = dt.date.today()
try:
    r = ce.get_cost_and_usage(
        TimePeriod={"Start": str(today - dt.timedelta(days=7)), "End": str(today + dt.timedelta(days=1))},
        Granularity="DAILY", Metrics=["UnblendedCost", "UsageQuantity"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": ["AWS Lambda"]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}])
    for day in r["ResultsByTime"]:
        for g in day["Groups"]:
            if any(s in g["Keys"][0] for s in ("MicroVM", "Microvm", "microvm", "MVM")):
                m = g["Metrics"]
                print(day["TimePeriod"]["Start"], g["Keys"][0], f"${float(m['UnblendedCost']['Amount']):.4f}",
                      m["UsageQuantity"]["Amount"], m["UsageQuantity"]["Unit"])
    print("(usage types shown are the ones whose name mentions MicroVM; re-run tomorrow for the full day)")
except Exception as e:
    print("Cost Explorer:", err(e))
