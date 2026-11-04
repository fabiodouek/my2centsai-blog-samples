import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { fmtShortTime } from '../../lib/format';
import type { ProxyEvent } from '../../lib/types';

const BINS = 15;
const SLOTS = 4;
const SERIES = ['var(--series-1)', 'var(--series-2)', 'var(--series-3)', 'var(--series-4)'];
const OTHER = 'var(--series-other)';
const PLOT_H = 56;
/** Left margin for the y-axis numbers, so they never sit on a column. */
const GUTTER = 26;

// Colour follows the host, never its rank: a host keeps its slot while it stays in the window.
const slotOf = new Map<string, number>();

interface Bin {
  start: number;
  total: number;
  byKey: Map<string, number>; // host or "other"
}

/**
 * Requests per minute over the last 15 minutes, stacked by host (top four, the rest as other).
 * `loaded` is how many requests the feed below holds (its "All" count), shown next to the 15-minute total.
 */
export function HostColumns({ events, now, loaded }: { events: ProxyEvent[]; now: number; loaded?: number }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(320);
  const [hover, setHover] = useState<number | null>(null);

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(Math.max(160, el.clientWidth)));
    ro.observe(el);
    setWidth(Math.max(160, el.clientWidth));
    return () => ro.disconnect();
  }, []);

  const minute = Math.floor(now / 60_000) * 60_000;
  const t0 = minute - (BINS - 1) * 60_000;

  const { bins, keys, totals, windowTotal } = useMemo(() => {
    const inWindow = events.filter((e) => e.ts >= t0);
    const hostTotals = new Map<string, number>();
    for (const e of inWindow) hostTotals.set(e.host, (hostTotals.get(e.host) ?? 0) + 1);

    // Release slots of hosts that left the window, then give free slots to the busiest hosts.
    for (const h of [...slotOf.keys()]) if (!hostTotals.has(h)) slotOf.delete(h);
    const ranked = [...hostTotals.entries()].sort((a, b) => b[1] - a[1]).map(([h]) => h);
    const top = ranked.slice(0, SLOTS);
    for (const h of [...slotOf.keys()]) if (!top.includes(h)) slotOf.delete(h);
    for (const h of top) {
      if (slotOf.has(h)) continue;
      const used = new Set(slotOf.values());
      const free = [0, 1, 2, 3].find((i) => !used.has(i));
      if (free !== undefined) slotOf.set(h, free);
    }
    const keyed = top.slice().sort((a, b) => slotOf.get(a)! - slotOf.get(b)!);

    const bins: Bin[] = Array.from({ length: BINS }, (_, i) => ({ start: t0 + i * 60_000, total: 0, byKey: new Map() }));
    for (const e of inWindow) {
      const i = Math.min(BINS - 1, Math.floor((e.ts - t0) / 60_000));
      if (i < 0) continue;
      const k = slotOf.has(e.host) ? e.host : 'other';
      bins[i].total++;
      bins[i].byKey.set(k, (bins[i].byKey.get(k) ?? 0) + 1);
    }
    const totals = new Map<string, number>();
    for (const h of keyed) totals.set(h, hostTotals.get(h) ?? 0);
    const otherTotal = inWindow.length - keyed.reduce((a, h) => a + (hostTotals.get(h) ?? 0), 0);
    if (otherTotal > 0) totals.set('other', otherTotal);
    return { bins, keys: otherTotal > 0 ? [...keyed, 'other'] : keyed, totals, windowTotal: inWindow.length };
  }, [events, t0]);

  const max = Math.max(4, ...bins.map((b) => b.total));
  const yMax = niceCeil(max);
  const plotW = Math.max(40, width - GUTTER);
  const slot = plotW / BINS;
  const colW = Math.min(24, Math.max(4, slot - 6));
  const colorOf = (k: string) => (k === 'other' ? OTHER : SERIES[slotOf.get(k) ?? 0]);
  const hovered = hover !== null ? bins[hover] : null;

  return (
    <div className="chart">
      <div className="chart-head">
        <span className="chart-title">Requests per minute</span>
        <span className="chart-figure tabular" title="The chart covers the last 15 minutes; the list below holds everything loaded since the page opened (its All count).">
          {windowTotal.toLocaleString()} in the last 15 min
          {loaded !== undefined ? <span className="chart-figure-at"> · {loaded.toLocaleString()} loaded</span> : null}
        </span>
      </div>
      <div className="chart-plot" ref={wrapRef} onMouseLeave={() => setHover(null)}>
        <svg
          width={width}
          height={PLOT_H + 16}
          role="img"
          aria-label={`Requests per minute over the last 15 minutes, ${windowTotal} in that window. ${keys
            .map((k) => `${k}: ${totals.get(k)}`)
            .join(', ')}.`}
        >
          <line x1={GUTTER} x2={width} y1={0.5} y2={0.5} className="grid" />
          <line x1={GUTTER} x2={width} y1={PLOT_H / 2 + 0.5} y2={PLOT_H / 2 + 0.5} className="grid" />
          <line x1={GUTTER} x2={width} y1={PLOT_H + 0.5} y2={PLOT_H + 0.5} className="baseline" />
          {bins.map((b, i) => {
            const x = GUTTER + i * slot + (slot - colW) / 2;
            let base = PLOT_H;
            const segs = keys
              .map((k) => ({ k, n: b.byKey.get(k) ?? 0 }))
              .filter((s) => s.n > 0)
              .map((s, j, arr) => {
                // Each segment spans its share of the column; upper segments give 2px to the surface gap.
                const full = (s.n / yMax) * PLOT_H;
                const top = base - full;
                const h = Math.max(1, full - (j > 0 ? 2 : 0));
                base = top;
                const isTop = j === arr.length - 1;
                return <path key={s.k} d={colPath(x, top, colW, h, isTop ? Math.min(3, colW / 2, h) : 0)} fill={colorOf(s.k)} />;
              });
            return (
              <g key={b.start} opacity={hover === null || hover === i ? 1 : 0.45}>
                {segs}
                <rect
                  x={GUTTER + i * slot}
                  y={0}
                  width={slot}
                  height={PLOT_H}
                  fill="transparent"
                  onMouseEnter={() => setHover(i)}
                />
              </g>
            );
          })}
          <text x={GUTTER - 6} y={4} className="axis-label" textAnchor="end" dominantBaseline="middle">
            {yMax}
          </text>
          <text x={GUTTER - 6} y={PLOT_H / 2} className="axis-label" textAnchor="end" dominantBaseline="middle">
            {yMax / 2}
          </text>
          <text x={GUTTER} y={PLOT_H + 13} className="axis-label">
            −15m
          </text>
          <text x={GUTTER + plotW / 2} y={PLOT_H + 13} className="axis-label" textAnchor="middle">
            −7m
          </text>
          <text x={width} y={PLOT_H + 13} className="axis-label" textAnchor="end">
            now
          </text>
        </svg>
        {hovered ? (
          <div
            className="chart-tip"
            style={{ left: Math.min(width - 168, Math.max(0, GUTTER + (hover ?? 0) * slot + slot / 2 - 84)) }}
            role="presentation"
          >
            <p className="chart-tip-title">
              {fmtShortTime(hovered.start)} · {hovered.total} {hovered.total === 1 ? 'request' : 'requests'}
            </p>
            {keys
              .filter((k) => hovered.byKey.get(k))
              .map((k) => (
                <p key={k} className="chart-tip-row">
                  <span className="legend-swatch" style={{ background: colorOf(k) }} />
                  <span className="chart-tip-key">{k}</span>
                  <span className="tabular">{hovered.byKey.get(k)}</span>
                </p>
              ))}
          </div>
        ) : null}
      </div>
      {keys.length ? (
        <ul className="legend" aria-label="Hosts">
          {keys.map((k) => (
            <li key={k} className="legend-item">
              <span className="legend-swatch" style={{ background: colorOf(k) }} />
              <span className="legend-key" title={k}>
                {k}
              </span>
              <span className="legend-value tabular">{totals.get(k)}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function niceCeil(n: number) {
  const steps = [4, 6, 8, 10, 12, 16, 20, 24, 30, 40, 50, 60, 80, 100, 150, 200, 300, 500, 1000];
  return steps.find((s) => s >= n) ?? Math.ceil(n / 500) * 500;
}

/** Column segment with optional rounded top corners (square at the baseline). */
function colPath(x: number, y: number, w: number, h: number, r: number) {
  if (r <= 0) return `M${x},${y}h${w}v${h}h${-w}z`;
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}z`;
}
