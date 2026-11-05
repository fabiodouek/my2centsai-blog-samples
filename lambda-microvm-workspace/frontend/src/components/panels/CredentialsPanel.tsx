import { useEffect, useState } from 'react';
import { RefreshCw, RotateCcw, ShieldOff } from 'lucide-react';
import { api, normApply, warningOf } from '../../lib/api';
import { fmtAgo } from '../../lib/format';
import { useNow, usePolling } from '../../lib/hooks';
import { ensureOverview, noteWrite, usePolicy, usePollS } from '../../lib/policy';
import { openPolicy, toast, useStore } from '../../lib/store';
import type { Credential } from '../../lib/types';
import { Button, ConfirmDialog, Empty, KeyFob, Note, SkeletonRows } from '../ui/primitives';

export function CredentialsPanel() {
  const [fast, setFast] = useState(false);
  // While a revoke (proxy restart, ~3 min) or restore (~1 min) is in flight, refresh every 5 s.
  const creds = usePolling(api.credentials, fast ? 5_000 : 15_000, true, 'credentials');
  const pending = creds.data?.some((c) => c.status === 'revoking' || c.status === 'restoring') ?? false;
  useEffect(() => setFast(pending), [pending]);
  const now = useNow();
  const [confirm, setConfirm] = useState<{ cred: Credential; action: 'revoke' | 'restore' } | null>(null);
  const [busy, setBusy] = useState(false);
  const me = useStore((s) => s.me);
  const isAdmin = Boolean(me?.isAdmin);
  // With part 2's control plane on, revoke/restore reload iron-proxy at the control sidecar's next check (every 30 s)
  // instead of restarting it.
  useEffect(() => ensureOverview(), []);
  const reload = usePolicy((s) => Boolean(s.overview?.enabled));
  const pollS = usePollS();

  async function run() {
    if (!confirm) return;
    setBusy(true);
    const { cred, action } = confirm;
    try {
      const r = action === 'revoke' ? await api.revoke(cred.name) : await api.restore(cred.name);
      const byReload = r?.method === 'reload';
      if (byReload) noteWrite({ apply: normApply(r.apply), change: null, warnings: [], raw: r });
      toast(
        'success',
        byReload
          ? `${action === 'revoke' ? 'Revoking' : 'Restoring'} ${cred.name}: up to about ${pollS} s`
          : action === 'revoke'
            ? `Revoking ${cred.name}: about 3 minutes`
            : `Restoring ${cred.name}: about a minute`,
        warningOf(r) ?? undefined,
      );
      setConfirm(null);
      await creds.refresh();
    } catch (e) {
      toast('error', `Could not ${action} ${cred.name}`, e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="pane">
      <div className="pane-head">
        <Note>
          Sandboxes only hold placeholders. iron-proxy swaps in the real key on the way out. Counts cover the last 15
          minutes for every sandbox on this proxy.
          {isAdmin ? '' : ' Only admins can revoke or restore a key.'}
        </Note>
      </div>
      <div className="pane-body">
        {creds.data === null && creds.loading ? <SkeletonRows rows={3} height="112px" /> : null}
        {creds.data === null && creds.error ? (
          <Empty
            title="Couldn’t load credentials"
            action={
              <Button size="sm" icon={<RefreshCw size={14} aria-hidden />} onClick={() => void creds.refresh()}>
                Retry
              </Button>
            }
          >
            {creds.error.message}
          </Empty>
        ) : null}
        {creds.data && creds.data.length === 0 ? <Empty title="No credentials configured">The proxy has no secrets to swap.</Empty> : null}
        <ul className="keyboard" role="list">
          {creds.data?.map((c) => (
            <CredentialRow
              key={c.name}
              c={c}
              now={now}
              reload={reload}
              canAct={isAdmin}
              onAction={(action) => setConfirm({ cred: c, action })}
            />
          ))}
        </ul>
        {reload ? (
          <p className="pane-note pane-note-link">
            Add, rotate or delete credentials on the{' '}
            <button type="button" className="link-button" onClick={() => openPolicy('credentials')}>
              Proxy policy
            </button>{' '}
            page.
          </p>
        ) : null}
        {creds.updatedAt ? (
          <p className="pane-foot">
            Updated {fmtAgo(creds.updatedAt, now)}
            {creds.error ? ` · last refresh failed: ${creds.error.message}` : ''}
          </p>
        ) : null}
      </div>

      <ConfirmDialog
        open={confirm !== null}
        title={confirm?.action === 'revoke' ? `Revoke ${confirm.cred.name} for every sandbox?` : `Restore ${confirm?.cred.name ?? ''} for every sandbox?`}
        confirmLabel={confirm?.action === 'revoke' ? 'Revoke for all sandboxes' : 'Restore for all sandboxes'}
        tone={confirm?.action === 'revoke' ? 'danger' : 'primary'}
        busy={busy}
        onConfirm={() => void run()}
        onClose={() => !busy && setConfirm(null)}
      >
        {confirm?.action === 'revoke' && reload ? (
          <>
            <p>
              iron-proxy will be denied access to the <strong>{confirm.cred.name}</strong> secret and reloads its config,
              which drops its cached copy, at its next check for changes. It takes <strong>up to about {pollS} s</strong>,
              with no restart; until then the key is still swapped in.
            </p>
            <p>
              It affects every sandbox on this proxy, including ones you don’t own. Afterwards their placeholder goes
              out unswapped and the upstream rejects it until the key is restored.
            </p>
            <p className="dialog-faint">The secret itself is not deleted. Restore removes the deny policy.</p>
          </>
        ) : confirm?.action === 'revoke' ? (
          <>
            <p>
              iron-proxy will be denied access to the <strong>{confirm.cred.name}</strong> secret. This takes about{' '}
              <strong>3 minutes</strong>: the shared proxy restarts briefly so it drops its cached copy of the key
              (requests keep flowing while the new proxy task starts).
            </p>
            <p>
              It affects every sandbox on this proxy, including ones you don’t own. Afterwards their placeholder goes
              out unswapped and the upstream rejects it (Anthropic answers 401) until the key is restored.
            </p>
            <p className="dialog-faint">The secret itself is not deleted. Restore removes the deny policy.</p>
          </>
        ) : (
          <>
            <p>
              iron-proxy gets read access to the <strong>{confirm?.cred.name}</strong> secret again, for every sandbox on
              this proxy.
            </p>
            <p className="dialog-faint">
              {reload
                ? `iron-proxy reloads and fetches the key again at its next check, within about ${pollS} s.`
                : 'No restart: the proxy fetches the key again within about a minute.'}
            </p>
          </>
        )}
      </ConfirmDialog>
    </div>
  );
}

function CredentialRow({
  c,
  now,
  reload,
  canAct,
  onAction,
}: {
  c: Credential;
  now: number;
  reload: boolean;
  canAct: boolean;
  onAction: (a: 'revoke' | 'restore') => void;
}) {
  const hosts = Object.entries(c.hosts).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...hosts.map(([, n]) => n));
  const revoked = c.status === 'revoked';
  const label =
    reload && (c.status === 'revoking' || c.status === 'restoring')
      ? `${c.status === 'revoking' ? 'Revoking' : 'Restoring'}… (proxy reloading)`
      : STATUS_LABEL[c.status];
  return (
    <li className={`key-row${revoked ? ' is-revoked' : ''}`}>
      <div className="key-top">
        <KeyFob name={c.name} size="md" empty={revoked} />
        {c.kind === 'custom' ? <span className="tag">custom</span> : null}
        <span className={`key-state ${STATUS_TONE[c.status]}`} title={c.statusDetail ?? undefined}>
          {label}
          {c.statusSince && (c.status === 'revoking' || c.status === 'restoring') ? ` · ${fmtAgo(c.statusSince, now)}` : ''}
        </span>
        <span className="filter-spacer" />
        {!canAct ? null : c.status === 'revoked' || c.status === 'revoking' ? (
          <Button size="sm" icon={<RotateCcw size={14} aria-hidden />} onClick={() => onAction('restore')}>
            Restore
          </Button>
        ) : (
          <Button size="sm" variant="danger" icon={<ShieldOff size={14} aria-hidden />} onClick={() => onAction('revoke')}>
            Revoke
          </Button>
        )}
      </div>
      {c.statusDetail && (c.status === 'revoking' || c.status === 'restoring') ? (
        <p className="key-none" role="status">
          {c.statusDetail}
        </p>
      ) : null}
      <dl className="key-facts">
        <div>
          <dt>Uses, 15 min</dt>
          <dd className="key-num tabular">{c.uses15m.toLocaleString()}</dd>
        </div>
        <div>
          <dt>Last used</dt>
          {/* last_used only looks at the same 15-minute window as the counts */}
          <dd className="tabular">{c.lastUsed ? fmtAgo(c.lastUsed, now) : 'not in the last 15 min'}</dd>
        </div>
        <div className="key-placeholder">
          <dt>Placeholder in sandboxes</dt>
          {/* The backend already ends a shortened placeholder with "…" */}
          <dd className="mono" title={c.placeholderPrefix}>
            {c.placeholderPrefix || '–'}
          </dd>
        </div>
      </dl>
      {hosts.length ? (
        <ul className="key-hosts" role="list" aria-label={`Hosts that received the ${c.name} key`}>
          {hosts.map(([h, n]) => (
            <li key={h}>
              <span className="key-host mono" title={h}>
                {h}
              </span>
              <span className="key-bar" aria-hidden>
                <span style={{ width: `${(n / max) * 100}%` }} />
              </span>
              <span className="key-count tabular">{n}</span>
            </li>
          ))}
        </ul>
      ) : revoked ? (
        // Unused and not revoked needs no line: "Last used" above already says "not in the last 15 min".
        <p className="key-none">Not swapped in while revoked.</p>
      ) : null}
    </li>
  );
}

const STATUS_LABEL: Record<Credential['status'], string> = {
  active: 'Active',
  revoking: 'Revoking… (proxy restarting, ~3 min)',
  revoked: 'Revoked',
  restoring: 'Restoring… (~1 min)',
  unknown: 'Policy unknown',
};

const STATUS_TONE: Record<Credential['status'], string> = {
  active: 'tone-run',
  revoking: 'tone-sleep',
  revoked: 'tone-deny',
  restoring: 'tone-sleep',
  unknown: 'tone-off',
};
