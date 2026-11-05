import { useStore } from '../lib/store';
import type { QuotaSegment } from '../lib/types';

type Cell = 'run' | 'sleep' | 'other' | 'free';

const CELL_LABEL: Record<Cell, string> = {
  run: 'running',
  sleep: 'suspended',
  other: 'other users',
  free: 'free',
};

const SLEEPING = new Set(['SUSPENDED', 'SUSPENDING']);

/**
 * The account's MicroVM memory quota (read from Service Quotas; 8 GB in a new account) drawn as one cell per GB, each
 * sandbox taking as many cells as its size. Above 16 GB a cell stands for several GB, so the bar keeps its length.
 */
export function QuotaMeter() {
  const quota = useStore((s) => s.quota);
  const sandboxes = useStore((s) => s.sandboxes);
  const config = useStore((s) => s.config);

  const quotaGb = quota?.quotaGb ?? config?.quotaGb ?? 8;
  const cellGb = quotaGb <= 16 ? 1 : Math.ceil(quotaGb / 16);
  const slots = Math.max(1, Math.round(quotaGb / cellGb));
  const live = (sandboxes ?? []).filter((s) => s.state !== 'TERMINATED');
  // The backend's segments (every live MicroVM in the account, with its size); before the first answer, ours only
  const segments: QuotaSegment[] = quota?.segments.length
    ? quota.segments
    : live.map((s) => ({ id: s.id, name: s.name, state: s.state, mine: true, gb: s.memoryGb }));
  const usedGb = quota?.usedGb ?? segments.reduce((n, x) => n + x.gb, 0);

  const cells: { kind: Cell; title: string; first: boolean }[] = [];
  const order = (x: QuotaSegment) => (!x.mine ? 2 : SLEEPING.has(x.state ?? '') ? 1 : 0);
  for (const seg of [...segments].sort((a, b) => order(a) - order(b))) {
    const kind: Cell = !seg.mine ? 'other' : SLEEPING.has(seg.state ?? '') ? 'sleep' : 'run';
    const n = Math.max(1, Math.round(seg.gb / cellGb));
    for (let i = 0; i < n && cells.length < slots; i++) {
      cells.push({ kind, first: i === 0, title: `${seg.mine ? (seg.name ?? 'sandbox') : 'another user’s MicroVM'} · ${seg.gb} GB · ${CELL_LABEL[kind]}` });
    }
  }
  while (cells.length < slots) cells.push({ kind: 'free', first: true, title: `${cellGb} GB · free` });

  const count = (k: Cell) => segments.filter((x) => (k === 'other' ? !x.mine : x.mine && (k === 'sleep') === SLEEPING.has(x.state ?? ''))).length;
  const present = (['run', 'sleep', 'other'] as Cell[]).filter((c) => cells.some((x) => x.kind === c));
  const full = usedGb >= quotaGb;
  const fmt = (gb: number) => (Number.isInteger(gb) ? String(gb) : gb.toFixed(1));
  const src = quota?.quotaSource ?? null;
  const sourceText = !src
    ? undefined
    : src.kind === 'service_quotas'
      ? `${fmt(quotaGb)} GB · this account's Service Quotas limit for MicroVM memory (${src.name ?? 'Max allocated ARM_64 MicroVM memory'}, ${src.quotaCode ?? 'L-CD1C0CC4'}${src.adjustable ? '; adjustable' : ''}; differs per account)`
      : `${fmt(quotaGb)} GB · the configured default (QUOTA_GB): Service Quotas couldn't be read${src.error ? ` (${src.error})` : ''}`;

  return (
    <div className="quota" title={sourceText}>
      <div className="quota-head">
        <span className="quota-title">MicroVM memory</span>
        <span className={`quota-value tabular${full ? ' is-full' : ''}`}>{quota ? `${fmt(usedGb)} of ${quotaGb} GB` : '…'}</span>
      </div>
      <div
        className="quota-cells"
        role="img"
        aria-label={`${fmt(usedGb)} of ${quotaGb} GB used: ${count('run')} running, ${count('sleep')} suspended${count('other') ? `, ${count('other')} from other users` : ''}`}
      >
        {cells.map((c, i) => (
          <span key={i} className={`quota-cell cell-${c.kind}${c.first && i > 0 ? ' is-start' : ''}`} title={c.title} />
        ))}
      </div>
      <div className="quota-legend" aria-hidden>
        {present.length ? (
          present.map((c) => (
            <span key={c} className="legend-item">
              <span className={`legend-swatch cell-${c}`} />
              {CELL_LABEL[c]}
            </span>
          ))
        ) : (
          <span className="legend-item">{slots} GB, all free</span>
        )}
      </div>
    </div>
  );
}
