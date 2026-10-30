"""Step 9: draw the charts from results/*.json and results/*.csv.

Each chart is written twice, charts/<name>-light.png and -dark.png, so the
post can follow the reader's theme. Colors are the dataviz reference palette
(validated: blue, orange, aqua, then yellow), one y-axis per chart, a legend
whenever there are two or more series."""

import csv
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
R, C = HERE / "results", HERE / "charts"
C.mkdir(exist_ok=True)

MODES = {
    "light": {"surface": "#fcfcfb", "text": "#0b0b0b", "text2": "#52514e", "grid": "#e4e3df",
              "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]},
    "dark": {"surface": "#1a1a19", "text": "#f0efec", "text2": "#c3c2b7", "grid": "#33332f",
             "series": ["#3987e5", "#d95926", "#199e70", "#c98500"]},
}


def load(name):
    p = R / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def pct(v, p):
    v = sorted(v)
    return v[max(0, min(len(v) - 1, int(round(p / 100 * len(v) + 0.5)) - 1))] if v else float("nan")


def figure(name, title, subtitle, xlabel, ylabel, draw, legend=True, size=(8, 4.2), below=False):
    for mode, m in MODES.items():
        plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
        fig, ax = plt.subplots(figsize=size, dpi=360)
        fig.patch.set_facecolor(m["surface"]); ax.set_facecolor(m["surface"])
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color(m["grid"]); ax.spines[s].set_linewidth(1)
        ax.grid(axis="y", color=m["grid"], linewidth=1)
        ax.set_axisbelow(True)
        ax.tick_params(colors=m["text2"], length=0)
        ax.set_xlabel(xlabel, color=m["text2"]); ax.set_ylabel(ylabel, color=m["text2"])
        fig.text(0.01, 0.97, title, color=m["text"], fontsize=13, fontweight="bold", va="top")
        fig.text(0.01, 0.905, subtitle, color=m["text2"], fontsize=9.5, va="top")
        draw(ax, m)
        if legend and len(ax.get_legend_handles_labels()[0]) > 1:
            lg = (ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=4, fontsize=9)
                  if below else ax.legend(frameon=False, loc="upper left", fontsize=9))
            for t in lg.get_texts():
                t.set_color(m["text"])
        fig.tight_layout(rect=(0, 0.04 if below else 0, 1, 0.86))
        fig.savefig(C / f"{name}-{mode}.png", facecolor=m["surface"])
        plt.close(fig)
    print(f"  charts/{name}-{{light,dark}}.png")


def line(ax, x, y, color, label, m):
    ax.plot(x, y, color=color, linewidth=2, label=label, solid_capstyle="round", solid_joinstyle="round")
    ax.scatter(x, y, s=36, color=color, edgecolors=m["surface"], linewidths=2, zorder=3)


# --- density ----------------------------------------------------------------
d = load("density")
if d:
    xs = [s["running"] for s in d]
    nodes = sorted(d[0]["node_mem_gib"])

    def draw(ax, m):
        for i, n in enumerate(nodes):
            line(ax, xs, [s["node_mem_gib"][n] for s in d], m["series"][i], n.split(".")[0], m)
        ax.set_ylim(bottom=0)
    figure("density-memory", "Node memory vs running sandboxes",
           "Idle AX tasks (cpu 50m, memory 64Mi) added in batches of 10 until Substrate refused them",
           "running sandboxes", "node working set (GiB)", draw)

    def draw(ax, m):
        line(ax, xs, [pct(s["resume_ms"], 50) for s in d], m["series"][0], "p50", m)
        line(ax, xs, [pct(s["resume_ms"], 95) for s in d], m["series"][1], "p95", m)
        ax.set_ylim(bottom=0)
    figure("density-resume", "Resume latency as the cluster fills",
           "First resume of each batch of 10, timed from the ax CLI", "running sandboxes", "ms", draw)

# --- latency vs concurrency ------------------------------------------------------
lat = load("latency")
if lat:
    lv = sorted(int(k) for k in lat)
    for verb in ("create", "resume", "suspend"):
        def draw(ax, m, verb=verb):
            for i, p in enumerate((50, 95, 99)):
                line(ax, lv, [pct(lat[str(n)][verb], p) for n in lv], m["series"][i], f"p{p}", m)
            ax.set_xticks(lv); ax.set_ylim(bottom=0)
        figure(f"latency-{verb}", f"{verb.capitalize()} latency vs concurrency",
               f"N {verb} calls in flight at once ({'once' if verb == 'create' else '3 rounds'} per level), "
               "timed from the ax CLI", "calls in flight", "ms", draw)

# --- wake on request ---------------------------------------------------------------
wk = load("wake")
if wk:
    w = wk["bursts"]
    bs = sorted(int(k) for k in w)

    def draw(ax, m):
        for i, p in enumerate((50, 95, 99)):
            line(ax, bs, [pct(w[str(n)]["ms"], p) for n in bs], m["series"][i], f"p{p}", m)
        ax.set_xticks(bs); ax.set_ylim(bottom=0)
    figure("wake", "Wake-on-request latency by burst size",
           "N suspended tasks (cpu 250m) each get one HTTPS request through the router at once (curl, in-cluster)",
           "simultaneous requests", "ms", draw)

    sizes = list(wk["by_cpu"])

    def draw(ax, m):
        vals = [pct(wk["by_cpu"][c]["ms"], 50) for c in sizes]
        ax.bar([f"{c} CPU" if not c.endswith("m") else f"{c}" for c in sizes], vals, width=0.5, color=m["series"][0])
        for i, v in enumerate(vals):
            ax.text(i, v, f"{v:.0f} ms", ha="center", va="bottom", color=m["text"], fontsize=9)
        ax.set_ylim(bottom=0)
    figure("wake-by-cpu", "Wake time depends on the sandbox's CPU limit",
           "One suspended task woken through the router, 5 times per size, p50 (curl, in-cluster)",
           "task CPU limit", "ms", draw, legend=False)

# --- control plane CPU ------------------------------------------------------------
cp = load("control_plane")
if cp and (R / "control_plane.csv").exists():
    groups = {"ax-server": "ax-server-", "ate-api-server": "ate-api-server-",
              "Substrate PostgreSQL": "postgres-", "AX PostgreSQL": "ax-postgres-"}
    series = defaultdict(lambda: defaultdict(float))
    for row in csv.DictReader(open(R / "control_plane.csv")):
        for g, prefix in groups.items():
            if row["pod"].startswith(prefix):
                series[g][float(row["t"])] += float(row["cpu_m"])

    def draw(ax, m):
        for i, g in enumerate(groups):
            pts = sorted(series[g].items())
            if pts:
                ax.plot([t for t, _ in pts], [v for _, v in pts], color=m["series"][i], linewidth=2, label=g)
        for verb, ph in cp.items():
            ax.axvspan(ph["start"], ph["end"], color=m["grid"], alpha=0.35, lw=0)
            ax.text((ph["start"] + ph["end"]) / 2, ax.get_ylim()[1] * 0.97, verb, ha="center", va="top",
                    color=m["text2"], fontsize=8.5)
        ax.set_ylim(bottom=0)
    figure("control-plane-cpu", "Control-plane CPU during 120-task waves",
           "Sum over replicas, from kubelet stats every ~5 s; shaded: create, resume, suspend, delete phases",
           "seconds", "CPU (millicores)", draw, size=(8, 4.8), below=True)

# --- noisy neighbors ----------------------------------------------------------------
nz = load("noisy")
if nz:
    scen = [k for k in ("baseline", "cpu", "disk", "memory") if k in nz]
    for metric, label in (("cpu_ms", "CPU benchmark (ms, p50)"), ("io_ms", "64 MB write + fsync (ms, p50)"),
                          ("resume_ms", "resume (ms, p50)")):
        def draw(ax, m, metric=metric):
            vals = [pct(nz[s][metric], 50) for s in scen]
            ax.bar(scen, vals, width=0.5, color=m["series"][0])
            for i, v in enumerate(vals):
                ax.text(i, v, f"{v:.0f}", ha="center", va="bottom", color=m["text"], fontsize=9)
            ax.set_ylim(bottom=0)
        figure(f"noisy-{metric.split('_')[0]}", f"Victims: {label}",
               "Six victim tasks on the same two workers; 4 hog tasks added per scenario",
               "neighbors", label.split(" (")[0], draw, legend=False)

# --- snapshot storm -------------------------------------------------------------------
ss = load("snapshot_storm")
if ss:
    def draw(ax, m):
        t = [p[0] / 60 for p in ss["timeline"]]
        ax.step(t, [p[1] for p in ss["timeline"]], where="post", color=m["series"][0], linewidth=2)
        ax.set_ylim(0, max(p[1] for p in ss["timeline"]) + 1)
    figure("snapshot-durable", "Snapshots durable in S3 after a 20-task suspend",
           "100 MB of random data per task; atelet uploads in the background after a 5-minute delay",
           "minutes after suspend", "tasks with a durable snapshot", draw, legend=False)

# --- CloudWatch: node CPU -------------------------------------------------------------
cw = load("cloudwatch")
if cw and cw.get("node_cpu"):
    def draw(ax, m):
        for i, s in enumerate(cw["node_cpu"][:3]):
            pts = sorted(zip(s["t"], s["v"]))
            if not pts:
                continue
            t0 = dt.datetime.fromisoformat(pts[0][0])
            ax.plot([(dt.datetime.fromisoformat(t) - t0).total_seconds() / 60 for t, _ in pts],
                    [v for _, v in pts], color=m["series"][i], linewidth=2, label=s["label"].split(".")[0][-20:])
        ax.set_ylim(0, 100)
    figure("cloudwatch-node-cpu", "Node CPU through the whole test (Container Insights)",
           "node_cpu_utilization, 1-minute averages", "minutes", "% CPU", draw)

# --- CloudWatch: NAT gateway traffic ------------------------------------------------------
if cw and cw.get("nat_out"):
    s0 = max(cw["nat_out"], key=lambda s: len(s["v"]))  # the current NAT gateway
    pts = sorted(zip(s0["t"], s0["v"]))
    if pts:
        def draw(ax, m):
            t0 = dt.datetime.fromisoformat(pts[0][0])
            ax.bar([(dt.datetime.fromisoformat(t) - t0).total_seconds() / 60 for t, _ in pts],
                   [v / 1e6 for _, v in pts], width=0.7, color=m["series"][0])
            ax.set_ylim(bottom=0)
        figure("cloudwatch-nat", "NAT gateway traffic during the tests",
               "BytesOutToDestination per minute; the spike is 20 snapshots (2 GB) uploading to S3",
               "minutes", "MB per minute", draw, legend=False)
