import { useEffect } from 'react';
import { RefreshCw } from 'lucide-react';
import { api } from '../../lib/api';
import { fmtAgo, fmtDateTime, fmtTime } from '../../lib/format';
import { useNow, usePolling } from '../../lib/hooks';
import { usePolicy } from '../../lib/policy';
import type { PolicyChange } from '../../lib/types';
import { Button, Empty, SkeletonRows } from '../ui/primitives';

const KIND_LABEL: Record<string, string> = {
  'allowlist.add': 'Allowlist',
  'allowlist.remove': 'Allowlist',
  'credential.create': 'Credential',
  'credential.rotate': 'Credential',
  'credential.delete': 'Credential',
  'credential.revoke': 'Credential',
  'credential.restore': 'Credential',
  'aws.scope': 'AWS access',
  'policy.reset': 'Reset',
};

function when(ts: number | null, now: number): string {
  if (!ts) return '–';
  return now - ts < 12 * 3600_000 ? fmtTime(ts) : fmtDateTime(ts);
}

export function ChangeLogSection() {
  const tick = usePolicy((s) => s.tick);
  const changes = usePolling(() => api.policyChanges(100), 30_000, true, 'policy-changes');
  const refresh = changes.refresh;
  useEffect(() => {
    if (tick) void refresh();
  }, [tick, refresh]);
  const now = useNow();

  return (
    <div className="policy-log">
      <div className="section-title-row">
        <h2 className="policy-h2">Change log</h2>
        <span className="policy-meta">
          Who changed the proxy policy and when; values of credentials are never recorded.
          {changes.updatedAt ? ` Updated ${fmtAgo(changes.updatedAt, now)}.` : ''}
        </span>
      </div>
      {changes.data === null && changes.loading ? <SkeletonRows rows={8} /> : null}
      {changes.data === null && changes.error ? (
        <Empty
          title="Couldn’t load the change log"
          action={
            <Button size="sm" icon={<RefreshCw size={14} aria-hidden />} onClick={() => void refresh()}>
              Retry
            </Button>
          }
        >
          {changes.error.message}
        </Empty>
      ) : null}
      {changes.data && changes.data.length === 0 ? <Empty title="No changes yet">Edits made on this page show up here.</Empty> : null}
      {changes.data && changes.data.length ? (
        <div className="table-scroll">
          <table className="table log-table">
            <thead>
              <tr>
                <th scope="col">Time</th>
                <th scope="col">Who</th>
                <th scope="col">Area</th>
                <th scope="col">Change</th>
                <th scope="col" className="num">
                  Config
                </th>
                <th scope="col" className="num">
                  Scope
                </th>
              </tr>
            </thead>
            <tbody>
              {changes.data.map((c) => (
                <LogRow key={c.id} c={c} now={now} />
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

function LogRow({ c, now }: { c: PolicyChange; now: number }) {
  return (
    <tr>
      <td className="log-time tabular" title={c.at ? new Date(c.at).toISOString() : undefined}>
        {when(c.at, now)}
      </td>
      <td className="log-actor" title={c.actor ?? undefined}>
        {c.actor ?? '–'}
      </td>
      <td className="log-area">
        <span className="tag" title={c.kind}>
          {KIND_LABEL[c.kind] ?? c.kind}
        </span>
      </td>
      <td className="log-summary" title={c.summary}>
        {c.summary}
      </td>
      <td className="num tabular log-ver">{c.configVersion !== null ? `v${c.configVersion}` : '–'}</td>
      <td className="num tabular log-ver">{c.scopeVersion !== null ? `v${c.scopeVersion}` : '–'}</td>
    </tr>
  );
}
