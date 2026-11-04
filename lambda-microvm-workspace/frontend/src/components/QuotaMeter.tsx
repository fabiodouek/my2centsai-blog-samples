import { useStore } from '../lib/store';

type Cell = 'run' | 'sleep' | 'other' | 'free';

const CELL_LABEL: Record<Cell, string> = {
  run: 'running',
  sleep: 'suspended',
  other: 'other users',
  free: 'free',
};

/** The account's 8 GB MicroVM quota drawn as one cell per 2 GB sandbox slot. */
export function QuotaMeter() {
  const quota = useStore((s) => s.quota);
  const sandboxes = useStore((s) => s.sandboxes);
  const config = useStore((s) => s.config);

  const quotaGb = quota?.quotaGb ?? config?.quotaGb ?? 8;
  const vmGb = quota?.vmMemoryGb ?? config?.vmMemoryGb ?? 2;
  const slots = Math.max(1, Math.round(quotaGb / vmGb));
  const live = (sandboxes ?? []).filter((s) => s.state !== 'TERMINATED');
  const sleeping = live.filter((s) => s.state === 'SUSPENDED' || s.state === 'SUSPENDING').length;
  const running = live.length - sleeping;
  const usedGb = quota?.usedGb ?? live.length * vmGb;
  const userGb = quota?.userGb ?? live.length * vmGb;
  const other = Math.max(0, Math.round((usedGb - userGb) / vmGb));

  const cells: Cell[] = [];
  for (let i = 0; i < running && cells.length < slots; i++) cells.push('run');
  for (let i = 0; i < sleeping && cells.length < slots; i++) cells.push('sleep');
  for (let i = 0; i < other && cells.length < slots; i++) cells.push('other');
  while (cells.length < slots) cells.push('free');

  const present = (['run', 'sleep', 'other'] as Cell[]).filter((c) => cells.includes(c));
  const full = usedGb >= quotaGb;

  return (
    <div className="quota">
      <div className="quota-head">
        <span className="quota-title">MicroVM memory</span>
        <span className={`quota-value tabular${full ? ' is-full' : ''}`}>
          {quota ? `${usedGb} of ${quotaGb} GB` : '…'}
        </span>
      </div>
      <div
        className="quota-cells"
        role="img"
        aria-label={`${usedGb} of ${quotaGb} GB used: ${running} running, ${sleeping} suspended${other ? `, ${other} from other users` : ''}`}
      >
        {cells.map((c, i) => (
          <span key={i} className={`quota-cell cell-${c}`} title={`${vmGb} GB · ${CELL_LABEL[c]}`} />
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
          <span className="legend-item">{slots} slots of {vmGb} GB, all free</span>
        )}
      </div>
    </div>
  );
}
