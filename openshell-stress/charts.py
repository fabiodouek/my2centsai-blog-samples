"""Draw the part 6 charts from the CSV/JSON files the tests write. Each chart is written twice,
<name>-light.png and <name>-dark.png (2880 px wide), so the post can follow the reader's theme.
Charts whose input files are missing are skipped.

    uv run python charts.py                                   # results/ -> charts/
    uv run python charts.py --results /tmp/smoke --out /tmp/smoke/charts

Colors: the validated categorical palette (blue, orange, aqua, yellow), stepped separately for
the light and dark surfaces; one y-axis per chart; direct labels where there are few series.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import matplotlib.transforms  # noqa: E402
from matplotlib.ticker import MaxNLocator  # noqa: E402

HERE = Path(__file__).resolve().parent
BLOG_IMAGES = Path(__file__).resolve().parent / "charts"   # --out ../path/to/blog/public/images/deep-dive/openshell-stress
MODES = {
    "light": {"surface": "#fcfcfb", "text": "#0b0b0b", "text2": "#52514e", "grid": "#e4e3df", "ref": "#8a8984",
              "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"], "band": "#f0efec"},
    "dark": {"surface": "#1a1a19", "text": "#f0efec", "text2": "#c3c2b7", "grid": "#33332f", "ref": "#8d8c85",
             "series": ["#3987e5", "#d95926", "#199e70", "#c98500"], "band": "#262624"},
}
OUT: Path = BLOG_IMAGES
written: list[str] = []


# --- loading -------------------------------------------------------------------------------

def rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def num(v, default=math.nan) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def short_node(n: str) -> str:
    return n.split(".")[0]


# --- drawing -------------------------------------------------------------------------------

def figure(name: str, title: str, subtitle: str, xlabel: str, ylabel: str, draw, legend: str = "auto",
           size=(8, 4.2)) -> None:
    for mode, m in MODES.items():
        plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
        fig, ax = plt.subplots(figsize=size, dpi=360)
        fig.patch.set_facecolor(m["surface"])
        ax.set_facecolor(m["surface"])
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(m["grid"])
        ax.grid(axis="y", color=m["grid"], linewidth=1)
        ax.set_axisbelow(True)
        ax.tick_params(colors=m["text2"], length=0)
        ax.set_xlabel(xlabel, color=m["text2"])
        ax.set_ylabel(ylabel, color=m["text2"])
        fig.text(0.01, 0.97, title, color=m["text"], fontsize=13, fontweight="bold", va="top")
        fig.text(0.01, 0.905, subtitle, color=m["text2"], fontsize=9.5, va="top")
        draw(ax, m)
        place_end_labels(ax, m)
        handles, labels = ax.get_legend_handles_labels()
        below = legend == "below"
        if legend != "none" and len(handles) > 1:
            if below:
                lg = ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=len(handles), fontsize=9)
            else:
                lg = ax.legend(frameon=False, loc="upper left" if legend == "auto" else legend, fontsize=9)
            for t in lg.get_texts():
                t.set_color(m["text"])
        fig.tight_layout(rect=(0, 0.05 if below else 0, 1, 0.86))
        fig.savefig(OUT / f"{name}-{mode}.png", facecolor=m["surface"])
        plt.close(fig)
    written.append(name)
    print(f"  {OUT}/{name}-{{light,dark}}.png")


def line(ax, x, y, color, label, m, style="-", end_label=True):
    ax.plot(x, y, color=color, linewidth=2, linestyle=style, label=label, solid_capstyle="round")
    ax.scatter(x, y, s=30, color=color, edgecolors=m["surface"], linewidths=1.5, zorder=3)
    if end_label and len(x):
        ax._p6_labels = getattr(ax, "_p6_labels", []) + [(x[-1], y[-1], label)]


def place_end_labels(ax, m) -> None:
    """Direct labels at the line ends, nudged apart vertically so they never overlap."""
    items = sorted(getattr(ax, "_p6_labels", []), key=lambda i: i[1])
    if not items:
        return
    lo, hi = ax.get_ylim()
    gap, last = (hi - lo) * 0.055, -math.inf
    shift = ax.transData + matplotlib.transforms.ScaledTranslation(6 / 72, 0, ax.figure.dpi_scale_trans)
    for x, y, label in items:
        last = max(y, last + gap)
        ax.text(x, last, label, transform=shift, va="center", fontsize=8.5, color=m["text2"], clip_on=False)


def ref_line(ax, y, text, m):
    ax.axhline(y, color=m["ref"], linewidth=1, linestyle=(0, (4, 3)))
    ax.annotate(text, (0, y), xycoords=("axes fraction", "data"), xytext=(4, 4), textcoords="offset points",
                fontsize=8.5, color=m["text2"])


def hbars(ax, labels, values, m, color, errs=None, fmt="{:.1f}", height=0.6, offset=0.0, label=None):
    ys = [i + offset for i in range(len(labels))]
    ax.barh(ys, values, height=height, color=color, label=label)
    if errs:
        ax.scatter(errs, ys, marker="|", s=120, color=m["text"], zorder=3, linewidths=1.5)
    for y, v in zip(ys, values):
        if not math.isnan(v):
            ax.annotate(fmt.format(v), (v, y), xytext=(4, 0), textcoords="offset points", va="center",
                        fontsize=8, color=m["text2"])
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels)
    if not ax.yaxis_inverted():  # first row at the top, also when called once per series
        ax.invert_yaxis()
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=m["grid"], linewidth=1)


def cdf(values):
    """Step-plot points of an empirical CDF, starting at 0 % at x = 0."""
    v = sorted(x for x in values if not math.isnan(x))
    return [0.0, *v], [0.0, *[100 * (i + 1) / len(v) for i in range(len(v))]]


# --- test 1: density -------------------------------------------------------------------------

VARIANT_LABEL = {"dedicated": "dedicated supervisors", "shared": "shared supervisor pool",
                 "shared-1gi": "shared pool, 1 GiB workspaces"}


def density(results: Path) -> None:
    ceilings = []
    for variant, label in VARIANT_LABEL.items():
        b = rows(results / f"density-{variant}-batches.csv")
        summ = results / f"density-{variant}-summary.json"
        if not b:
            continue
        s = json.loads(summ.read_text()) if summ.exists() else {}
        ceilings.append((label, s.get("max_ready", num(b[-1]["ready"])), s.get("stopped_by", "")))
        limits = s.get("limits", {})
        node_keys = sorted({k[:-5] for k in b[0] if k.startswith("node") and k.endswith("_pods") and "sandbox" not in k})
        base = s.get("baseline", {}).get("nodes", {})
        if base:  # a first point at 0 sandboxes: what the nodes ran before the test
            first = {"requested": 0, "ready": 0}
            for nk in node_keys:
                d = base.get(b[0][nk], {})
                first.update({nk: b[0][nk], f"{nk}_pods": d.get("pods", 0), f"{nk}_ebs": d.get("volume_attachments", 0)})
            b = [first, *b]
        x = [num(r["requested"]) for r in b]

        def draw(ax, m, b=b, node_keys=node_keys, x=x, limits=limits):
            for i, nk in enumerate(node_keys):
                node = b[0][nk]
                line(ax, x, [num(r[f"{nk}_pods"]) for r in b], m["series"][i], f"{short_node(node)} Pods", m)
                line(ax, x, [num(r[f"{nk}_ebs"]) for r in b], m["series"][i], f"{short_node(node)} EBS volumes", m,
                     style=(0, (2, 2)))
            lim = next(iter(limits.values()), {})
            if lim.get("max_pods"):
                ref_line(ax, lim["max_pods"], f"max Pods per node ({lim['max_pods']})", m)
            if lim.get("ebs_limit"):
                ref_line(ax, lim["ebs_limit"], f"EBS attachments per node, CSINode ({lim['ebs_limit']})", m)
            ax.set_ylim(bottom=0, top=max(62, ax.get_ylim()[1]))
            ax.xaxis.set_major_locator(MaxNLocator(integer=True))
            ax.margins(x=0.12)
        figure(f"density-{variant}", f"Density: {label}",
               f"Pods and attached EBS volumes per node as sandboxes are created; stopped by: {s.get('stopped_by', '?')[:70]}",
               "sandboxes requested", "count per node", draw, legend="none")

        sampler = rows(results / f"density-{variant}-sampler.csv")
        if sampler and "t_end" in b[-1]:
            pts = []
            for r in b[1:] if base else b:
                t = num(r["t_end"])
                near = [s for s in sampler if abs(num(s["ts"]) - t) < 3]
                if near:
                    by_node = {}
                    for s_ in near:
                        by_node[s_["node"]] = num(s_["node_mem_ws_mib"])
                    pts.append((num(r["ready"]), sum(by_node.values()) / 1024))
            first_ts = min(num(x["ts"]) for x in sampler)
            start = {x["node"]: num(x["node_mem_ws_mib"]) for x in sampler if num(x["ts"]) - first_ts < 1}
            if start:  # before the first create: what the nodes used with no test sandboxes
                pts.insert(0, (0, sum(start.values()) / 1024))
            if pts:
                def draw_mem(ax, m, pts=pts):
                    line(ax, [p[0] for p in pts], [p[1] for p in pts], m["series"][0], "both nodes", m)
                    ax.set_ylim(bottom=0)
                    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
                figure(f"density-{variant}-memory", f"Node memory: {label}",
                       "Working-set memory of both nodes together (kubelet summary API) against Ready sandboxes",
                       "Ready sandboxes", "GiB", draw_mem, legend="none")

    if ceilings:
        def draw_ceiling(ax, m):
            labels = [f"{c[0]}\nstopped by: {c[2].split(' (')[0]}" for c in ceilings]
            hbars(ax, labels, [num(c[1]) for c in ceilings], m, m["series"][0], fmt="{:.0f}",
                  height=min(0.6, 0.25 * len(ceilings) + 0.1))
            ax.tick_params(axis="y", labelsize=8.5)
            ax.xaxis.set_major_locator(MaxNLocator(integer=True))
            ax.set_ylim(max(2.5, len(ceilings) - 0.4), -0.6)  # room for three variants, top to bottom
        figure("density-ceiling", "How many sandboxes two nodes hold",
               "Most Ready sandboxes before the first create failed or got stuck, and what stopped it",
               "Ready sandboxes", "", draw_ceiling, legend="none")


# --- test 2: create latency ------------------------------------------------------------------

def create_latency(results: Path) -> None:
    sets = [(t, rows(results / f"create-{t}-levels.csv")) for t in ("dedicated", "shared")]
    sets = [(t, r) for t, r in sets if r]
    if not sets:
        return

    def draw(ax, m):
        for i, (topo, lv) in enumerate(sets):
            lv = sorted(lv, key=lambda r: num(r["level"]))
            x = [num(r["level"]) for r in lv]
            line(ax, x, [num(r["ready_p50_s"]) for r in lv], m["series"][i], f"{topo} p50", m)
            line(ax, x, [num(r["ready_p95_s"]) for r in lv], m["series"][i], f"{topo} p95", m, style=(0, (2, 2)))
        ax.set_xticks(sorted({num(r["level"]) for _, lv in sets for r in lv}))
        ax.set_ylim(bottom=0)
        ax.margins(x=0.1)
    figure("create-latency", "Create latency vs concurrency",
           "N CreateSandbox calls sent at once; seconds from the call to Ready, per sandbox",
           "creates in flight", "seconds to Ready", draw, legend="none")


# --- test 3: policy propagation --------------------------------------------------------------

def policy(results: Path) -> None:
    files = sorted(results.glob("policy-*-*.csv"))
    files = [f for f in files if not f.name.endswith(("-sampler.csv", "-gateway.csv"))]
    if not files:
        return
    data = [(f.stem.removeprefix("policy-"), rows(f)) for f in files]

    def draw_allow(ax, m):
        for i, (label, rs) in enumerate(data[:4]):
            x, y = cdf([num(r["probe_allow_s"]) for r in rs])
            ax.step(x, y, where="post", color=m["series"][i], linewidth=2, label=label.replace("-", ", "))
        ax.set_ylim(0, 105)
        ax.set_xlim(left=0)
    figure("policy-propagation", "Policy propagation",
           "Seconds from the policy revision's creation until each sandbox's probe first got through",
           "seconds after the change", "% of sandboxes", draw_allow, legend="center right")

    def draw_cut(ax, m):
        for i, (label, rs) in enumerate(data[:4]):
            x, y = cdf([num(r["cut_s"]) for r in rs])
            ax.step(x, y, where="post", color=m["series"][i], linewidth=2, label=label.replace("-", ", "))
        ax.set_ylim(0, 105)
        ax.set_xlim(left=0)
    figure("policy-connections-cut", "Open connections closed by a new policy",
           "Seconds from an unrelated policy change until each sandbox's long-lived response was cut",
           "seconds after the change", "% of sandboxes", draw_cut)


# --- test 4: egress ladder -------------------------------------------------------------------

def ladder(results: Path) -> None:
    same, other = rows(results / "ladder.csv"), rows(results / "ladder-other.csv")
    if not same and not other:
        return
    sets = [(lbl, [r for r in rs if r.get("fresh_p50_ms")]) for lbl, rs in
            (("supervisor on the workload's node", same), ("supervisor on the other node", other)) if rs]

    def draw_set(prefix_filter, metric, err_metric=None):
        def draw(ax, m):
            labels = []
            for _, rs in sets:
                for r in rs:
                    if prefix_filter(r["rung"]) and r["rung"] not in labels:
                        labels.append(r["rung"])
            h = 0.8 / len(sets)
            for i, (lbl, rs) in enumerate(sets):
                by = {r["rung"]: r for r in rs}
                vals = [num(by.get(k, {}).get(metric)) for k in labels]
                errs = [num(by.get(k, {}).get(err_metric)) for k in labels] if err_metric else None
                hbars(ax, labels, vals, m, m["series"][i], errs=errs, height=h * 0.9,
                      offset=(i - (len(sets) - 1) / 2) * h, label=lbl, fmt="{:.1f}")
            ax.set_xlim(left=0)
            ax.margins(x=0.15)
        return draw

    echo = lambda r: not r.startswith("https")  # noqa: E731
    https = lambda r: r.startswith("https")  # noqa: E731
    figure("ladder-latency", "Egress overhead: request latency",
           "One GET to the in-cluster echo server on a new connection, from inside the sandbox; bar p50, tick p95",
           "milliseconds", "", draw_set(echo, "fresh_p50_ms", "fresh_p95_ms"), legend="below")
    figure("ladder-throughput", "Egress overhead: requests per second",
           "8 parallel Python clients, new connection per request, against the echo server",
           "requests per second", "", draw_set(echo, "rps"), legend="below")
    figure("ladder-upload", "Egress overhead: upload throughput",
           "1 MiB POST bodies to the echo server, one per new connection",
           "MiB per second", "", draw_set(echo, "upload_mib_s"), legend="below")
    if any(https(r["rung"]) for _, rs in sets for r in rs):
        figure("ladder-https", "Egress overhead: HTTPS to an AWS endpoint",
               "GET / to s3.us-east-1.amazonaws.com through the NAT gateway, kept-alive connection; bar p50, tick p95",
               "milliseconds", "", draw_set(https, "keepalive_p50_ms", "keepalive_p95_ms"), legend="below")


# --- test 5: control plane -------------------------------------------------------------------

def control(results: Path) -> None:
    for origin in ("cluster", "laptop"):
        st = rows(results / f"control-{origin}-steps.csv")
        if not st:
            continue
        st = sorted(st, key=lambda r: num(r["level"]))
        x = [num(r["level"]) for r in st]
        ops = [op for op in ("list", "get", "exec") if f"{op}_p50_ms" in st[0]]

        def draw_lat(ax, m, st=st, x=x, ops=ops):
            for i, op in enumerate(ops):
                line(ax, x, [num(r[f"{op}_p95_ms"]) for r in st], m["series"][i], f"{op} p95", m)
            ax.set_xscale("log", base=2)
            ax.set_xticks(x)
            ax.set_xticklabels([f"{v:.0f}" for v in x])
            ax.set_ylim(bottom=0)
            ax.margins(x=0.12)
        figure(f"control-{origin}-latency", "API latency under load",
               f"Parallel clients ({'in the cluster' if origin == 'cluster' else 'on the laptop, through the port-forward'}) "
               "cycling list, get and exec; p95 per call",
               "parallel clients", "milliseconds", draw_lat, legend="none")

        def draw_cpu(ax, m, st=st, x=x):
            line(ax, x, [num(r.get("gw_cpu_cores")) for r in st], m["series"][0], "gateway", m, end_label=False)
            ax.set_xscale("log", base=2)
            ax.set_xticks(x)
            ax.set_xticklabels([f"{v:.0f}" for v in x])
            ax.set_ylim(bottom=0)
            ax.margins(x=0.12)
        if any(math.isfinite(num(r.get("gw_cpu_cores"))) for r in st):
            figure(f"control-{origin}-gateway-cpu", "Gateway CPU under API load",
                   "CPU of the gateway Pod over each step, from the kubelet's cumulative CPU counter",
                   "parallel clients", "CPU cores", draw_cpu, legend="none")
        else:
            print(f"  (control-{origin}-gateway-cpu skipped: no gw_cpu_cores in the steps CSV)")

        def draw_rate(ax, m, st=st, x=x):
            line(ax, x, [num(r["calls_per_s"]) for r in st], m["series"][0], "calls/s", m, end_label=False)
            ax.set_xscale("log", base=2)
            ax.set_xticks(x)
            ax.set_xticklabels([f"{v:.0f}" for v in x])
            ax.set_ylim(bottom=0)
        figure(f"control-{origin}-rate", "API calls per second",
               "Completed list, get and exec calls per second across all clients",
               "parallel clients", "calls per second", draw_rate, legend="none")


# --- test 6: noisy neighbours ----------------------------------------------------------------

def noisy(results: Path) -> None:
    for probe_file in sorted(results.glob("noisy*-probe.csv")):
        tag = probe_file.name.removesuffix("-probe.csv")
        samples = rows(probe_file)
        phases = rows(results / f"{tag}-phases.csv")
        if not samples:
            continue
        t = [num(s["t_rel"]) for s in samples]

        def bands(ax, m, phases=phases):
            for p in phases:
                if p.get("t_off_rel"):
                    ax.axvspan(num(p["t_on_rel"]), num(p["t_off_rel"]), color=m["band"], zorder=0)
                    ax.annotate(f"{p['phase']} hogs", (num(p["t_on_rel"]), 1), xycoords=("data", "axes fraction"),
                                xytext=(4, -12), textcoords="offset points", fontsize=8.5, color=m["text2"])
            ax.set_xlim(left=0)
            ax.set_ylim(bottom=0)

        def draw_cpu(ax, m, samples=samples, t=t):
            ax.plot(t, [num(s["cpu_ms"]) for s in samples], color=m["series"][0], linewidth=1.5, label="CPU work")
            bands(ax, m)
        figure(f"{tag}-cpu", "Noisy neighbours: the victim's CPU work",
               "Time for a fixed piece of Python work in the probe sandbox, once a second; shaded: hogs running",
               "seconds", "milliseconds", draw_cpu, legend="none")

        def draw_io(ax, m, samples=samples, t=t):
            ax.plot(t, [num(s["fsync_ws_ms"]) for s in samples], color=m["series"][0], linewidth=1.5,
                    label="own EBS workspace (/sandbox)")
            ax.plot(t, [num(s["fsync_tmp_ms"]) for s in samples], color=m["series"][1], linewidth=1.5,
                    label="node root volume (/tmp)")
            bands(ax, m)
        figure(f"{tag}-io", "Noisy neighbours: the victim's disk writes",
               "4 KiB write + fsync in the probe sandbox, once a second; shaded: hogs running",
               "seconds", "milliseconds", draw_io, legend="below")

        def draw_net(ax, m, samples=samples, t=t):
            ax.plot(t, [num(s["net_ms"]) for s in samples], color=m["series"][0], linewidth=1.5, label="echo request")
            bands(ax, m)
        figure(f"{tag}-net", "Noisy neighbours: the victim's network requests",
               "One GET to the echo server through the probe's supervisor, new connection, once a second",
               "seconds", "milliseconds", draw_net, legend="none")


def main() -> None:
    global OUT
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--results", type=Path, default=HERE / "results")
    ap.add_argument("--out", type=Path, default=BLOG_IMAGES)
    args = ap.parse_args()
    OUT = args.out
    OUT.mkdir(parents=True, exist_ok=True)
    for fn in (density, create_latency, policy, ladder, control, noisy):
        fn(args.results)
    print(f"{len(written)} chart(s) in {OUT}")


if __name__ == "__main__":
    main()
