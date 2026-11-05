import { useEffect, useId, useMemo, useState } from 'react';
import { RefreshCw, RotateCcw } from 'lucide-react';
import { api } from '../../lib/api';
import { fmtAgo, fmtDateTime, fmtTime, shortId } from '../../lib/format';
import { useNow, usePolling, usePref } from '../../lib/hooks';
import { noteWrite, usePolicy, usePollS } from '../../lib/policy';
import { toast } from '../../lib/store';
import type { ControlObject, ProxyConfigView, ResetPlan, SyncState } from '../../lib/types';
import { Button, ConfirmDialog, Empty, Segmented, SkeletonRows } from '../ui/primitives';
import { fmtSeconds } from './ApplyStatus';

type FileKey = 'config' | 'scope';

const BADGE: Record<SyncState, { label: string; tone: 'run' | 'deny' }> = {
  in_sync: { label: 'In sync', tone: 'run' },
  pending: { label: 'Pending', tone: 'run' },
  refused: { label: 'Refused', tone: 'deny' },
  stale: { label: 'Stale', tone: 'deny' },
};

/** Absolute time (with the date when it isn't today) plus how long ago, ISO in the tooltip. */
function Stamp({ ts, now }: { ts: number | null; now: number }) {
  if (!ts) return <span className="config-none">–</span>;
  const abs = now - ts < 12 * 3600_000 ? fmtTime(ts) : fmtDateTime(ts);
  return (
    <span title={new Date(ts).toISOString()}>
      <span className="tabular">{abs}</span>
      <span className="config-ago tabular"> · {fmtAgo(ts, now)}</span>
    </span>
  );
}

function Version({ v }: { v: number | null }) {
  return <span className="tabular">{v === null ? '–' : `v${v}`}</span>;
}

/** The sync state as the page sees it now: the server's answer, aged locally while the tab sits open. */
function liveState(v: ProxyConfigView, now: number): { state: SyncState; nextIn: number | null; ageS: number | null } {
  const since = (now - v.receivedAt) / 1000;
  const sc = v.applied.sidecar;
  const ageS = sc.heartbeatAgeS === null ? null : sc.heartbeatAgeS + since;
  const nextIn = sc.nextPollInS === null ? null : Math.max(0, sc.nextPollInS - since);
  const state = ageS !== null && ageS > sc.staleAfterS && v.sync.state !== 'refused' ? 'stale' : v.sync.state;
  return { state, nextIn, ageS };
}

function ObjectFacts({ o, now, extra }: { o: ControlObject; now: number; extra?: React.ReactNode }) {
  return (
    <dl className="config-facts">
      <dt>Version</dt>
      <dd>
        <Version v={o.version} />
        {o.updatedBy ? <span className="config-ago"> · {o.updatedBy}</span> : null}
      </dd>
      <dt>Last modified</dt>
      <dd>
        <Stamp ts={o.lastModified} now={now} />
      </dd>
      <dt>ETag</dt>
      <dd className="mono config-id" title={o.etag ?? undefined}>
        {o.etag ? o.etag.replace(/"/g, '') : '–'}
      </dd>
      {o.versionId ? (
        <>
          <dt>S3 version</dt>
          <dd className="mono config-id" title={o.versionId}>
            {shortId(o.versionId)}
          </dd>
        </>
      ) : null}
      {extra}
    </dl>
  );
}

/**
 * Admins only: iron-proxy.yaml and aws-scope.json as they are in part 2's control bucket (what the proxy should
 * run) beside what the control sidecar reports it applied (status.json), with a badge for whether they match.
 * The backend redacts secret material, which neither file should hold.
 */
export function ConfigSection() {
  const tick = usePolicy((s) => s.tick);
  // Follow the status poller: a new applied version or a new heartbeat refreshes this view too
  const mark = usePolicy((s) =>
    s.status
      ? `${s.status.config.version}/${s.status.scope.version}/${s.status.target.configVersion}/${s.status.target.scopeVersion}/${s.status.sidecar.updatedAt}`
      : '',
  );
  const view = usePolling(api.policyConfig, 15_000, true, 'policy-config');
  const refresh = view.refresh;
  useEffect(() => {
    if (tick) void refresh();
  }, [tick, refresh]);
  useEffect(() => {
    if (mark) void refresh();
  }, [mark, refresh]);
  // Read again just after the sidecar's next check, so the badge and the times follow each round
  const nextAt = view.data?.applied.sidecar.nextPollInS != null ? view.data.receivedAt + view.data.applied.sidecar.nextPollInS * 1000 : null;
  useEffect(() => {
    if (nextAt === null) return;
    const wait = nextAt + 1500 - Date.now();
    if (wait <= 0 || wait > 120_000) return;
    const t = window.setTimeout(() => {
      if (!document.hidden) void refresh();
    }, wait);
    return () => window.clearTimeout(t);
  }, [nextAt, refresh]);
  const now = useNow();
  const [file, setFile] = usePref<FileKey>('ws.configFile.v1', 'config');

  const v = view.data;
  const text = v ? (file === 'config' ? v.content.config : v.content.scope) : '';
  const redactions = v ? v.redactions[file] : [];
  const lines = useMemo(() => text.replace(/\n$/, '').split('\n'), [text]);
  const redactedLines = useMemo(() => new Set(redactions.map((r) => r.line)), [redactions]);

  if (!v && view.error) {
    return (
      <Empty
        title="Couldn’t load the proxy config"
        action={
          <Button size="sm" icon={<RefreshCw size={14} aria-hidden />} onClick={() => void refresh()}>
            Retry
          </Button>
        }
      >
        {view.error.message}
      </Empty>
    );
  }
  if (!v) {
    return (
      <div className="policy-grid">
        <SkeletonRows rows={12} />
        <SkeletonRows rows={8} />
      </div>
    );
  }

  const { state, nextIn, ageS } = liveState(v, now);
  const badge = BADGE[state];
  const sc = v.applied.sidecar;
  const desiredScope = v.desired.scope;
  const fileLabel = file === 'config' ? v.desired.config.key : (desiredScope?.key ?? 'aws-scope.json');

  return (
    <div className="policy-grid config-grid">
      <div className="policy-main">
        <div className="section-title-row">
          <h2 className="policy-h2">Proxy config</h2>
          <span className="policy-meta">
            What iron-proxy should run, as it is in the control bucket. Fetched {fmtAgo(v.receivedAt, now)}.
          </span>
        </div>
        <div className="config-toolbar">
          <Segmented<FileKey>
            label="File"
            value={file}
            onChange={setFile}
            options={[
              { value: 'config', label: v.desired.config.key },
              { value: 'scope', label: desiredScope?.key ?? 'aws-scope.json' },
            ]}
          />
          <span className="policy-meta tabular">
            {lines.length.toLocaleString()} lines
            {file === 'config' && v.desired.config.size !== null ? ` · ${(v.desired.config.size / 1024).toFixed(1)} KB` : ''}
          </span>
          <Button
            size="sm"
            variant="ghost"
            className="config-refresh"
            icon={<RefreshCw size={14} aria-hidden />}
            loading={view.loading}
            onClick={() => void refresh()}
          >
            Refresh
          </Button>
        </div>
        {redactions.length ? (
          <p className="pane-note is-warn" role="note">
            The server redacted {redactions.length === 1 ? 'one value' : `${redactions.length} values`} that looked like
            secret material ({[...new Set(redactions.map((r) => r.what))].join(', ')}
            {file === 'config' ? `; line ${[...redactedLines].filter((n) => n > 0).join(', ')}` : ''}). This file should
            hold none: iron-proxy reads secrets from Secrets Manager.
          </p>
        ) : null}
        {text ? (
          <pre className="config-code" tabIndex={0} aria-label={`${fileLabel}, as it is in the control bucket`}>
            <code>
              {lines.map((line, i) => (
                <span key={i} className={`config-line${redactedLines.has(i + 1) && file === 'config' ? ' is-redacted' : ''}`}>
                  <span className="config-ln" aria-hidden>
                    {i + 1}
                  </span>
                  {line || ' '}
                </span>
              ))}
            </code>
          </pre>
        ) : (
          <Empty title={`No ${fileLabel} in the bucket`}>Part 2 seeds it when it’s applied with control_plane=true.</Empty>
        )}
      </div>

      <aside className="policy-side config-side" aria-label="Sync state">
        <div className="config-sync" aria-live="polite">
          <span className={`state-pill tone-${badge.tone}`}>
            <span className={`dot tone-${badge.tone}${state === 'pending' ? ' pulse' : ''}`} aria-hidden />
            {badge.label}
          </span>
          {state === 'pending' ? (
            <span className="config-next tabular" aria-hidden>
              {nextIn === null ? '' : nextIn >= 1 ? `next check in ${Math.ceil(nextIn)} s` : 'check due now'}
            </span>
          ) : null}
        </div>
        <p className={`config-detail${badge.tone === 'deny' ? ' is-deny' : ''}`}>
          {state === 'stale' && v.sync.state !== 'stale'
            ? `The control sidecar hasn’t reported for ${Math.round(ageS ?? 0)} s (over ${sc.staleAfterS} s); this view may be out of date.`
            : v.sync.detail}
        </p>

        <h3 className="policy-h3">Desired · control bucket</h3>
        <p className="config-file mono">{v.desired.config.key}</p>
        <ObjectFacts o={v.desired.config} now={now} />
        {desiredScope ? (
          <>
            <p className="config-file mono">{desiredScope.key}</p>
            <ObjectFacts
              o={desiredScope}
              now={now}
              extra={
                <>
                  <dt>AWS access</dt>
                  <dd>
                    {desiredScope.role ?? '–'}
                    {desiredScope.sessionPolicy === null ? '' : desiredScope.sessionPolicy ? ' · session policy' : ' · no session policy'}
                    {desiredScope.seed ? ' · seed' : ''}
                  </dd>
                </>
              }
            />
          </>
        ) : null}

        <h3 className="policy-h3">Applied · {v.applied.statusKey}</h3>
        <dl className="config-facts">
          <dt>Config</dt>
          <dd>
            applied <Version v={v.applied.config.version} />
            {v.applied.config.syncedVersion !== v.applied.config.version ? (
              <span className="config-ago">
                {' '}
                · synced <Version v={v.applied.config.syncedVersion} />
              </span>
            ) : null}
            <span className="fact-line">
              <Stamp ts={v.applied.config.appliedAt} now={now} />
            </span>
          </dd>
          <dt>AWS scope</dt>
          <dd>
            applied <Version v={v.applied.scope.version} />
            {v.applied.scope.syncedVersion !== null && v.applied.scope.syncedVersion !== v.applied.scope.version ? (
              <span className="config-ago">
                {' '}
                · synced <Version v={v.applied.scope.syncedVersion} />
              </span>
            ) : null}
            {v.applied.scope.role ? <span className="config-ago"> · {v.applied.scope.role}</span> : null}
            <span className="fact-line">
              <Stamp ts={v.applied.scope.appliedAt} now={now} />
            </span>
          </dd>
          <dt>Reload</dt>
          <dd className={v.applied.reload.ok === false ? 'is-deny' : undefined}>
            {v.applied.reload.ok === null ? '–' : v.applied.reload.ok ? 'OK' : 'Failed'}
            {v.applied.reload.httpStatus !== null ? <span className="config-ago tabular"> · HTTP {v.applied.reload.httpStatus}</span> : null}
            {v.applied.reload.error ? <span className="fact-line">{v.applied.reload.error}</span> : null}
            <span className="fact-line">
              <Stamp ts={v.applied.reload.at} now={now} />
            </span>
          </dd>
          {v.applied.config.refusedReason || v.applied.config.rejectedVersion !== null ? (
            <>
              <dt>Refused</dt>
              <dd className="is-deny">
                <Version v={v.applied.config.rejectedVersion} />
                {v.applied.config.refusedReason ? <span className="fact-line">{v.applied.config.refusedReason}</span> : null}
              </dd>
            </>
          ) : null}
          {v.applied.scope.error ? (
            <>
              <dt>Role</dt>
              <dd className="is-deny">{v.applied.scope.error}</dd>
            </>
          ) : null}
          <dt>Heartbeat</dt>
          <dd className={state === 'stale' ? 'is-deny' : undefined}>
            <Stamp ts={sc.updatedAt} now={now} />
            <span className="fact-line config-ago">stale after {sc.staleAfterS} s without one</span>
          </dd>
          <dt>Checks</dt>
          <dd>
            <span className="tabular">every {sc.pollS ?? '?'} s</span>
            {nextIn !== null && state !== 'stale' ? (
              <span className="config-ago tabular"> · next {nextIn >= 1 ? `in ${Math.ceil(nextIn)} s` : 'due now'}</span>
            ) : null}
          </dd>
          {v.applied.latencyS !== null && state === 'in_sync' ? (
            <>
              <dt>Last change</dt>
              <dd className="tabular">applied {fmtSeconds(v.applied.latencyS)} after it was made</dd>
            </>
          ) : null}
          {v.sync.etagMatch !== null ? (
            <>
              <dt>ETag</dt>
              <dd className={v.sync.etagMatch ? undefined : 'config-ago'}>
                {v.sync.etagMatch ? 'the sidecar has the object in the bucket' : 'the sidecar hasn’t fetched the object in the bucket yet'}
              </dd>
            </>
          ) : null}
          {sc.task ? (
            <>
              <dt>Proxy task</dt>
              <dd className="mono config-id" title={sc.task}>
                {shortId(sc.task)}
                {sc.startedAt ? (
                  <span className="fact-line">
                    started <Stamp ts={sc.startedAt} now={now} />
                  </span>
                ) : null}
              </dd>
            </>
          ) : null}
        </dl>

        <FactoryReset />
      </aside>
    </div>
  );
}

const CONFIRM_WORD = 'reset';

/**
 * Admins: put the whole proxy policy back to part 2's deploy-time defaults. The dialog lists exactly what changes
 * (from GET /api/policy/reset) and the button only works after typing the confirmation word.
 */
function FactoryReset() {
  const pollS = usePollS();
  const [plan, setPlan] = useState<ResetPlan | null>(null);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [word, setWord] = useState('');
  const ids = useId();

  async function openDialog() {
    setLoading(true);
    try {
      setPlan(await api.resetPlan());
      setWord('');
      setOpen(true);
    } catch (e) {
      toast('error', 'Couldn’t prepare the reset', e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function confirm() {
    if (word.trim().toLowerCase() !== CONFIRM_WORD) return;
    setBusy(true);
    try {
      const r = await api.resetPolicy(CONFIRM_WORD);
      noteWrite(r);
      setOpen(false);
      toast('success', 'Proxy policy reset', `Config v${r.apply?.configVersion ?? '?'} goes live at the proxy’s next check (within about ${pollS} s).`);
    } catch (e) {
      toast('error', 'Couldn’t reset the proxy policy', e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const p = plan;
  const aws = p?.aws;
  return (
    <section className="config-reset" aria-labelledby={`${ids}-h`}>
      <h3 id={`${ids}-h`} className="policy-h3">
        Factory reset
      </h3>
      <p className="config-reset-text">
        Puts the allowlist, the credentials and AWS access back to part 2’s deploy-time defaults, for every sandbox on this
        proxy.
      </p>
      <Button variant="danger" size="sm" icon={<RotateCcw size={14} aria-hidden />} loading={loading} onClick={() => void openDialog()}>
        Reset to defaults…
      </Button>
      <ConfirmDialog
        open={open}
        title="Reset the proxy policy to its defaults?"
        confirmLabel="Reset proxy policy"
        busy={busy}
        confirmDisabled={word.trim().toLowerCase() !== CONFIRM_WORD}
        onConfirm={() => void confirm()}
        onClose={() => !busy && setOpen(false)}
      >
        {p ? (
          <div className="reset-plan">
            <p>
              It applies to <strong>every sandbox on this proxy</strong>, running ones included, at the proxy’s next check
              (within about {pollS} s). iron-proxy.yaml is regenerated from part 2’s template: config v{p.configVersion} → v
              {p.nextConfigVersion}.
            </p>
            {p.changes === 0 ? <p className="dialog-faint">Nothing differs from the defaults; the reset still writes a new config version.</p> : null}
            <dl className="reset-list">
              <dt>Allowlist</dt>
              <dd>
                {p.allowlist.remove.length || p.allowlist.add.length ? (
                  <>
                    {p.allowlist.remove.length ? (
                      <span className="reset-line">
                        Removes <span className="mono">{p.allowlist.remove.join(', ')}</span>
                      </span>
                    ) : null}
                    {p.allowlist.add.length ? (
                      <span className="reset-line">
                        Adds back <span className="mono">{p.allowlist.add.join(', ')}</span>
                      </span>
                    ) : null}
                  </>
                ) : (
                  <span className="reset-same">Already the {p.allowlist.after.length} seeded hosts</span>
                )}
              </dd>
              <dt>Credentials</dt>
              <dd>
                {p.credentials.reactivate.length ? (
                  <span className="reset-line is-warn">
                    Revoked, active again: <span className="mono">{p.credentials.reactivate.join(', ')}</span>
                  </span>
                ) : null}
                {p.credentials.builtinChanged.length ? (
                  <span className="reset-line">
                    Template rules restored for <span className="mono">{p.credentials.builtinChanged.join(', ')}</span>
                  </span>
                ) : null}
                {p.credentials.remove.length ? (
                  <>
                    <span className="reset-line">Out of the config (their secrets stay in Secrets Manager):</span>
                    <ul className="reset-creds">
                      {p.credentials.remove.map((c) => (
                        <li key={c.name}>
                          <span className="mono">{c.name}</span>
                          {c.env ? <span className="mono"> · {c.env}</span> : null}
                          {c.hosts.length ? <span> · {c.hosts.join(', ')}</span> : null}
                          <span className="reset-faint mono"> · {c.secretName}</span>
                        </li>
                      ))}
                    </ul>
                    <span className="reset-faint">
                      To restore one, add it again under Credentials with the same name and its value; new sandboxes get a
                      new placeholder.
                    </span>
                  </>
                ) : null}
                {!p.credentials.reactivate.length && !p.credentials.remove.length && !p.credentials.builtinChanged.length ? (
                  <span className="reset-same">Already the built-ins ({p.credentials.builtin.join(', ')}), all active</span>
                ) : null}
              </dd>
              <dt>AWS access</dt>
              <dd>
                {aws && aws.changes ? (
                  <span className="reset-line">
                    <span className="mono">{aws.from.role}</span>
                    {aws.from.sessionPolicy
                      ? `, session policy (${aws.from.actions.length} picks)`
                      : `, no session policy${aws.from.actions.length ? ` (${aws.from.actions.length} saved picks, cleared)` : ''}`}{' '}
                    → <span className="mono">{aws.to.role}</span>, no session policy, no picks
                  </span>
                ) : (
                  <span className="reset-same">Already {aws ? <span className="mono">{aws.to.role}</span> : null}, no session policy</span>
                )}
              </dd>
            </dl>
            <label className="reset-confirm" htmlFor={`${ids}-word`}>
              Type <span className="mono">{CONFIRM_WORD}</span> to confirm
            </label>
            <input
              id={`${ids}-word`}
              className="input mono-input"
              value={word}
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setWord(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  void confirm();
                }
              }}
            />
          </div>
        ) : null}
      </ConfirmDialog>
    </section>
  );
}
