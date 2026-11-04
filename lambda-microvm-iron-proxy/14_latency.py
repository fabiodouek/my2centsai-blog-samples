"""Latency: transparent vs explicit. The same allowlisted request (GET https://api.github.com/zen with the
placeholder token) three ways from one MicroVM, interleaved, 10 rounds after 2 warm-ups, each a fresh curl
(new TCP + TLS):
  transparent           resolve through the DNS Firewall override, connect to the NLB on 443
  transparent, pinned   the same with --resolve (no DNS), to isolate the lookup
  explicit              a CONNECT tunnel through the NLB on 8080 (what HTTPS_PROXY does)
Then the DNS lookup time through the override: dig and getaddrinfo, cache misses (random names) and hits."""

import json

from common import banner, check, median, rand_label, run, start_log, summary, tf

start_log("14_latency")
NLB = tf()["nlb_ips"][0]
ROUNDS, WARM = 10, 2
URL = "https://api.github.com/zen"
FMT = "%{time_namelookup} %{time_connect} %{time_appconnect} %{time_starttransfer} %{time_total} %{http_code}"
VARIANTS = {
    "transparent": f"curl -s -o /dev/null -w '{FMT}' {URL} -H \"Authorization: token $GH_TOKEN\"",
    "transparent, pinned": f"curl -s -o /dev/null -w '{FMT}' --resolve api.github.com:443:{NLB} {URL} -H \"Authorization: token $GH_TOKEN\"",
    "explicit (8080)": f"curl -s -o /dev/null -w '{FMT}' -x http://{NLB}:8080 {URL} -H \"Authorization: token $GH_TOKEN\"",
}

vm = run("14-latency")
script = []
for i in range(WARM + ROUNDS):
    for name, cmd in VARIANTS.items():
        script.append(f"echo \"{i}|{name}|$({cmd})\"; sleep 0.3")
out = vm.sh("\n".join(script), timeout=300)
data = {name: [] for name in VARIANTS}
for line in out.splitlines():
    if line.count("|") < 2:
        continue
    i, name, vals = line.split("|", 2)
    f = vals.split()
    if int(i) < WARM or len(f) < 6:
        continue
    data[name].append({"dns": float(f[0]), "connect": float(f[1]), "tls": float(f[2]), "ttfb": float(f[3]),
                       "total": float(f[4]), "code": f[5]})

banner(f"HTTP: median of {ROUNDS} requests each (seconds; curl's cumulative timers)")
print(f"{'variant':22} {'dns':>7} {'connect':>8} {'tls done':>9} {'ttfb':>7} {'total':>7}  min-max total   codes")
med = {}
for name, rows in data.items():
    m = {k: median([r[k] for r in rows]) for k in ("dns", "connect", "tls", "ttfb", "total")}
    med[name] = m
    tot = [r["total"] for r in rows] or [float("nan")]
    codes = sorted({r["code"] for r in rows})
    print(f"{name:22} {m['dns']:7.4f} {m['connect']:8.4f} {m['tls']:9.4f} {m['ttfb']:7.4f} {m['total']:7.4f}  "
          f"{min(tot):.3f}-{max(tot):.3f}   {codes}")
    check(f"{name}: all {ROUNDS} requests 200", len(rows) == ROUNDS and codes == ["200"], f"{len(rows)} rows, codes {codes}")
d = med["transparent"]["total"] - med["explicit (8080)"]["total"]
print(f"\ntransparent - explicit: {d * 1000:+.1f} ms median total; DNS share of transparent: {med['transparent']['dns'] * 1000:.1f} ms")

banner("DNS through the override")


def dig_ms(cmds):
    o = vm.sh("\n".join(cmds), timeout=120)
    return [int(l.split(":")[1].split()[0]) for l in o.splitlines() if "Query time" in l]


miss = dig_ms([f"dig +time=3 +tries=1 {rand_label()}.example.com A | grep 'Query time'" for _ in range(ROUNDS)])
hit = dig_ms(["dig +time=3 +tries=1 api.github.com A | grep 'Query time'" for _ in range(ROUNDS)])
gai = vm.sh("python3 - <<'EOF'\nimport json,random,socket,string,time\n"
            "def t(n):\n s=time.perf_counter(); socket.getaddrinfo(n,443); return round((time.perf_counter()-s)*1000,2)\n"
            "rn=lambda: 'r'+''.join(random.choice(string.ascii_lowercase) for _ in range(10))+'.example.com'\n"
            f"print(json.dumps({{'miss':[t(rn()) for _ in range({ROUNDS})],'hit':[t('api.github.com') for _ in range({ROUNDS})]}}))\nEOF", timeout=120)
g = json.loads(gai.splitlines()[-1])
vm.terminate()
print(f"dig, random name (Resolver cache miss)   median {median(miss)} ms   {miss}")
print(f"dig, api.github.com (repeated)            median {median(hit)} ms   {hit}")
print(f"getaddrinfo, random name (A + AAAA)       median {median(g['miss'])} ms   {g['miss']}")
print(f"getaddrinfo, api.github.com (repeated)    median {median(g['hit'])} ms   {g['hit']}")
check("DNS answers through the override in under 50 ms (median, cache miss)", median(miss) < 50, f"{median(miss)} ms")
summary()
