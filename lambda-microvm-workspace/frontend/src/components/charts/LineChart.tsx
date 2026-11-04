import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import uPlot from 'uplot';
import { fmtShortTime, fmtTime } from '../../lib/format';

export interface Point {
  t: number; // epoch seconds
  v: number;
}

interface Props {
  title: string;
  points: Point[];
  /** Upper bound of the y axis for the given data maximum. */
  yMax: (dataMax: number) => number;
  fmt: (v: number) => string;
  /** Headline for the latest (or hovered) value. */
  describe: (v: number) => string;
  refLines?: { value: number; label: string }[];
  /** Allowed y tick steps (uPlot incrs). */
  yIncrs?: number[];
  windowS?: number;
  height?: number;
  dimmed?: boolean;
}

function cssVar(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/**
 * X ticks: whole minutes on the local clock, every `incr` seconds (uPlot picks it from the whole-minute
 * incrs below). Labels are HH:MM, so a tick off the minute repeats or mislabels one: 30 s ticks printed
 * "21:15, 21:16, 21:16, 21:17", and uPlot's own time splitter can put a tick a moment before the minute
 * when the scale bounds have sub-millisecond fractions. Distinct whole minutes give distinct labels.
 */
function minuteSplits(min: number, max: number, incr: number): number[] {
  const step = Math.max(1, Math.round(incr / 60)); // minutes between ticks
  const out: number[] = [];
  // Whole minutes since the epoch are whole local minutes too (time zone offsets are whole minutes).
  for (let t = Math.ceil(min / 60) * 60; t <= max; t += 60) {
    const d = new Date(t * 1000);
    if ((d.getHours() * 60 + d.getMinutes()) % step === 0) out.push(t);
  }
  return out;
}

/** Last guard: a label equal to the one before it is left blank. */
function blankRepeats(labels: string[]): string[] {
  return labels.map((l, i) => (i > 0 && labels[i - 1] === l ? '' : l));
}

function withAlpha(hex: string, a: number) {
  const m = hex.replace('#', '');
  const n = parseInt(m.length === 3 ? m.replace(/(.)/g, '$1$1') : m, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

/** A single-series time line (uPlot) with a crosshair; the header reads the hovered value. */
export function LineChart({ title, points, yMax, fmt, describe, refLines = [], yIncrs, windowS = 600, height = 84, dimmed }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const plot = useRef<uPlot | null>(null);
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const propsRef = useRef({ yMax, fmt, refLines, windowS });
  propsRef.current = { yMax, fmt, refLines, windowS };

  useLayoutEffect(() => {
    const el = host.current!;
    const series = cssVar('--series-1') || '#3987e5';
    const muted = cssVar('--n7') || '#969189';
    const grid = cssVar('--n4') || '#2d2925';
    const surface = cssVar('--n1') || '#14110e';
    const refColor = cssVar('--n6') || '#5f5a54';
    const font = `10px ${cssVar('--font-ui') || 'system-ui'}`;

    const opts: uPlot.Options = {
      width: Math.max(160, el.clientWidth),
      height,
      // Right padding: the last time label is centred on its tick and would be clipped at the edge.
      padding: [8, 18, 0, 0],
      legend: { show: false },
      cursor: {
        y: false,
        drag: { x: false, y: false, setScale: false },
        points: { size: 8, width: 2, stroke: surface, fill: series },
      },
      scales: {
        x: {
          time: true,
          range: (_u, min, max) => {
            const w = propsRef.current.windowS;
            // min/max are null before the first sample; Math.min(null, x) would be 0 (1970).
            const hi = max != null && Number.isFinite(max) ? max : Date.now() / 1000;
            // Grow with the data (at least 2 min wide) up to the full window.
            const lo = min != null && Number.isFinite(min) ? Math.min(hi - 120, Math.max(min, hi - w)) : hi - w;
            return [lo, hi];
          },
        },
        y: { range: (_u, _min, max) => [0, propsRef.current.yMax(max != null && Number.isFinite(max) ? max : 0)] },
      },
      axes: [
        {
          stroke: muted,
          font,
          size: 18,
          gap: 4,
          space: 64,
          // Whole minutes only: labels are HH:MM, so 30 s ticks would print the same minute twice.
          incrs: [60, 120, 300, 600, 900, 1800, 3600],
          splits: (_u, _i, min, max, incr) => minuteSplits(min, max, incr),
          grid: { show: false },
          ticks: { show: false },
          values: (_u, vals) => blankRepeats(vals.map((v) => fmtShortTime(v * 1000))),
        },
        {
          stroke: muted,
          font,
          size: 46,
          gap: 4,
          space: 22,
          ...(yIncrs ? { incrs: yIncrs } : {}),
          grid: { stroke: grid, width: 1 },
          ticks: { show: false },
          values: (_u, vals) => vals.map((v) => propsRef.current.fmt(v)),
        },
      ],
      series: [
        {},
        {
          stroke: series,
          width: 2,
          fill: withAlpha(series, 0.1),
          points: { show: false },
          spanGaps: false,
        },
      ],
      hooks: {
        setCursor: [(u) => setHoverIdx(u.cursor.idx ?? null)],
        draw: [
          (u) => {
            const ctx = u.ctx;
            for (const r of propsRef.current.refLines) {
              const y = Math.round(u.valToPos(r.value, 'y', true)) + 0.5;
              if (y < u.bbox.top || y > u.bbox.top + u.bbox.height) continue;
              ctx.save();
              ctx.strokeStyle = refColor;
              ctx.lineWidth = 1;
              ctx.beginPath();
              ctx.moveTo(u.bbox.left, y);
              ctx.lineTo(u.bbox.left + u.bbox.width, y);
              ctx.stroke();
              ctx.fillStyle = muted;
              ctx.font = `${10 * devicePixelRatio}px ${cssVar('--font-ui') || 'system-ui'}`;
              ctx.textAlign = 'right';
              ctx.fillText(r.label, u.bbox.left + u.bbox.width - 4 * devicePixelRatio, y - 4 * devicePixelRatio);
              ctx.restore();
            }
          },
        ],
      },
    };
    const u = new uPlot(opts, [[], []], el);
    plot.current = u;
    const ro = new ResizeObserver(() => u.setSize({ width: Math.max(160, el.clientWidth), height }));
    ro.observe(el);
    return () => {
      ro.disconnect();
      u.destroy();
      plot.current = null;
    };
  }, [height]);

  useEffect(() => {
    plot.current?.setData([points.map((p) => p.t), points.map((p) => p.v)]);
  }, [points]);

  const shown = hoverIdx !== null && points[hoverIdx] ? points[hoverIdx] : points[points.length - 1];
  return (
    <div className={`chart${dimmed ? ' is-dimmed' : ''}`}>
      <div className="chart-head">
        <span className="chart-title">{title}</span>
        <span className="chart-figure tabular">
          {shown ? describe(shown.v) : '–'}
          {shown && hoverIdx !== null ? <span className="chart-figure-at"> at {fmtTime(shown.t * 1000)}</span> : null}
        </span>
      </div>
      <div className="chart-plot" role="img" aria-label={`${title}: ${shown ? describe(shown.v) : 'no samples yet'}`}>
        <div ref={host} style={points.length < 2 ? { visibility: 'hidden' } : undefined} />
        {points.length < 2 ? (
          <span className="chart-wait">{dimmed ? 'Not enough samples to draw' : 'Collecting samples, one every 5 s'}</span>
        ) : null}
      </div>
    </div>
  );
}
