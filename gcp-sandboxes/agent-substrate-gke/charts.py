"""Draw the charts from results/*.json (this run, Substrate on GKE) next to eks-baseline/*.json (the AX-on-EKS runs,
copied from google-ax-stress/results). Both sets come from the same harness, so each chart draws two series on the
same axes. Each chart is written twice, charts/<name>-light.png and -dark.png.

Colours are the dataviz reference palette, validated for both themes: blue for GKE, orange for EKS."""

import csv
import datetime as dt
import json
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

HERE = Path(__file__).resolve().parent
R, E, C = HERE / "results", HERE / "eks-baseline", HERE / "charts"
C.mkdir(exist_ok=True)
GKE, EKS = "Substrate on GKE (c3-standard-4)", "AX on EKS (m7i.xlarge)"

MODES = {
    "light": {"surface": "#fcfcfb", "text": "#0b0b0b", "text2": "#52514e", "grid": "#e4e3df", "gke": "#2a78d6", "eks": "#eb6834", "third": "#1baf7a"},
    "dark": {"surface": "#1a1a19", "text": "#f0efec", "text2": "#c3c2b7", "grid": "#33332f", "gke": "#3987e5", "eks": "#d95926", "third": "#199e70"},
}


def load(d, name):
    p = d / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else None


def pct(v, p):
    v = sorted(v)
    return v[max(0, min(len(v) - 1, int(round(p / 100 * len(v) + 0.5)) - 1))] if v else float("nan")


def figure(name, title, subtitle, xlabel, ylabel, draw, legend=True, size=(8, 4.2)):
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
            lg = ax.legend(frameon=False, loc="upper left", fontsize=9)
            for t in lg.get_texts():
                t.set_color(m["text"])
        fig.tight_layout(rect=(0, 0, 1, 0.86))
        fig.savefig(C / f"{name}-{mode}.png", facecolor=m["surface"])
        plt.close(fig)
    print(f"  charts/{name}-{{light,dark}}.png")


def line(ax, x, y, color, label, m, dashed=False):
    ax.plot(x, y, color=color, linewidth=2, label=label, linestyle="--" if dashed else "-",
            solid_capstyle="round", solid_joinstyle="round")
    if not dashed:
        ax.scatter(x, y, s=36, color=color, edgecolors=m["surface"], linewidths=2, zorder=3)


def bars2(ax, m, labels, gke, eks, fmt="{:.0f}"):
    w = 0.38
    xs = range(len(labels))
    for off, vals, key, lab in ((-w / 2, gke, "gke", GKE), (w / 2, eks, "eks", EKS)):
        ax.bar([x + off for x in xs], vals, width=w, color=m[key], label=lab)
        for x, v in zip(xs, vals):
            ax.text(x + off, v, fmt.format(v), ha="center", va="bottom", color=m["text"], fontsize=8.5)
    ax.set_xticks(list(xs)); ax.set_xticklabels(labels); ax.set_ylim(bottom=0, top=max(gke + eks) * 1.15)


# --- latency vs concurrency: p50 solid, p95 dashed ----------------------------------------------
lat, lat_e = load(R, "latency"), load(E, "latency")
if lat and lat_e:
    lv = sorted(int(k) for k in lat)
    for verb in ("create", "resume", "suspend"):
        def draw(ax, m, verb=verb):
            for data, key, name in ((lat, "gke", GKE), (lat_e, "eks", EKS)):
                line(ax, lv, [pct(data[str(n)][verb], 50) for n in lv], m[key], f"{name}, p50", m)
                line(ax, lv, [pct(data[str(n)][verb], 95) for n in lv], m[key], f"{name}, p95", m, dashed=True)
            ax.set_xticks(lv); ax.set_ylim(bottom=0)
        figure(f"latency-{verb}", f"{verb.capitalize()} latency vs concurrency",
               f"N {verb} calls in flight at once ({'once' if verb == 'create' else '3 rounds'} per level), timed from the ax CLI",
               "calls in flight", "ms", draw)

# --- wake on request ----------------------------------------------------------------------------
wk, wk_e = load(R, "wake"), load(E, "wake")
if wk and wk_e:
    sizes = list(wk["by_cpu"])

    def draw(ax, m):
        bars2(ax, m, [f"{c} CPU" if not c.endswith("m") else c for c in sizes],
              [pct(wk["by_cpu"][c]["ms"], 50) for c in sizes], [pct(wk_e["by_cpu"][c]["ms"], 50) for c in sizes])
    figure("wake-by-cpu", "Wake time depends on the sandbox's CPU limit",
           "One suspended task woken through the router, 5 times per size, p50 (curl, in-cluster)",
           "task CPU limit", "ms", draw)

    bs = sorted(int(k) for k in wk["bursts"])

    def draw(ax, m):
        for data, key, name in ((wk, "gke", GKE), (wk_e, "eks", EKS)):
            line(ax, bs, [pct(data["bursts"][str(n)]["ms"], 50) for n in bs], m[key], f"{name}, p50", m)
            line(ax, bs, [pct(data["bursts"][str(n)]["ms"], 95) for n in bs], m[key], f"{name}, p95", m, dashed=True)
        ax.set_xticks(bs); ax.set_ylim(bottom=0)
    figure("wake", "Wake-on-request latency by burst size",
           "N suspended tasks (cpu 250m) each get one HTTPS request through the router at once (curl, in-cluster)",
           "simultaneous requests", "ms", draw)

# --- density ------------------------------------------------------------------------------------
d, d_e = load(R, "density"), load(E, "density")
if d and d_e:
    def draw(ax, m):
        for data, key, name in ((d, "gke", GKE), (d_e, "eks", EKS)):
            line(ax, [s["running"] for s in data], [pct(s["resume_ms"], 50) for s in data], m[key], name, m)
        ax.set_ylim(bottom=0)
    figure("density-resume", "Resume latency as the cluster fills",
           "p50 of each batch of 10 idle tasks (cpu 50m, memory 64Mi), added until Substrate refused", "running sandboxes", "ms", draw)

    def draw(ax, m):
        for data, key, name in ((d, "gke", GKE), (d_e, "eks", EKS)):
            line(ax, [s["running"] for s in data], [sum(s["node_mem_gib"].values()) / len(s["node_mem_gib"]) for s in data],
                 m[key], name, m)
        ax.set_ylim(bottom=0)
    figure("density-memory", "Node memory vs running sandboxes",
           "Mean working set of the two nodes (kubelet stats), same idle tasks", "running sandboxes", "GiB per node", draw)

# --- noisy neighbours ---------------------------------------------------------------------------
nz, nz_e = load(R, "noisy"), load(E, "noisy")
if nz and nz_e:
    scen = [k for k in ("baseline", "cpu", "disk", "memory") if k in nz and k in nz_e]
    for metric, p, label, name in (("cpu_ms", 50, "CPU benchmark (ms, p50)", "noisy-cpu"),
                                   ("io_ms", 95, "64 MB write + fsync (ms, p95)", "noisy-io"),
                                   ("suspend_ms", 95, "suspend (ms, p95)", "noisy-suspend")):
        def draw(ax, m, metric=metric, p=p):
            bars2(ax, m, scen, [pct(nz[s][metric], p) for s in scen], [pct(nz_e[s][metric], p) for s in scen])
        figure(name, f"Victims: {label}", "Six victim tasks on the same two workers; four hog tasks of one kind added per scenario",
               "neighbours", label.split(" (")[0], draw)

# --- snapshot storm -----------------------------------------------------------------------------
ss, ss_e = load(R, "snapshot_storm"), load(E, "snapshot_storm")
if ss and ss_e:
    def draw(ax, m):
        for data, key, name in ((ss, "gke", GKE), (ss_e, "eks", EKS)):
            ax.step([p[0] / 60 for p in data["timeline"]], [p[1] for p in data["timeline"]], where="post",
                    color=m[key], linewidth=2, label=name)
        ax.set_ylim(0, 21)
    figure("snapshot-durable", "Snapshots in object storage after a 20-task suspend",
           "100 MB of random data per task; atelet uploads in the background after a 5-minute delay",
           "minutes after suspend", "tasks with a durable snapshot", draw)

# --- control-plane CPU --------------------------------------------------------------------------
cp = load(R, "control_plane")
if cp and (R / "control_plane.csv").exists():
    groups = {"ax-server": "ax-server-", "ate-api-server": "ate-api-server-",
              "Substrate PostgreSQL": "postgres-", "AX PostgreSQL": "ax-postgres-"}
    series = defaultdict(lambda: defaultdict(float))
    for row in csv.DictReader(open(R / "control_plane.csv")):
        for g, prefix in groups.items():
            if row["pod"].startswith(prefix):
                series[g][float(row["t"])] += float(row["cpu_m"])

    def draw(ax, m):
        cols = [m["gke"], m["eks"], m["third"], m["text2"]]
        for i, g in enumerate(groups):
            pts = sorted(series[g].items())
            if pts:
                ax.plot([t for t, _ in pts], [v for _, v in pts], color=cols[i], linewidth=2, label=g)
        for verb, ph in cp.items():
            ax.axvspan(ph["start"], ph["end"], color=m["grid"], alpha=0.35, lw=0)
            ax.text((ph["start"] + ph["end"]) / 2, ax.get_ylim()[1] * 0.97, verb, ha="center", va="top", color=m["text2"], fontsize=8.5)
        ax.set_ylim(bottom=0)
    figure("control-plane-cpu", "Control-plane CPU during 120-task waves",
           "Sum over replicas, from kubelet stats every ~3 s; shaded: create, resume, suspend, delete phases",
           "seconds", "CPU (millicores)", draw, size=(8, 4.8))

# --- where the snapshot bytes went: Cloud NAT vs Cloud Storage ----------------------------------
mon = load(R, "monitoring")
if mon and ss:
    def per_min(key):
        tot = defaultdict(float)
        for s in mon[key]:
            for t, v in zip(s["t"], s["v"]):
                tot[t] += v
        return tot
    gcs, nat = per_min("gcs_received"), per_min("nat_sent")
    stamps = sorted(set(gcs) | set(nat))
    t0 = dt.datetime.fromisoformat(stamps[0].replace("Z", "+00:00"))
    mins = [(dt.datetime.fromisoformat(t.replace("Z", "+00:00")) - t0).total_seconds() / 60 for t in stamps]

    def draw(ax, m):
        ax.bar(mins, [gcs.get(t, 0) / 1e6 for t in stamps], width=0.8, color=m["gke"], label="Cloud Storage: bytes received")
        ax.bar(mins, [nat.get(t, 0) / 1e6 for t in stamps], width=0.8, color=m["eks"], label="Cloud NAT: bytes sent")
        ax.set_ylim(bottom=0)
    figure("nat-vs-gcs", "Where 20 snapshots (2 GB) went: Cloud Storage, not Cloud NAT",
           "Cloud Monitoring, per minute, two 20-task storms; the EKS run pushed 2.1 GB through its NAT gateway",
           "minutes", "MB per minute", draw)
