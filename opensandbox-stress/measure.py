"""Measurement helpers with no cluster and no SDK in them, so the unit tests run anywhere.

common.py re-exports all of this (and wires the Sampler to kubectl):

  pct(values, p)                 nearest-rank percentile (p50, p95, p99 of a list)
  describe(label, values_ms)     {label, n, min, p50, p95, p99, max, mean}, rounded to 0.1
  spread(per_run)                {median, min, max, spread, spread_pct, runs}: how RUNS repeats agree
  parallel(fn, items, workers)   run fn over items on a thread pool; one Result per item, in order
  save(name, data)               results/<name>.json, refused if it contains an account ID, an IP
                                 or a home directory path
  CsvSink                        the CSV writer the Sampler uses
  summary_rows / container_rows  rows out of one kubelet stats/summary document
  Sampler                        samples each node's stats/summary every N seconds into
                                 results/<name>.csv with the header t,node,pod,cpu_m,mem_mib
  NodeAliases                    node-a, node-b, ... for the node names (an EKS node name holds
                                 the node's private IP)
  mask(text)                     the display filter: <registry>, <account>, <ip>, ~
  node_growth(batches)           the nodes' working-set growth per sandbox added (02_density.py)
  storm_nat(storms, series, p)   NAT bytes per pause storm, each one-minute point counted once (08)

Units: cpu_m is millicores (usageNanoCores / 1e6), mem_mib is the working set in MiB
(workingSetBytes / 2**20), the number the kubelet uses for eviction and `kubectl top` shows.
A value the kubelet did not report (a pod that just started has no CPU rate yet) is left empty
in the CSV, never written as 0.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
CSV_HEADER = ["t", "node", "pod", "cpu_m", "mem_mib"]
# cpu_s is the container's cumulative CPU time (usageCoreNanoSeconds / 1e9) and cpu_at the kubelet's
# timestamp for it (Unix seconds): a CPU average over a window comes from their deltas (window_cpu),
# not from the kubelet's own smoothed rate (cpu_m).
CONTAINER_HEADER = ["t", "node", "pod", "container", "cpu_m", "mem_mib", "rss_mib", "cpu_s", "cpu_at"]
NODE_ROW = "_node"


# --- statistics ------------------------------------------------------------------------------

def pct(values: Iterable[float], p: float) -> float:
    """The nearest-rank p-th percentile: the smallest value with at least p% of the values at or
    below it. pct(v, 50) of an even-sized list is the lower middle value (no interpolation, so
    every reported number is one that was measured). nan for no values."""
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return float("nan")
    if not 0 <= p <= 100:
        raise ValueError(f"p must be within 0..100, not {p}")
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def _r(value: float | None) -> float | None:
    return None if value is None or (isinstance(value, float) and math.isnan(value)) else round(value, 1)


def describe(label: str, values_ms: Iterable[float]) -> dict:
    """{label, n, min, p50, p95, p99, max, mean, p99_is_max} of a list of milliseconds. Below 100
    values the nearest-rank p99 is the maximum, and p99_is_max says so: report p95 then. Empty: n 0,
    the rest None. Failed calls are never values here: callers count them separately."""
    values = [v for v in values_ms if v is not None]
    if not values:
        return {"label": label, "n": 0, "min": None, "p50": None, "p95": None, "p99": None, "max": None, "mean": None,
                "p99_is_max": None}
    return {"label": label, "n": len(values), "min": _r(min(values)), "p50": _r(pct(values, 50)),
            "p95": _r(pct(values, 95)), "p99": _r(pct(values, 99)), "max": _r(max(values)),
            "mean": _r(statistics.fmean(values)), "p99_is_max": len(values) < 100}


def spread(per_run: Iterable[float | None]) -> dict:
    """How the RUNS repeats of one measurement agree: {median, min, max, spread, spread_pct, runs}.
    spread is max - min; spread_pct is that as a share of the median. Missing runs (None) are
    left out of the numbers but kept in `runs`."""
    runs = list(per_run)
    values = [v for v in runs if v is not None]
    if not values:
        return {"median": None, "min": None, "max": None, "spread": None, "spread_pct": None, "runs": runs}
    median = statistics.median(values)
    width = max(values) - min(values)
    return {"median": _r(median), "min": _r(min(values)), "max": _r(max(values)), "spread": _r(width),
            "spread_pct": _r(100 * width / median) if median else None, "runs": [_r(v) for v in runs]}


# --- running things side by side ---------------------------------------------------------------

@dataclass
class Result:
    """One call of parallel(): the item, what fn returned or the error, and when it ran
    (seconds since parallel() was called)."""
    item: Any
    value: Any = None
    error: str | None = None
    error_type: str | None = None
    start: float = 0.0
    end: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def seconds(self) -> float:
        return self.end - self.start

    @property
    def ms(self) -> float:
        return 1000 * (self.end - self.start)


def parallel(fn: Callable[[Any], Any], items: Iterable[Any], workers: int, together: bool = False) -> list[Result]:
    """Run fn(item) for every item on `workers` threads and return one Result per item, in the
    order of `items`. An exception is caught and recorded (error, error_type), never raised.

    together=True holds the first `workers` calls at a barrier and releases them at once, so N
    calls are really in flight at the same moment (the latency-under-concurrency tests)."""
    items = list(items)
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if not items:
        return []
    origin = time.monotonic()
    first_wave = min(workers, len(items))
    barrier = threading.Barrier(first_wave) if together and first_wave > 1 else None
    results = [Result(item=item) for item in items]

    def call(index: int) -> None:
        result = results[index]
        if barrier is not None and index < first_wave:
            try:
                barrier.wait(timeout=60)
            except threading.BrokenBarrierError:
                pass
        result.start = time.monotonic() - origin
        try:
            result.value = fn(result.item)
        except BaseException as error:  # noqa: BLE001 - recorded, the caller decides
            result.error = str(error)[:500] or type(error).__name__
            result.error_type = type(error).__name__
        result.end = time.monotonic() - origin

    with ThreadPoolExecutor(max_workers=first_wave) as pool:
        list(pool.map(call, range(len(items))))
    return results


# --- what is safe to write or print ---------------------------------------------------------------

_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
# A sandbox ID is a UUID; its last group can be 12 digits. UUIDs are set aside before the account check.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")
_HOME = re.compile(r"/Users/[^/\s\"']+|/home/[^/\s\"']+")
_ARN = re.compile(r"arn:aws[\w-]*:[\w-]*:[\w-]*:\d{12}:")
_ANY_ARN = re.compile(r"arn:aws[a-zA-Z-]*:[^\s\"',)]+")
_EKS_ENDPOINT = re.compile(r"[0-9A-Fa-f]{32}\.[a-z0-9]+\.[a-z0-9-]+\.eks\.amazonaws\.com")
_ELB_HOST = re.compile(r"[a-z0-9-]+\.elb\.[a-z0-9-]+\.amazonaws\.com")
_NODE_NAME = re.compile(r"ip-\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}")


def mask(text: str, registry: str | None = None) -> str:
    """The display filter: the ECR registry host -> <registry>, an ARN -> <arn>, the EKS API endpoint
    -> <eks-endpoint>, an ELB/NLB host name -> <nlb>, any other 12-digit number (an AWS account ID)
    -> <account>, an EKS node name -> <node>, any IPv4 address -> <ip>, a home directory -> ~."""
    if registry:
        text = text.replace(registry + "/", "<registry>/").replace(registry, "<registry>")
    text = _ANY_ARN.sub("<arn>", text)
    text = _EKS_ENDPOINT.sub("<eks-endpoint>", text)
    text = _ELB_HOST.sub("<nlb>", text)
    uuids = _UUID.findall(text)
    text = _ACCOUNT.sub("<account>", _UUID.sub("\0uuid\0", text))
    for value in uuids:
        text = text.replace("\0uuid\0", value, 1)
    text = _NODE_NAME.sub("<node>", text)
    text = _IPV4.sub("<ip>", text)
    return _HOME.sub("~", text)


def leaks(text: str) -> list[str]:
    """What in this text must not be written to results/ or printed: account IDs (and ARNs that
    carry one), IPv4 addresses, EKS node names (ip-10-0-0-1: the node's IP), home directory paths.
    Returns the kinds found (empty when clean)."""
    found = []
    if _ARN.search(text):
        found.append("an ARN with an account ID")
    if _EKS_ENDPOINT.search(text):
        found.append("the EKS API endpoint")
    if _ELB_HOST.search(text):
        found.append("a load balancer host name")
    if _ACCOUNT.search(_UUID.sub("<uuid>", text)):
        found.append("a 12-digit number (account ID)")
    if _IPV4.search(text):
        found.append("an IPv4 address")
    if _NODE_NAME.search(text):
        found.append("a node name with an IP")
    if _HOME.search(text):
        found.append("a home directory path")
    return found


def _strings(data: Any) -> Iterable[str]:
    """Every string in a JSON-like structure, keys included. Numbers are not strings: a byte count
    with 12 digits is not an account ID."""
    if isinstance(data, str):
        yield data
    elif isinstance(data, dict):
        for key, value in data.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(data, (list, tuple, set)):
        for value in data:
            yield from _strings(value)


def masked(data: Any, registry: str | None = None) -> Any:
    """A copy of a JSON-like structure with mask() applied to every string (keys included); numbers
    are left as they are."""
    if isinstance(data, str):
        return mask(data, registry)
    if isinstance(data, dict):
        return {mask(str(k), registry): masked(v, registry) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [masked(v, registry) for v in data]
    return data


def save(name: str, data: Any, results: Path | None = None, quarantine: Path | None = None,
         registry: str | None = None) -> Path:
    """Write results/<name>.json, which is meant to be published. Every string goes through mask()
    first (an error message naming 127.0.0.1:18080 or an ARN is kept, masked), so a long run's
    numbers are never refused at the end. When masking changed something, the unmasked file goes to
    logs/<name>.raw.json (gitignored) for debugging. If a string still leaks after masking (it should
    not), the file goes to logs/<name>.unsafe.json instead and ValueError says what was found."""
    folder = results or RESULTS
    hold = quarantine or (folder.parent / "logs")
    raw = json.loads(json.dumps(data, default=str))
    clean = masked(raw, registry)
    text = json.dumps(clean, indent=1) + "\n"
    if clean != raw:
        hold.mkdir(parents=True, exist_ok=True)
        (hold / f"{name}.raw.json").write_text(json.dumps(raw, indent=1) + "\n")
    problems = sorted({kind for s in _strings(clean) for kind in leaks(s)})
    if problems:
        hold.mkdir(parents=True, exist_ok=True)
        (hold / f"{name}.unsafe.json").write_text(text)
        raise ValueError(f"results/{name}.json would contain {', '.join(problems)}: written to "
                         f"logs/{name}.unsafe.json instead")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    partial = folder / f".{name}.json.partial"
    partial.write_text(text)
    os.replace(partial, path)          # all or nothing: a crash mid-write keeps the previous file
    return path


def _png_text(path: Path) -> str:
    """The text chunks (tEXt, iTXt, zTXt) of a PNG: its only text; the chart's own text is pixels."""
    import struct
    import zlib

    data = path.read_bytes()
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ""
    out, pos = [], 8
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"tEXt":
            out.append(body.replace(b"\0", b" ").decode("latin-1"))
        elif kind == b"zTXt":
            key, _, rest = body.partition(b"\0")
            out.append(key.decode("latin-1") + " " + zlib.decompress(rest[1:]).decode("latin-1", "replace"))
        elif kind == b"iTXt":
            out.append(body.replace(b"\0", b" ").decode("utf-8", "replace"))
        pos += 12 + length
    return "\n".join(out)


def scan_published(*folders: Path) -> list[tuple[str, str]]:
    """(file, what) for everything in these folders (results/, charts/) that must not be published:
    account IDs, ARNs with one, IPv4 addresses, node names with an IP, home directory paths. JSON is
    checked string by string and CSV cell by cell, skipping numbers (a 12-digit byte count is not an
    account ID); PNGs by their text chunks; any other file as text."""
    found: list[tuple[str, str]] = []
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(p for p in folder.rglob("*") if p.is_file()):
            name = f"{folder.name}/{path.relative_to(folder)}"
            if path.suffix == ".json":
                try:
                    texts = list(_strings(json.loads(path.read_text())))
                except ValueError:
                    texts = [path.read_text(errors="replace")]
            elif path.suffix == ".csv":
                with path.open(newline="") as handle:
                    texts = [cell for row in csv.reader(handle) for cell in row
                             if not re.fullmatch(r"-?\d+(\.\d+)?([eE][-+]?\d+)?", cell)]
            elif path.suffix == ".png":
                texts = [_png_text(path)]
            else:
                texts = [path.read_text(errors="replace")]
            texts.append(name)
            for kind in sorted({k for t in texts for k in leaks(t)}):
                found.append((name, kind))
    return found


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the node names ------------------------------------------------------------------------------

def mem_mib_of(quantity: str | None) -> float:
    """A Kubernetes memory quantity ("128Mi", "1Gi", "160Mi") in MiB; 0 for None."""
    if not quantity:
        return 0.0
    units = {"Ki": 1 / 1024, "Mi": 1.0, "Gi": 1024.0, "Ti": 1024.0 ** 2, "k": 1e3 / 2**20, "M": 1e6 / 2**20,
             "G": 1e9 / 2**20}
    for suffix in sorted(units, key=len, reverse=True):
        if quantity.endswith(suffix):
            return float(quantity[: -len(suffix)]) * units[suffix]
    return float(quantity) / 2**20


# --- manifests ------------------------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def render_template(text: str, values: dict[str, str], name: str = "manifest") -> str:
    """Fill ${KEY} placeholders (upper case only: a shell script's ${p#/proc/} inside a manifest is
    left alone). Raises ValueError naming any placeholder left unfilled outside comment lines."""
    text = _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    left = sorted(set(_PLACEHOLDER.findall(body)))
    if left:
        raise ValueError(f"{name}: {', '.join('${' + k + '}' for k in left)} left unfilled")
    return text


# --- the node-side VM sampler (manifests/vmm-sampler.yaml) ---------------------------------------------

VMM_KINDS = ("qemu", "clh", "virtiofsd", "shim")


def parse_vmm(text: str, node: str) -> list[dict]:
    """The sampler's lines "vmm <unix time> <kind> <pid> <rss KiB> <sandbox id> <pod>" as dicts
    {t, node, kind, pid, rss_mib, sandbox, pod}. "-" (not found) becomes None; anything else in the
    log (the start line, a half-written last line) is skipped."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 7 or parts[0] != "vmm" or parts[2] not in VMM_KINDS:
            continue
        try:
            t, pid, rss = int(parts[1]), int(parts[3]), int(parts[4])
        except ValueError:
            continue
        out.append({"t": t, "node": node, "kind": parts[2], "pid": pid, "rss_mib": round(rss / 1024, 1),
                    "sandbox": None if parts[5] == "-" else parts[5], "pod": None if parts[6] == "-" else parts[6]})
    return out


def vmm_by_pod(samples: list[dict], at: float | None = None) -> dict[str, dict]:
    """{pod: {qemu, virtiofsd, shim, clh, total}} in MiB, from each VM's latest sample at or before
    `at` (unix time; default the latest). Processes of one kind and one sample time are summed
    (virtiofsd runs as two). A VM whose pod was not found is keyed "sandbox:<first 16 of its id>"."""
    per: dict[tuple[str, str, int], float] = {}
    for s in samples:
        if at is not None and s["t"] > at:
            continue
        key = s["pod"] or (f"sandbox:{s['sandbox'][:16]}" if s["sandbox"] else None)
        if key is None:
            continue
        per[(key, s["kind"], s["t"])] = per.get((key, s["kind"], s["t"]), 0.0) + s["rss_mib"]
    latest: dict[tuple[str, str], tuple[int, float]] = {}
    for (key, kind, t), rss in per.items():
        if (key, kind) not in latest or t > latest[(key, kind)][0]:
            latest[(key, kind)] = (t, rss)
    out: dict[str, dict] = {}
    for (key, kind), (_, rss) in latest.items():
        out.setdefault(key, {})[kind] = round(rss, 1)
    for values in out.values():
        values["total"] = round(sum(values.values()), 1)
    return out


def vmm_gap(vmm: dict[str, dict], kubelet: dict[str, dict]) -> dict:
    """The node's view of each Kata VM against the kubelet's view of its pod (medians, MiB).
    kubelet maps pod name -> {mem_mib, rss_mib} (pod_stats()). vmm_total_mib sums the RSS of the
    VM's processes, an upper bound on their memory: a page that several processes map (the guest's
    RAM, which QEMU and virtiofsd both map, shared libraries) counts once in each. gap_mib is that
    sum minus the pod's working set: an upper bound on what the kubelet, and so `kubectl top` and
    evictions, do not count (node_growth() measures the cost itself).
    Paired per pod when the sampler found the pod names; otherwise median against median."""
    def med(values):
        values = [v for v in values if v is not None]
        return _r(statistics.median(values)) if values else None

    joined = sorted(set(vmm) & set(kubelet))
    out = {"vms": len(vmm), "pods": len(kubelet), "joined": len(joined),
           "vmm_total_mib": med(v.get("total") for v in vmm.values())}
    for kind in VMM_KINDS:
        out[f"{kind}_mib"] = med(v.get(kind) for v in vmm.values())
    pods = joined or list(kubelet)
    out["kubelet_mem_mib"] = med(kubelet[p].get("mem_mib") for p in pods)
    out["kubelet_rss_mib"] = med(kubelet[p].get("rss_mib") for p in pods)
    if joined:
        out["paired_by"] = "pod"
        out["gap_mib"] = med((vmm[p]["total"] - kubelet[p]["mem_mib"]) for p in joined
                             if kubelet[p].get("mem_mib") is not None)
    else:
        out["paired_by"] = "medians"
        out["gap_mib"] = (_r(out["vmm_total_mib"] - out["kubelet_mem_mib"])
                          if out["vmm_total_mib"] is not None and out["kubelet_mem_mib"] is not None else None)
    return out



def node_growth(batches: list[dict]) -> float | None:
    """What one more idle sandbox costs the nodes, in MiB: how much both nodes' working set grew per
    sandbox, from the first batch to the last one before a create failed (a batch with a refused
    create also holds pods being deleted). batches: 02_density.py's, each with `running`, `failed`
    and `node_totals` ({node: {mem_mib}}). None without two such batches or a node's number."""
    clean = []
    for batch in batches:
        if batch.get("failed"):
            break
        clean.append(batch)

    def total(batch: dict) -> float | None:
        values = [v.get("mem_mib") for v in (batch.get("node_totals") or {}).values()]
        return sum(values) if values and None not in values else None

    if len(clean) < 2:
        return None
    first, last = clean[0], clean[-1]
    added = last["running"] - first["running"]
    if added <= 0 or total(first) is None or total(last) is None:
        return None
    return _r((total(last) - total(first)) / added)



def node_growth_span(batches: list[dict]) -> int | None:
    """How many sandboxes node_growth() spans: between the first batch and the last before a create
    failed (with two clean batches of 10, one step of 10). None when node_growth() has no answer."""
    clean = []
    for batch in batches:
        if batch.get("failed"):
            break
        clean.append(batch)
    return clean[-1]["running"] - clean[0]["running"] if len(clean) >= 2 and node_growth(batches) is not None else None

def _when(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def storm_nat(storms: list[dict], series: dict, period: float, busy_share: float = 0.1,
              coverage: float = 0.98) -> tuple[list[dict], list[dict]]:
    """The NAT gateway's bytes out for each pause storm, with every one-minute point counted once.

    storms: [{run, start, end, snapshot_bytes, fresh_bytes}], start and end the pause window (ISO,
    UTC); series: {t: [ISO], v: [bytes]}, one point per `period` seconds, stamped with the start of
    its period. A storm's window is its pause window padded by one period on each side (a push can
    land in the point after the one it started in). Storms whose windows overlap form one group,
    and a group's points are summed once. Returns (storms, groups).

    Each storm's nat_bytes_out (and `attributed`):
      - "window": its group holds only this storm, so the group's total is the storm's;
      - "minute": the group holds several storms and exactly one busy point per storm (a point of
        at least busy_share of the group's largest), in time order, each inside its own storm's
        window, and the busy points hold at least `coverage` of the group's total (so no storm's
        tail is lost in a quiet point): the storm's busy point. The rest of the group's total is
        the group's unattributed_bytes;
      - None: the points cannot be told apart, and only the group's total stands."""
    points = sorted((_when(t), v) for t, v in zip(series.get("t", []), series.get("v", [])) if v is not None)
    pad = timedelta(seconds=period)
    ordered = sorted(storms, key=lambda s: _when(s["start"]))
    spans = [(_when(s["start"]) - pad, _when(s.get("end") or s["start"]) + pad) for s in ordered]
    members: list[list[int]] = []
    for i, (lo, _hi) in enumerate(spans):
        if members and lo <= max(spans[j][1] for j in members[-1]):
            members[-1].append(i)
        else:
            members.append([i])
    out_storms, groups = [], []
    for g, group in enumerate(members):
        lo, hi = spans[group[0]][0], max(spans[i][1] for i in group)
        inside = [(t, v) for t, v in points if lo <= t <= hi]
        total = sum(v for _, v in inside)
        largest = max((v for _, v in inside), default=0)
        busy = [(t, v) for t, v in inside if largest and v >= busy_share * largest]
        busy_total = sum(v for _, v in busy)
        separable = len(group) > 1 and len(busy) == len(group) and all(
            spans[i][0] <= t <= spans[i][1] for i, (t, _v) in zip(group, busy)) and (
            total > 0 and busy_total >= coverage * total)
        groups.append({"runs": [ordered[i].get("run") for i in group],
                       "window": [lo.strftime("%Y-%m-%dT%H:%M:%SZ"), hi.strftime("%Y-%m-%dT%H:%M:%SZ")],
                       "points": len(inside), "busy_points": [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t, _ in busy],
                       "nat_bytes_out": total,
                       "unattributed_bytes": (total - busy_total) if separable else None,
                       "snapshot_bytes": sum(ordered[i].get("snapshot_bytes") or 0 for i in group),
                       "fresh_bytes": sum(ordered[i].get("fresh_bytes") or 0 for i in group),
                       "per_storm_mean": total / len(group)})
        for k, i in enumerate(group):
            storm = ordered[i]
            if len(group) == 1:
                value, how = total, "window"
            elif separable:
                value, how = busy[k][1], "minute"
            else:
                value, how = None, None
            out_storms.append({"run": storm.get("run"), "pause_window": [storm["start"], storm.get("end")],
                               "group": g, "nat_bytes_out": value, "attributed": how,
                               "snapshot_bytes": storm.get("snapshot_bytes"), "fresh_bytes": storm.get("fresh_bytes")})
    return out_storms, groups

class NodeAliases:
    """node-a, node-b, ... in the sorted order of the real names. An EKS node name is
    ip-<private IP>.ec2.internal, so results and output use the alias."""

    def __init__(self, names: Iterable[str]):
        self.names = sorted(set(names))
        self.map = {name: f"node-{chr(ord('a') + i)}" if i < 26 else f"node-{i}" for i, name in enumerate(self.names)}

    def __call__(self, name: str) -> str:
        return self.map.get(name, "node-?")


# --- stats/summary rows --------------------------------------------------------------------------

def _cpu_m(stats: dict | None) -> float | None:
    nano = (stats or {}).get("usageNanoCores")
    return None if nano is None else round(nano / 1e6, 1)


def _mib(stats: dict | None, key: str = "workingSetBytes") -> float | None:
    value = (stats or {}).get(key)
    return None if value is None else round(value / 2**20, 1)


def _pod_value(pod: dict, kind: str, reader: Callable[[dict | None], float | None]) -> float | None:
    """The pod-level value when the kubelet reports one (it includes what runs in the pod's cgroup
    outside the containers, a Kata VM's hypervisor among it), else the sum over the containers, or
    None when a container has no value yet."""
    own = reader(pod.get(kind))
    if own is not None:
        return own
    values = [reader(c.get(kind)) for c in pod.get("containers", [])]
    if not values or any(v is None for v in values):
        return None
    return round(sum(values), 1)


def summary_rows(summary: dict, t: float, node: str, namespaces: Iterable[str] | None = None) -> list[list]:
    """CSV rows (t, node, pod, cpu_m, mem_mib) from one stats/summary document: one `_node` row with
    the node's totals, then one row per pod (optionally only pods in `namespaces`)."""
    wanted = set(namespaces) if namespaces else None
    node_stats = summary.get("node", {})
    rows = [[t, node, NODE_ROW, _cpu_m(node_stats.get("cpu")), _mib(node_stats.get("memory"))]]
    for pod in summary.get("pods", []):
        ref = pod.get("podRef", {})
        if wanted is not None and ref.get("namespace") not in wanted:
            continue
        rows.append([t, node, ref.get("name", "?"), _pod_value(pod, "cpu", _cpu_m), _pod_value(pod, "memory", _mib)])
    return rows


def _cpu_counter(stats: dict | None) -> tuple[float | None, float | None]:
    """(cumulative CPU seconds, the kubelet's Unix timestamp for it), or (None, None)."""
    stats = stats or {}
    nano, when = stats.get("usageCoreNanoSeconds"), stats.get("time")
    if nano is None or not when:
        return None, None
    try:
        at = datetime.fromisoformat(str(when).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None, None
    return round(nano / 1e9, 3), round(at, 3)


def container_rows(summary: dict, t: float, node: str, namespaces: Iterable[str] | None = None) -> list[list]:
    """CSV rows (t, node, pod, container, cpu_m, mem_mib, rss_mib, cpu_s, cpu_at): one per container,
    for the per-sidecar numbers (the egress sidecar's RSS) and CPU averages over a window."""
    wanted = set(namespaces) if namespaces else None
    rows = []
    for pod in summary.get("pods", []):
        ref = pod.get("podRef", {})
        if wanted is not None and ref.get("namespace") not in wanted:
            continue
        for c in pod.get("containers", []):
            cpu_s, cpu_at = _cpu_counter(c.get("cpu"))
            rows.append([t, node, ref.get("name", "?"), c.get("name", "?"), _cpu_m(c.get("cpu")),
                         _mib(c.get("memory")), _mib(c.get("memory"), "rssBytes"), cpu_s, cpu_at])
    return rows


MIN_CPU_SPAN_S = 10.0   # a CPU average needs counter readings at least this far apart


def window_cpu(rows: list[dict], pod: str, container: str, start_unix: float, end_unix: float,
               min_span: float = MIN_CPU_SPAN_S) -> float | None:
    """Average millicores of one container over [start_unix, end_unix], from its cumulative CPU
    counter: (last - first) / (their timestamps' difference).

    Readings are chosen by the counter's own timestamp (cpu_at, the kubelet's, which can trail the
    moment the sampler read it by a housekeeping interval of about 10 s), never by when they were
    read: a reading taken during the load but stamped before it would otherwise pull the average
    toward the idle time around the load. Only readings stamped inside the window count. None
    ("unknown") with fewer than two distinct readings, readings less than min_span seconds apart,
    or a counter that went backwards (a restart). A caller must treat None as unknown, never as
    "not busy"."""
    readings: dict[float, float] = {}
    for r in rows:
        if r["pod"] != pod or r.get("container") != container:
            continue
        at, used = r.get("cpu_at"), r.get("cpu_s")
        if at in (None, "") or used in (None, ""):
            continue
        at, used = float(at), float(used)
        if start_unix <= at <= end_unix:
            readings[at] = used
    if len(readings) < 2:
        return None
    first, last = min(readings), max(readings)
    span, cpu = last - first, readings[last] - readings[first]
    if span < min_span or cpu < 0:
        return None
    return round(1000 * cpu / span, 1)


def bound(cpu_m: float | None, threshold_m: float) -> bool | None:
    """True / False against a threshold, None when the CPU is unknown (never read as "not bound")."""
    return None if cpu_m is None else cpu_m >= threshold_m


def any_bound(values: Iterable[float | None], threshold_m: float) -> bool | None:
    """Several replicas: True when any known one reaches the threshold, None when none does but one
    is unknown (it may have), False only when every one is known and below it."""
    values = list(values)
    if any(v is not None and v >= threshold_m for v in values):
        return True
    if any(v is None for v in values) or not values:
        return None
    return False


def pod_stats(summary: dict, pod_name: str) -> dict | None:
    """{cpu_m, mem_mib, rss_mib, mem_source, containers: {name: {cpu_m, mem_mib, rss_mib}}} of one pod,
    or None. mem_source says whether the kubelet gave a pod-level memory number ("pod": the pod
    cgroup) or the containers' sum stands in for it ("containers")."""
    for pod in summary.get("pods", []):
        if pod.get("podRef", {}).get("name") == pod_name:
            return {"cpu_m": _pod_value(pod, "cpu", _cpu_m), "mem_mib": _pod_value(pod, "memory", _mib),
                    "mem_source": "pod" if _mib(pod.get("memory")) is not None else "containers",
                    "rss_mib": _pod_value(pod, "memory", lambda s: _mib(s, "rssBytes")),
                    "containers": {c.get("name", "?"): {"cpu_m": _cpu_m(c.get("cpu")), "mem_mib": _mib(c.get("memory")),
                                                        "rss_mib": _mib(c.get("memory"), "rssBytes")}
                                   for c in pod.get("containers", [])}}
    return None


class CsvSink:
    """A CSV file with a fixed header. None is written as an empty field; every write is flushed,
    so a killed run keeps what it measured."""

    def __init__(self, path: Path, header: list[str]):
        self.path = path
        self.header = header
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("w", newline="")
        self._writer = csv.writer(self._file)
        self._writer.writerow(header)
        self.rows = 0

    def write(self, rows: Iterable[list]) -> None:
        for row in rows:
            if len(row) != len(self.header):
                raise ValueError(f"{self.path.name}: a row has {len(row)} fields, the header {len(self.header)}")
            self._writer.writerow(["" if v is None else v for v in row])
            self.rows += 1
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()


def read_csv(path: Path) -> list[dict]:
    """A CSV as dicts with cpu_m, mem_mib, rss_mib and t as floats (None when empty)."""
    out = []
    with path.open(newline="") as handle:
        for row in csv.DictReader(handle):
            if None in row.values() or None in row:
                continue        # a row the sampler is still writing (the file is read while it grows)
            for key in ("t", "cpu_m", "mem_mib", "rss_mib", "cpu_s", "cpu_at"):
                if key in row:
                    row[key] = float(row[key]) if row[key] not in ("", None) else None
            out.append(row)
    return out


def window_stats(rows: list[dict], pod_prefix: str, start: float, end: float) -> dict:
    """CPU and memory of the pods whose name starts with pod_prefix, between start and end (seconds
    on the sampler's clock), from read_csv() rows: {samples, cpu_mean_m, cpu_peak_m, mem_peak_mib}.
    With several replicas, the values of one sample time are summed first."""
    per_t: dict[float, dict[str, float]] = {}
    for row in rows:
        if not row["pod"].startswith(pod_prefix) or row["t"] is None or not start <= row["t"] <= end:
            continue
        slot = per_t.setdefault(row["t"], {"cpu": 0.0, "mem": 0.0, "cpu_seen": 0, "mem_seen": 0})
        if row["cpu_m"] is not None:
            slot["cpu"] += row["cpu_m"]
            slot["cpu_seen"] += 1
        if row["mem_mib"] is not None:
            slot["mem"] += row["mem_mib"]
            slot["mem_seen"] += 1
    cpus = [v["cpu"] for v in per_t.values() if v["cpu_seen"]]
    mems = [v["mem"] for v in per_t.values() if v["mem_seen"]]
    return {"samples": len(per_t), "cpu_mean_m": _r(statistics.fmean(cpus)) if cpus else None,
            "cpu_peak_m": _r(max(cpus)) if cpus else None, "mem_peak_mib": _r(max(mems)) if mems else None}


def peaks_by_pod(rows: list[dict], pods: Iterable[str], start: float, end: float) -> dict[str, float | None]:
    """{pod: its highest cpu_m between start and end} from read_csv() rows, for each of `pods`
    (None for a pod with no CPU sample in the window)."""
    out: dict[str, float | None] = {}
    for pod in pods:
        values = [r["cpu_m"] for r in rows if r["pod"] == pod and r["t"] is not None and start <= r["t"] <= end
                  and r["cpu_m"] is not None]
        out[pod] = max(values) if values else None
    return out


# --- the Sampler -------------------------------------------------------------------------------------

@dataclass
class Phase:
    name: str
    start: float
    end: float | None = None
    extra: dict = field(default_factory=dict)


class Sampler:
    """Samples every node's kubelet stats/summary every `every` seconds while the `with` block runs.

    Writes results/<name>.csv (t,node,pod,cpu_m,mem_mib; t in seconds since the block started, one
    `_node` row per node per sample) and, with containers=True, results/<name>-containers.csv
    (t,node,pod,container,cpu_m,mem_mib,rss_mib). fetch(node) returns the parsed summary; it runs
    on the sampler's own thread, the nodes of one sample side by side. A failed fetch is counted and
    skipped, never raised. Phases (`with sampler.phase("create 10"):`) are kept for the results
    JSON, so the charts can shade them.
    """

    def __init__(self, name: str, every: float = 5.0, *, nodes: list[str], fetch: Callable[[str], dict],
                 alias: Callable[[str], str] | None = None, containers: bool = False,
                 namespaces: Iterable[str] | None = None, results: Path | None = None):
        if every <= 0:
            raise ValueError("every must be positive")
        self.name = name
        self.every = every
        self.nodes = list(nodes)
        self.fetch = fetch
        self.alias = alias or (lambda n: n)
        self.containers = containers
        self.namespaces = list(namespaces) if namespaces else None
        self.folder = results or RESULTS
        self.path = self.folder / f"{name}.csv"
        self.container_path = self.folder / f"{name}-containers.csv"
        self.samples = 0
        self.errors = 0
        self.latest: dict[str, dict] = {}
        self.phases: list[Phase] = []
        self.t0 = 0.0
        self.started_at = ""
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sink: CsvSink | None = None
        self._csink: CsvSink | None = None

    def elapsed(self) -> float:
        return round(time.time() - self.t0, 1)

    def sample_once(self) -> None:
        """One sample of every node (also used by the tests, with a fixture as fetch)."""
        t = self.elapsed()
        with ThreadPoolExecutor(max_workers=max(1, len(self.nodes))) as pool:
            fetched = list(pool.map(self._fetch_one, self.nodes))
        for node, summary in zip(self.nodes, fetched):
            if summary is None:
                continue
            label = self.alias(node)
            with self._lock:
                self.latest[label] = summary
                if self._sink is not None:
                    self._sink.write(summary_rows(summary, t, label, self.namespaces))
                if self._csink is not None:
                    self._csink.write(container_rows(summary, t, label, self.namespaces))
        self.samples += 1

    def _fetch_one(self, node: str) -> dict | None:
        try:
            return self.fetch(node)
        except Exception:  # noqa: BLE001 - a missed sample is counted, not fatal
            self.errors += 1
            return None

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            self.sample_once()
            self._stop.wait(max(0.0, self.every - (time.monotonic() - started)))

    def __enter__(self) -> "Sampler":
        self.t0 = time.time()
        self.started_at = utc_now()
        self._sink = CsvSink(self.path, CSV_HEADER)
        if self.containers:
            self._csink = CsvSink(self.container_path, CONTAINER_HEADER)
        self._thread = threading.Thread(target=self._loop, name=f"sampler-{self.name}", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(30.0, 3 * self.every))
        for sink in (self._sink, self._csink):
            if sink is not None:
                sink.close()
        for phase in self.phases:
            if phase.end is None:
                phase.end = self.elapsed()
        files = self.path.name + (f", {self.container_path.name}" if self.containers else "")
        print(f"  (metrics: results/{files}: {self.samples} samples every {self.every:g} s"
              + (f", {self.errors} missed" if self.errors else "") + ")", flush=True)

    @contextmanager
    def phase(self, name: str, **extra):
        """Record the start and end (seconds on the sampler's clock) of a step of the test."""
        item = Phase(name=name, start=self.elapsed(), extra=dict(extra))
        self.phases.append(item)
        try:
            yield item
        finally:
            item.end = self.elapsed()

    def phase_list(self) -> list[dict]:
        return [{"name": p.name, "start": p.start, "end": p.end, **p.extra} for p in self.phases]

    def node_totals(self) -> dict[str, dict]:
        """{node alias: {cpu_m, mem_mib}} from the latest sample of each node."""
        with self._lock:
            latest = dict(self.latest)
        out = {}
        for label, summary in latest.items():
            node = summary.get("node", {})
            out[label] = {"cpu_m": _cpu_m(node.get("cpu")), "mem_mib": _mib(node.get("memory"))}
        return out

    def pods_now(self) -> dict[str, dict]:
        """{pod name: pod_stats(...) + node} from the latest sample of each node."""
        with self._lock:
            latest = dict(self.latest)
        out = {}
        for label, summary in latest.items():
            for pod in summary.get("pods", []):
                name = pod.get("podRef", {}).get("name")
                if name:
                    stats = pod_stats(summary, name) or {}
                    stats["node"] = label
                    out[name] = stats
        return out
