const EIGHT_HOURS_S = 8 * 3600;

/**
 * Lifetime on one fixed 8 h scale for every sandbox, so rows compare at a glance: the filled part
 * is time used, the track beyond the sandbox's own max lifetime is drawn as unavailable.
 */
export function LifeBar({
  startedAt,
  maxDurationS,
  now,
  tone,
  height = 3,
}: {
  startedAt: number | null;
  maxDurationS: number;
  now: number;
  tone: string;
  height?: number;
}) {
  const scale = Math.max(EIGHT_HOURS_S, maxDurationS);
  const used = startedAt ? Math.min(maxDurationS, Math.max(0, (now - startedAt) / 1000)) : 0;
  const usedPct = (used / scale) * 100;
  const capPct = (maxDurationS / scale) * 100;
  const left = maxDurationS - used;
  return (
    <span
      className="lifebar"
      style={{ height }}
      role="meter"
      aria-label="Lifetime used"
      aria-valuemin={0}
      aria-valuemax={maxDurationS}
      aria-valuenow={Math.round(used)}
      aria-valuetext={`${Math.round(used / 60)} of ${Math.round(maxDurationS / 60)} minutes used`}
    >
      <span className="lifebar-cap" style={{ width: `${capPct}%` }} />
      <span className={`lifebar-used tone-${tone}${left < 1800 ? ' is-low' : ''}`} style={{ width: `${usedPct}%` }} />
    </span>
  );
}
