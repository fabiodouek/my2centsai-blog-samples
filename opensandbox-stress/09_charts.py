"""Draw the charts from results/*.json and results/*.csv: one light and one dark PNG per chart, at
2x (1600 x 900 px for an 800 x 450 slot), so the post can follow the reader's theme.

  uv run python 09_charts.py                      every chart whose results exist
  uv run python 09_charts.py --results DIR --charts DIR

Style: one y-axis per chart; 2 px lines with 8 px markers ringed in the surface color; thin bars
with a rounded data end, square at the baseline; solid hairline grid; text in the text colors,
never in a series color; a legend whenever there are two or more series, plus a label at the end
of each line. Series colors are the first slots of a validated categorical palette (blue, orange,
aqua, yellow), stepped separately for the dark surface; a chart with one series uses slot 1 for
every mark. The numbers behind every chart are in the results JSON (the table view).
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from matplotlib.ticker import FixedLocator, FuncFormatter, MaxNLocator, NullFormatter  # noqa: E402

from measure import mem_mib_of, read_csv  # noqa: E402

HERE = Path(__file__).resolve().parent
SIZE = (8, 4.5)
DPI = 200
THEMES = {
    "light": {"surface": "#fcfcfb", "text": "#0b0b0b", "text2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
              "axis": "#c3c2b7", "shade": "#f0efec", "series": ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]},
    "dark": {"surface": "#1a1a19", "text": "#f0efec", "text2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
             "axis": "#383835", "shade": "#262624", "series": ["#3987e5", "#d95926", "#199e70", "#c98500"]},
}
DRAWN: list[str] = []


# --- the frame -----------------------------------------------------------------------------------

def ticks(axis_top: float) -> FuncFormatter:
    """One style per axis: 0 / 1,000 / 2,000 in full when the axis stays under 10,000; compact
    (0 / 10K / 20K) on an axis that reaches it."""
    def fmt(value, _pos=None) -> str:
        if axis_top >= 10000:
            return f"{value / 1000:,.0f}K" if value else "0"
        return f"{value:,.0f}" if value == int(value) else f"{value:,.1f}"
    return FuncFormatter(fmt)


def figure(out: Path, name: str, title: str, subtitle: str, xlabel: str, ylabel: str, draw, *,
           left: float = 0.09, right: float = 0.97, legend: bool = True) -> None:
    """Draw `draw(ax, theme)` twice, light and dark, into charts/<name>-{light,dark}.png. The axes
    have a fixed position (no tight_layout), so the bar helper's pixel math holds. Leave `right`
    room for labels at the end of lines."""
    for mode, m in THEMES.items():
        plt.rcParams.update({"font.family": "sans-serif",
                             "font.sans-serif": ["Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans"],
                             "font.size": 10, "axes.unicode_minus": False})
        fig = plt.figure(figsize=SIZE, dpi=DPI)
        fig.patch.set_facecolor(m["surface"])
        ax = fig.add_axes((left, 0.14, right - left, 0.66))
        ax.set_facecolor(m["surface"])
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(m["axis"])
            ax.spines[side].set_linewidth(1)
        ax.grid(axis="y", color=m["grid"], linewidth=0.8, linestyle="-")
        ax.set_axisbelow(True)
        ax.tick_params(colors=m["text2"], length=0, labelsize=9)
        ax.set_xlabel(xlabel, color=m["text2"], fontsize=9.5)
        ax.set_ylabel(ylabel, color=m["text2"], fontsize=9.5)
        fig.text(0.015, 0.965, title, color=m["text"], fontsize=13.5, fontweight="bold", va="top")
        fig.text(0.015, 0.905, subtitle, color=m["text2"], fontsize=9.5, va="top")
        draw(ax, m)
        if ax.get_yscale() == "log":                                    # 10 / 100 / 1,000 in full, no minor labels
            ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos=None: f"{v:,.0f}" if v >= 1 else f"{v:g}"))
            ax.yaxis.set_minor_formatter(NullFormatter())
            ax.tick_params(which="minor", length=0)
        elif not isinstance(ax.yaxis.get_major_locator(), FixedLocator):   # not a category axis
            ax.yaxis.set_major_formatter(ticks(ax.get_ylim()[1]))
        handles, _ = ax.get_legend_handles_labels()
        if legend and len(handles) > 1:
            lg = ax.legend(frameon=False, loc="upper left", fontsize=9, ncol=min(4, len(handles)),
                           bbox_to_anchor=(0, 1.08), handlelength=1.6, columnspacing=1.4)
            for text in lg.get_texts():
                text.set_color(m["text"])
            for key in lg.get_lines():
                key.set_linewidth(min(key.get_linewidth(), 6))   # a bar's key is a short swatch, not the bar
        fig.savefig(out / f"{name}-{mode}.png", facecolor=m["surface"])
        plt.close(fig)
    DRAWN.append(name)
    print(f"  charts/{name}-{{light,dark}}.png")


def line(ax, x, y, color, label, m, end_label: str | None = None, linestyle: str = "-") -> None:
    """A 2 px line with 8 px markers ringed in the surface color, and an optional label at its end."""
    points = [(a, b) for a, b in zip(x, y) if b is not None]
    if not points:
        return
    xs, ys = zip(*points)
    ax.plot(xs, ys, color=color, linewidth=2, label=label, linestyle=linestyle, solid_capstyle="round",
            solid_joinstyle="round", dash_capstyle="round", zorder=3)
    ax.plot(xs, ys, linestyle="none", marker="o", markersize=8, markerfacecolor=color, markeredgecolor=m["surface"],
            markeredgewidth=2, zorder=4)
    if end_label:
        ax.annotate(end_label, (xs[-1], ys[-1]), xytext=(8, 0), textcoords="offset points", va="center",
                    color=m["text2"], fontsize=8.5)


def bars(ax, labels, values, m, *, colors=None, slot=(0, 1), width_pt=11.0, label=None, value_fmt="{:.0f}",
         show_values=True, horizontal=False, texts=None) -> None:
    """Thin bars with a rounded data end and a square end at the baseline (zero; on a log value
    axis, the axis's bottom). Call after the value axis's limits and scale are set: the rounded end
    is a round line cap, pulled back by its radius. slot=(i, n): this series is the i-th of n side
    by side in each group, a 4 px gap (2 px at 1x) between neighbours. texts: the value labels
    themselves, one per bar (else value_fmt)."""
    fig = ax.figure
    if horizontal:
        ax.set_ylim(-0.6, len(labels) - 0.4)
    else:
        ax.set_xlim(-0.6, len(labels) - 0.4)
    fig.canvas.draw()
    radius_px = width_pt / 2 * fig.dpi / 72
    across_px = ax.get_window_extent().height if horizontal else ax.get_window_extent().width
    across_units = len(labels) + 0.2
    index, count = slot
    offset = (index - (count - 1) / 2) * (2 * radius_px + 4) * across_units / across_px
    lo, hi = ax.get_xlim() if horizontal else ax.get_ylim()
    span_px = ax.get_window_extent().width if horizontal else ax.get_window_extent().height
    log = not horizontal and ax.get_yscale() == "log"
    base = lo if log else 0.0
    radius = radius_px * ((math.log10(hi) - math.log10(lo)) if log else (hi - lo)) / span_px   # decades on a log axis
    # Everything on the far side of the baseline is clipped: the round cap there becomes a square end.
    clip = Rectangle((0, -1e12) if horizontal else (-1e12, base), 2e12, 2e12, transform=ax.transData)
    for i, value in enumerate(values):
        if value is None:
            continue
        color = (colors[i] if colors else m["series"][0])
        pos = i + offset
        if log:
            end = value / 10 ** radius if value / 10 ** radius > base else value
        else:
            end = max(0.0, value - radius) if value > radius else value / 2
        coords = ([base, end], [pos, pos]) if horizontal else ([pos, pos], [base, end])
        artist, = ax.plot(*coords, color=color, linewidth=width_pt, solid_capstyle="round", zorder=3,
                          label=label if i == 0 else None)
        artist.set_clip_path(clip)
        if show_values and count <= 2:
            text = texts[i] if texts else value_fmt.format(value)
            if horizontal:
                ax.annotate(text, (value, pos), xytext=(6, 0), textcoords="offset points", va="center",
                            color=m["text"], fontsize=8.5)
            elif count == 1:
                ax.annotate(text, (pos, value), xytext=(0, 5), textcoords="offset points", ha="center",
                            color=m["text"], fontsize=8.5)
            else:
                # Two bars side by side: each value hangs off its bar's outer edge, so the pair never
                # overlaps. Three or more: no values on the bars (the axis and results JSON carry them).
                outer = width_pt / 2 if index == 0 else -width_pt / 2
                ax.annotate(text, (pos, value), xytext=(outer, 5), textcoords="offset points",
                            ha="right" if index == 0 else "left", color=m["text"], fontsize=8)
    if horizontal:
        ax.set_yticks(range(len(labels)))
        ax.set_yticklabels(labels)
        ax.grid(axis="y", visible=False)
        ax.grid(axis="x", color=m["grid"], linewidth=0.8)
    else:
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels)


def end_labels(ax, items, m, min_gap_pt: float = 11.0) -> None:
    """Labels at the ends of lines, [(x, y, text)], moved apart vertically so that no two are closer
    than min_gap_pt (the lines' ends stay where they are). Call after the axis limits are set."""
    fig = ax.figure
    fig.canvas.draw()
    scale = fig.dpi / 72
    placed = sorted(((ax.transData.transform((x, y))[1], x, y, text) for x, y, text in items if y is not None))
    shifted = []
    for py, x, y, text in placed:
        if shifted and py - shifted[-1][0] < min_gap_pt * scale:
            py = shifted[-1][0] + min_gap_pt * scale
        shifted.append((py, x, y, text))
    for py, x, y, text in shifted:
        offset = (py - ax.transData.transform((x, y))[1]) / scale
        ax.annotate(text, (x, y), xytext=(8, offset), textcoords="offset points", va="center",
                    color=m["text2"], fontsize=8.5)


def top(values, headroom: float = 1.18) -> float:
    vals = [v for v in values if v is not None]
    return max(vals) * headroom if vals else 1.0


def shade(ax, start: float, end: float, m, text: str | None = None, y: float = 0.98) -> None:
    ax.axvspan(start, end, color=m["shade"], linewidth=0, zorder=0)
    if text:
        ax.text((start + end) / 2, y, text, transform=ax.get_xaxis_transform(), ha="center", va="top",
                color=m["muted"], fontsize=7.5)


def load(results: Path, name: str):
    path = results / f"{name}.json"
    return json.loads(path.read_text()) if path.is_file() else None


def med(spread_dict) -> float | None:
    return (spread_dict or {}).get("median")


# --- the charts ----------------------------------------------------------------------------------

def density(results: Path, out: Path) -> None:
    d = load(results, "density")
    if not d or not d.get("modes"):
        return
    modes = list(d["modes"])
    shape = f"{d['shape']['cpu']} / {d['shape']['memory']}"

    def draw_ceiling(ax, m):
        values = [med(d["modes"][k]["ceiling"]) for k in modes]
        ax.set_xlim(0, top(values, 1.3))
        bars(ax, [f"{k}\n(limit: {d['modes'][k]['limit']})" for k in modes], values, m, horizontal=True)
        ax.invert_yaxis()
    figure(out, "density-ceiling", "Idle sandboxes before the cluster refused one",
           f"Two m7i.2xlarge nodes; sandboxes of {shape} created in batches of {d['batch']}; median of "
           f"{d['runs_per_mode']} runs", "running sandboxes at the refusal", "", draw_ceiling, left=0.2)

    def draw_memory(ax, m):
        ys_all = []
        for i, k in enumerate(modes[:3]):
            runs = d["modes"][k]["runs"]
            if not runs:
                continue
            run = sorted(runs, key=lambda r: r["ceiling"])[len(runs) // 2]   # the median run
            xs = [b["running"] for b in run["batches"]]
            ys = [sum((v.get("mem_mib") or 0) for v in b["node_totals"].values()) / 1024 or None for b in run["batches"]]
            ys_all += ys
            line(ax, xs, ys, m["series"][i], k, m, end_label=f"{k}  {ys[-1]:.1f} GiB" if ys and ys[-1] else k)
        ax.set_ylim(0, top(ys_all, 1.15))
    figure(out, "density-memory", "Memory the two nodes use as idle sandboxes are added",
           "Working set of both nodes after each batch (the run with the median ceiling of each mode)",
           "running sandboxes", "GiB, both nodes", draw_memory, right=0.8)

    def growth(k: str) -> float | None:
        return med(d["modes"][k].get("node_growth_mib"))   # 02_density.py --resummarize adds it to older results

    def draw_per_sandbox(ax, m):
        pods = [med(d["modes"][k]["sandbox_mem_mib"]) for k in modes]
        nodes = [growth(k) for k in modes]
        ax.set_ylim(0, top(pods + nodes, 1.22))
        bars(ax, modes, pods, m, colors=[m["series"][0]] * len(modes), slot=(0, 2), value_fmt="{:.0f} MiB",
             label="the pod's working set (what the kubelet counts)")
        bars(ax, modes, nodes, m, colors=[m["series"][1]] * len(modes), slot=(1, 2), value_fmt="{:.0f} MiB",
             label="the nodes' working-set growth per sandbox (the cost)")
    kata = d["modes"].get("kata") or {}
    node_side = kata.get("node_side")
    if node_side:
        reserved = mem_mib_of(d["shape"]["memory"]) + mem_mib_of((kata.get("runtimeclass_overhead") or {}).get("memory"))

        def draw_kata(ax, m):
            labels = ["the kubelet counts\n(pod working set)", "the nodes' working set\ngrows, per sandbox",
                      "summed RSS on the node\n(QEMU, virtiofsd, shim)", "the scheduler reserves\n(request + overhead)"]
            values = [med(node_side["kubelet_mem_mib"]), growth("kata"), med(node_side["vmm_total_mib"]), reserved]
            ax.set_ylim(0, top(values))
            bars(ax, labels, values, m, colors=[m["series"][0]] * 3 + [m["muted"]], value_fmt="{:.0f} MiB")
        figure(out, "density-kata-memory", "Memory of one idle Kata sandbox, four ways",
               f"Median of {d['runs_per_mode']} runs. The growth is the cost; the RSS sum is an upper bound (a shared "
               "page counts in each process)", "", "MiB", draw_kata,
               legend=False)

    figure(out, "density-per-sandbox", "Memory of one idle sandbox, two ways",
           f"Median of {d['runs_per_mode']} runs; the pod's working set at the ceiling, the growth per sandbox added",
           "", "MiB", draw_per_sandbox)


def latency(results: Path, out: Path) -> None:
    d = load(results, "latency")
    if not d:
        return
    levels = d["levels"]
    kinds = [(k, name) for k, name in (("cold", "cold"), ("warm", "from a Pool")) if d.get(k)]
    if kinds:
        def draw(ax, m):
            # Cold and warm on one log axis (about 100 ms to 26 s): both stay readable, and the end
            # labels are moved apart where two lines end close together.
            values, labels = [], []
            for i, (kind, name) in enumerate(kinds):
                for p, style in (("p50", "-"), ("p95", (0, (4, 2)))):      # p99 is the maximum below 100 samples
                    ys = [d[kind].get(str(n), {}).get("describe", {}).get(p) for n in levels]
                    line(ax, levels, ys, m["series"][i], f"{name} {p}", m, linestyle=style)
                    values += [v for v in ys if v]
                    labels.append((levels[-1], ys[-1], f"{name} {p} {ys[-1]:,.0f} ms" if ys[-1] else ""))
            ax.set_xticks(levels)
            ax.set_yscale("log")
            ax.set_ylim(min(values) / 1.6, max(values) * 1.8)
            end_labels(ax, labels, m)
        figure(out, "latency-create", "Create latency vs creates in flight, cold and from a Pool",
               f"N creates at once from a client pod in the cluster ({d['shape']['cpu']} / {d['shape']['memory']}); SDK "
               f"call to a healthy sandbox; at least {d['min_samples']} samples per level", "creates in flight",
               "ms (log scale)", draw, right=0.8)
    delivery = d.get("delivery")
    if delivery:
        n = delivery["n"]

        def draw_delivery(ax, m):
            labels = [f"controller path\n1 BatchSandbox x{n}", f"API path\n{n} server creates", "published\n100, setup not stated"]
            values = [med(delivery["controller_allocated_ms"]), med(delivery["api_wall_ms"]), 920.0]
            ax.set_ylim(0, top(values))
            bars(ax, labels, values, m, colors=[m["series"][0], m["series"][0], m["muted"]], value_fmt="{:,.0f} ms")
        figure(out, "latency-delivery", f"Handing out {n} warm sandboxes at once",
               f"Pool of {n} Ready pods (100m / 128Mi); time until all are allocated (controller) or answered (API); "
               "median of the runs", "", "ms", draw_delivery, legend=False)


def pause_storm(results: Path, out: Path) -> None:
    d = load(results, "pause_storm")
    if not d or not d.get("runs"):
        return

    def draw(ax, m):
        for i, run in enumerate(d["runs"][:3]):
            timeline = run.get("timeline") or []
            if not timeline:
                continue
            ax.step([t for t, _, _ in timeline], [n for _, n, _ in timeline], where="post", color=m["series"][i],
                    linewidth=2, label=f"storm {run['run']}")
        ax.set_ylim(0, d["size"] * 1.1)
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    figure(out, "pause-durable", f"Snapshots durable in ECR after {d['size']} sandboxes pause at once",
           f"{d['mb']} MiB of random data each; a snapshot counts once its commit Job pushed the image",
           "seconds after the pause calls", "durable snapshots", draw)

    def draw_resume(ax, m):
        values = [(r.get("resume_command_ms") or {}).get("p50") for r in d["runs"]]
        ax.set_ylim(0, top(values))
        bars(ax, [f"storm {r['run']}" for r in d["runs"]], values, m, value_fmt="{:,.0f} ms")
    figure(out, "pause-resume", f"Resume of {d['size']} sandboxes at once, to the first command",
           "p50 per storm; the first command checks the data's sha256", "", "ms", draw_resume, legend=False)


def ladder(results: Path, out: Path) -> None:
    d = load(results, "ladder")
    if not d or not d.get("summary"):
        return
    s = d["summary"]
    rungs = [r for r in ("none", "dns", "dns+nft", "mitm", "vault") if r in s]
    if rungs:
        def draw_latency(ax, m):
            p50 = [s[r]["sequential"]["p50"] for r in rungs]
            p99 = [s[r]["sequential"]["p99"] for r in rungs]
            ax.set_ylim(0, top(p99))
            bars(ax, rungs, p50, m, colors=[m["series"][0]] * len(rungs), slot=(0, 2), label="p50", value_fmt="{:.1f}")
            bars(ax, rungs, p99, m, colors=[m["series"][1]] * len(rungs), slot=(1, 2), label="p99", value_fmt="{:.1f}")
        figure(out, "ladder-latency", "Request latency through each egress layer",
               "One request at a time from a sandbox to the in-cluster echo server, new connection each; "
               f"{d['sequential']} requests x {len(s[rungs[0]]['sequential_p50_ms']['runs'])} runs", "", "ms", draw_latency)

        cached = (load(results, "ladder-resolve-once") or {}).get("summary") or {}

        def draw_rps(ax, m):
            # Two ways of reaching the echo server: a lookup per request (the ladder), and one lookup
            # then the cached address (05 --resolve-once). The values run from about 14 to about
            # 6,000 req/s, so the axis is logarithmic and every bar carries its value.
            per_request = [med(s[r]["load_rps"]) for r in rungs]
            once = [med((cached.get(r + "/cached") or {}).get("load_rps")) for r in rungs]
            def marks(entry: dict) -> str:
                """* the echo server was the cap in a run, p the proxy (05's proxy_bound_runs)."""
                return ("*" if entry.get("target_bound_runs") else "") + (" p" if entry.get("proxy_bound_runs") else "")
            star = [marks(s[r]) for r in rungs]
            star_once = [marks(cached.get(r + "/cached") or {}) for r in rungs]
            known = [v for v in per_request + once if v]
            ax.set_yscale("log")
            ax.set_ylim(min(known) / 2.5, max(known) * 3)

            def fmt(values, stars):
                return [f"{v:,.0f}{mark}" if v is not None else "" for v, mark in zip(values, stars)]
            bars(ax, rungs, per_request, m, colors=[m["series"][0]] * len(rungs), slot=(0, 2),
                 label="a lookup per request", texts=fmt(per_request, star))
            bars(ax, rungs, once, m, colors=[m["series"][1]] * len(rungs), slot=(1, 2),
                 label="one lookup, then the cached address", texts=fmt(once, star_once))
        figure(out, "ladder-throughput", "Requests per second through each egress layer",
               f"{d['concurrency']} clients, new connection per request, median of "
               f"{len(s[rungs[0]]['load_rps']['runs'])} runs; cap in at least one run: * the echo server, p the proxy "
               "(one core)", "",
               "requests per second (log scale)", draw_rps)

        looked = defaultdict(list)
        for record in d.get("records", []):
            if record.get("rung") in rungs and (record.get("lookup") or {}).get("p50") is not None:
                looked[record["rung"]].append(record["lookup"])
        lookup_rungs = [r for r in rungs if looked.get(r)]
        if lookup_rungs:
            def draw_lookup(ax, m):
                # p50 as the bar, p95 as a lighter whisker above it; p50's value to the left of the
                # bar's end, p95's to the right of the whisker's cap, so the two never collide.
                p50 = [statistics.median(x["p50"] for x in looked[r]) for r in lookup_rungs]
                p95 = [statistics.median(x["p95"] for x in looked[r]) for r in lookup_rungs]
                ax.set_ylim(0, top(p95, 1.15))
                bars(ax, lookup_rungs, p50, m, label="p50", show_values=False)
                for i, (low, high) in enumerate(zip(p50, p95)):
                    ax.plot([i, i], [low, high], color=m["muted"], linewidth=1.5, zorder=4,
                            label="p95" if i == 0 else None)
                    ax.plot([i], [high], marker="_", markersize=12, markeredgewidth=1.5, color=m["muted"], zorder=4)
                    ax.annotate(f"{low:.1f}", (i, low), xytext=(-9, 1), textcoords="offset points", ha="right",
                                va="bottom", color=m["text"], fontsize=8.5)
                    ax.annotate(f"p95 {high:.1f}", (i, high), xytext=(9, 1), textcoords="offset points", ha="left",
                                va="bottom", color=m["text2"], fontsize=8)
            runs = max(len(v) for v in looked.values())
            figure(out, "ladder-lookup", "How long the sandbox waits for a name lookup",
                   f"getaddrinfo from the sandbox, {d['sequential']} one at a time, median of {runs} runs", "", "ms",
                   draw_lookup)
        with_sidecar = [r for r in rungs if r != "none"]

        def draw_rss(ax, m):
            values = [med(s[r]["sidecar_peak_rss_mib"]) for r in with_sidecar]
            ax.set_ylim(0, top(values))
            bars(ax, with_sidecar, values, m, value_fmt="{:.0f} MiB")
        if with_sidecar:
            figure(out, "ladder-sidecar-rss", "What the egress sidecar holds in memory under load",
                   "Peak RSS of the sidecar container during the load phase; median of the runs", "", "MiB", draw_rss,
                   legend=False)
    sts = [r for r in ("aws-floor", "sigv4") if r in s]
    if len(sts) == 2:
        def draw_sts(ax, m):
            values = [s[r]["sequential"]["p50"] for r in sts]
            ax.set_ylim(0, top(values))
            bars(ax, ["unsigned call, no sidecar\n(AWS answers 403)", "SigV4 addon re-signs\n(AWS answers 200)"], values,
                 m, value_fmt="{:.0f} ms")
        figure(out, "ladder-sigv4", "STS GetCallerIdentity from a sandbox, p50",
               "Includes AWS's own latency; boto3 with keep-alive, one call at a time", "", "ms", draw_sts, legend=False)


def control_plane(results: Path, out: Path) -> None:
    d = load(results, "control_plane")
    csv_path = results / "control_plane.csv"
    if not d:
        return
    if csv_path.is_file():
        series: dict[str, dict[float, float]] = defaultdict(lambda: defaultdict(float))
        for row in read_csv(csv_path):
            for name, prefix in (("server", "opensandbox-server-"), ("controller", "opensandbox-controller-manager-")):
                if row["pod"].startswith(prefix) and row["cpu_m"] is not None:
                    series[name][row["t"]] += row["cpu_m"]

        def draw_cpu(ax, m):
            # Every API level and create wave is shaded; only run 1's API levels carry a label (x1 ...
            # x64), and the create waves one label for the group: per-band labels would run together.
            ramp, waves = [], []
            for phase in d.get("phases", []):
                if not phase.get("end"):
                    continue
                if phase["name"].startswith("api x"):
                    ramp.append(phase)
                    shade(ax, phase["start"], phase["end"], m,
                          phase["name"].split(" ")[1] if phase["name"].endswith(" run 1") else None)
                elif phase["name"].startswith("create x"):
                    waves.append(phase)
                    shade(ax, phase["start"], phase["end"], m)
            if ramp:
                runs = len({p["name"].split(" run ")[-1] for p in ramp})
                ax.text((ramp[0]["start"] + ramp[-1]["end"]) / 2, 0.92, f"API ramp, clients at once ({runs} runs)",
                        transform=ax.get_xaxis_transform(), ha="center", va="top", color=m["muted"], fontsize=7.5)
            if waves:
                sizes = sorted({int(p["name"].split(" ")[1][1:]) for p in waves})
                runs = len({p["name"].split(" run ")[-1] for p in waves})
                ax.text((waves[0]["start"] + waves[-1]["end"]) / 2, 0.98,
                        f"create waves x{', x'.join(str(n) for n in sizes)} ({runs} runs)",
                        transform=ax.get_xaxis_transform(), ha="center", va="top", color=m["muted"], fontsize=7.5)
            for i, name in enumerate(("server", "controller")):
                points = sorted(series[name].items())
                if points:
                    ax.plot([t for t, _ in points], [v for _, v in points], color=m["series"][i], linewidth=2,
                            label=name, solid_joinstyle="round")
            ax.set_ylim(0, top([v for name in series for v in series[name].values()], 1.25))
        figure(out, "control-plane-cpu", "Lifecycle server and controller CPU under load",
               "From the kubelet every 2 s; shaded: the API ramp levels and the create waves (server limit 1,000m)",
               "seconds", "CPU (millicores)", draw_cpu)
    by_level = d.get("ramp_by_level") or {}
    if by_level:
        levels = sorted(int(k) for k in by_level)

        def draw_rps(ax, m):
            ys = [med(by_level[str(n)]["rps"]) for n in levels]
            line(ax, levels, ys, m["series"][0], "req/s", m, end_label=f"{ys[-1]:,.0f} req/s" if ys[-1] else None)
            ax.set_xscale("log", base=2)
            ax.set_xticks(levels)
            ax.set_xticklabels([str(n) for n in levels])
            ax.set_ylim(0, top(ys))
        figure(out, "control-plane-rps", "API requests per second the one server replica answers",
               "List and status-poll calls from a Job next to the server, keep-alive clients; median of the runs",
               "clients at once", "requests per second", draw_rps, right=0.86)

        def draw_p99(ax, m):
            ys = [med(by_level[str(n)]["p99_ms"]) for n in levels]
            line(ax, levels, ys, m["series"][0], "p99", m, end_label=f"{ys[-1]:,.0f} ms" if ys[-1] else None)
            ax.set_xscale("log", base=2)
            ax.set_xticks(levels)
            ax.set_xticklabels([str(n) for n in levels])
            ax.set_ylim(0, top(ys))
        figure(out, "control-plane-p99", "API p99 latency as clients are added",
               "Each client alternates a list and a status poll on one keep-alive connection; median of the runs' p99",
               "clients at once", "ms", draw_p99,
               right=0.86)


def noisy(results: Path, out: Path) -> None:
    d = load(results, "noisy")
    if not d or not d.get("summary"):
        return
    runtimes = list(d["summary"])
    scenarios = ["baseline", "cpu", "disk"]
    for metric, title, unit, p in (("cpu_ms", "A fixed CPU job in the victims", "ms, p50", "p50"),
                                   ("io_ms", "64 MiB write + fsync in the victims", "ms, p50", "p50"),
                                   ("fsync_ms", "Small fsync latency in the victims", "ms, p99", "p99")):
        def draw(ax, m, metric=metric, p=p):
            all_values = [d["summary"][rt].get(sc, {}).get(metric, {}).get(p) for rt in runtimes for sc in scenarios]
            ax.set_ylim(0, top(all_values))
            shown = runtimes[:3]
            for i, rt in enumerate(shown):
                values = [d["summary"][rt].get(sc, {}).get(metric, {}).get(p) for sc in scenarios]
                bars(ax, scenarios, values, m, colors=[m["series"][i]] * 3, slot=(i, len(shown)), label=rt,
                     value_fmt="{:,.0f}" if metric != "fsync_ms" else "{:,.1f}")
            ax.set_xticks(range(len(scenarios)))
            ax.set_xticklabels(["no neighbors", f"{d['hogs']} CPU hogs", f"{d['hogs']} disk hogs"])
        figure(out, f"noisy-{metric.split('_')[0]}", title,
               f"{d['victims']} victims ({d['victim_shape']['cpu']} CPU / {d['victim_shape']['memory']}, Guaranteed) next to "
               f"hogs (requests {d['hog_requests']['cpu']}, limit {d['hog_limits']['cpu']} CPU); victims sharing a node "
               "with a hog", "", unit, draw)


def cloudwatch(results: Path, out: Path) -> None:
    d = load(results, "cloudwatch")
    if not d:
        return
    from datetime import datetime

    def minutes(ts: list[str], t0: datetime) -> list[float]:
        return [(datetime.fromisoformat(t.replace("Z", "+00:00")) - t0).total_seconds() / 60 for t in ts]

    t0 = datetime.fromisoformat(d["window"]["start"].replace("Z", "+00:00"))
    nodes = sorted(d.get("nodes", {}))
    if nodes:
        def draw_cpu(ax, m):
            for i, node in enumerate(nodes[:3]):
                s = d["nodes"][node].get("node_cpu_utilization", {"t": [], "v": []})
                if s["v"]:
                    ax.plot(minutes(s["t"], t0), s["v"], color=m["series"][i], linewidth=2, label=node)
            ax.set_ylim(0, 100)
        figure(out, "cloudwatch-node-cpu", "Node CPU through the whole test (Container Insights)",
               "node_cpu_utilization, 1-minute averages", "minutes since Container Insights came on", "% CPU", draw_cpu)
    nat = d.get("nat") or {}
    if nat:
        gateway = max(nat.values(), key=lambda g: len(g.get("BytesOutToDestination", {}).get("v", [])))
        s = gateway.get("BytesOutToDestination", {"t": [], "v": []})
        if s["v"]:
            def draw_nat(ax, m):
                xs = minutes(s["t"], t0)
                ys = [v / 1e6 for v in s["v"]]
                ax.vlines(xs, 0, ys, color=m["series"][0], linewidth=2.5)
                ax.set_ylim(0, top(ys, 1.25))
                # One label per group of storms (they run about a minute apart on a two-hour axis), with
                # the group's total; a label per storm would print on top of each other.
                groups = d.get("pause_storm_nat_groups") or [
                    {"runs": [i["run"]], "window": i["pause_window"], "nat_bytes_out": i.get("nat_bytes_out")}
                    for i in d.get("pause_storm_nat", [])]
                for group in groups:
                    start = (datetime.fromisoformat(group["window"][0].replace("Z", "+00:00")) - t0).total_seconds() / 60
                    runs = group["runs"]
                    name = f"pause storm {runs[0]}" if len(runs) == 1 else f"pause storms {runs[0]}-{runs[-1]}"
                    sent = group.get("nat_bytes_out")
                    ax.text(start, 0.98, name + (f": {sent / 1e9:.2f} GB" if sent else ""),
                            transform=ax.get_xaxis_transform(), color=m["text2"], fontsize=8, va="top", ha="right")
            figure(out, "cloudwatch-nat", "Bytes the NAT gateway sent out, per minute",
                   "BytesOutToDestination; the spikes are the snapshot pushes to ECR", "minutes since Container Insights "
                   "came on", "MB per minute", draw_nat, legend=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draw light and dark chart pairs from results/.")
    parser.add_argument("--results", type=Path, default=HERE / "results")
    parser.add_argument("--charts", type=Path, default=HERE / "charts")
    args = parser.parse_args(argv)
    args.charts.mkdir(parents=True, exist_ok=True)
    DRAWN.clear()
    for chart in (density, latency, pause_storm, ladder, control_plane, noisy, cloudwatch):
        try:
            chart(args.results, args.charts)
        except (KeyError, TypeError, ValueError, IndexError) as error:
            print(f"  {chart.__name__}: skipped ({type(error).__name__}: {error})", file=sys.stderr)
    print(f"{len(DRAWN)} chart pair(s) in {args.charts.name}/" if DRAWN else "no results to draw yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
