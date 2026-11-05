import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, Info, RefreshCw, Search, Undo2, Upload, X } from 'lucide-react';
import { api, ApiError } from '../../lib/api';
import {
  ensureDetails,
  knownAction,
  remember,
  retryCatalog,
  retryDetail,
  useCatalogVersion,
  useRoleCatalog,
  useServiceDetail,
} from '../../lib/awsCatalog';
import { fmtAgo, fmtShortTime } from '../../lib/format';
import { useNow, usePolling } from '../../lib/hooks';
import { noteWrite, usePolicy } from '../../lib/policy';
import { toast } from '../../lib/store';
import type {
  AccessLevel,
  AwsAction,
  AwsCatalog,
  AwsCoverage,
  AwsEffectiveRow,
  AwsPreview,
  AwsRole,
  AwsSearchResult,
  AwsServiceSummary,
  AwsWildcard,
  Tone,
} from '../../lib/types';
import { Button, Empty, Note, Segmented, SkeletonRows } from '../ui/primitives';
import { ApplyStatus } from './ApplyStatus';

const sorted = (xs: Iterable<string>) => [...new Set(xs)].sort();
const sameSet = (a: string[], b: string[]) => a.length === b.length && sorted(a).join('\n') === sorted(b).join('\n');
const prefixOf = (action: string) => action.split(':')[0] ?? '';
const SEARCH_LIMIT = 100;

type LevelFilter = 'All' | AccessLevel;
const LEVEL_OPTIONS: { value: LevelFilter; label: string }[] = [
  { value: 'All', label: 'All' },
  { value: 'List', label: 'List' },
  { value: 'Read', label: 'Read' },
  { value: 'Write', label: 'Write' },
  { value: 'Permissions', label: 'Permissions' },
  { value: 'Tagging', label: 'Tagging' },
];

const capitalize = (t: string) => (t ? t[0].toUpperCase() + t.slice(1) : t);

/** A short form of a server reason, for a row (the full text is in the title and in group headings). */
function shortReason(reason: string | null): string | null {
  if (!reason) return null;
  const r = reason.toLowerCase();
  if (r.startsWith('denied by')) return 'denied by the role';
  if (r.includes('outside the') && r.includes('role')) return 'outside this role';
  if (r.includes('ceiling')) return 'outside the read-only ceiling';
  if (r.includes('mapping')) return 'no endpoint mapping';
  if (r.includes('secret')) return 'never here: reads secrets';
  return reason.length > 44 ? `${reason.slice(0, 42)}…` : reason;
}

const TONE_LABEL: Record<Tone, string> = { all: 'all effective', partial: 'partly effective', none: 'no effect' };

/** How much of a pick is effective with the role: filled (all), half (some), empty ring (none). Colour plus shape. */
function ToneDot({ tone, title }: { tone: Tone; title?: string }) {
  return <span className={`tone-dot tone-${tone}`} role="img" aria-label={TONE_LABEL[tone]} title={title ?? TONE_LABEL[tone]} />;
}

function ToneLegend({ role }: { role: string | null }) {
  return (
    <p className="tone-legend">
      <span>
        <ToneDot tone="all" /> all effective
      </span>
      <span>
        <ToneDot tone="partial" /> some
      </span>
      <span>
        <ToneDot tone="none" /> none
      </span>
      <span className="tone-legend-note">with {role ? <span className="mono">{role}</span> : 'the role'}, computed from its policies</span>
    </p>
  );
}

export function AwsSection({ editable }: { editable: boolean }) {
  const tick = usePolicy((s) => s.tick);
  const status = usePolicy((s) => s.status);
  const now = useNow();
  const scope = usePolling(api.awsScope, 20_000, true, 'aws-scope');
  const refreshScope = scope.refresh;
  useEffect(() => {
    if (tick) void refreshScope();
  }, [tick, refreshScope]);

  // The admin's edits; null = follow the applied scope.
  const [roleEdit, setRoleEdit] = useState<string | null>(null);
  const [spEdit, setSpEdit] = useState<boolean | null>(null);
  const [selected, setSelected] = useState<string[] | null>(null);
  const edited = useRef(false);
  const [active, setActive] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [level, setLevel] = useState<LevelFilter>('All');
  const [opened, setOpened] = useState<string | null>(null); // a service opened from search results
  const [preview, setPreview] = useState<AwsPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [applying, setApplying] = useState(false);
  useCatalogVersion(); // re-render when a service's details or search results teach us about an action

  // Which role the page shows: the one being edited, else the applied one (null until the scope says, or for an
  // older scope without a role: the server's default).
  const appliedRoleRaw = scope.data?.role ?? null;
  const role = roleEdit ?? appliedRoleRaw;
  const catalog = useRoleCatalog(role);
  const roles = catalog.data?.roles ?? [];
  const defaultRole = roles.find((r) => r.default)?.id ?? catalog.data?.role ?? null;
  const appliedRole = appliedRoleRaw ?? defaultRole;
  const shownRole = role ?? catalog.current?.role ?? defaultRole;
  const appliedSp = scope.data?.sessionPolicy ?? false;
  const spOn = spEdit ?? appliedSp;

  // Follow the applied actions until the admin edits the selection.
  const applied = scope.data?.actions ?? null;
  useEffect(() => {
    if (applied && !edited.current) setSelected(applied);
  }, [applied]);

  // Load the services the selection touches, so its chips know what the role still allows.
  const selKey = selected ? sorted(selected).join(',') : null;
  useEffect(() => {
    if (selKey && spOn) ensureDetails(selKey.split(','), shownRole);
  }, [selKey, spOn, shownRole]);

  // Start on the first service with a chosen action.
  const services = catalog.data?.services;
  useEffect(() => {
    if (!active && services?.length && selected) {
      const ids = new Set(services.map((s) => s.id));
      const first = selected.map(prefixOf).find((p) => ids.has(p));
      setActive(first ?? services[0].id);
    }
  }, [active, services, selected]);

  // Live preview, debounced: role, switch and actions.
  const previewKey = selKey === null || !scope.data ? null : `${shownRole ?? ''}|${spOn ? 1 : 0}|${selKey}`;
  useEffect(() => {
    if (previewKey === null || !scope.data) return;
    const [r, on, acts] = previewKey.split('|');
    const actions = acts ? acts.split(',') : [];
    const sp = on === '1';
    const p0 = scope.data.preview;
    if (!edited.current && p0 && p0.sessionPolicy === sp && (p0.role ?? r) === r && sameSet(actions, scope.data.actions)) {
      setPreview(p0);
      setPreviewError(null);
      return;
    }
    let cancelled = false;
    setPreviewing(true);
    const t = window.setTimeout(async () => {
      try {
        const p = await api.awsPreview({ role: r || null, sessionPolicy: sp, actions });
        if (!cancelled) {
          setPreview(p);
          setPreviewError(null);
        }
      } catch (e) {
        if (!cancelled) setPreviewError(e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setPreviewing(false);
      }
    }, 250);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [previewKey, scope.data]);

  // Server-side search over the cached Service Authorization Reference, evaluated for the shown role.
  const searching = spOn && query.trim().length > 0;
  const lvl = level === 'All' ? null : level;
  const [search, setSearch] = useState<{ key: string; data: AwsSearchResult | null; error: string | null }>({ key: '', data: null, error: null });
  const searchKey = searching ? `${query.trim()}|${lvl ?? ''}|${shownRole ?? ''}` : '';
  useEffect(() => {
    setOpened(null);
    if (!searchKey) return;
    let cancelled = false;
    const [q, , r] = searchKey.split('|');
    api.awsSearch(q, lvl, SEARCH_LIMIT, r || null).then(
      (res) => {
        if (cancelled) return;
        remember(res.actions, r || null);
        setSearch({ key: searchKey, data: res, error: null });
      },
      (e: unknown) => {
        if (!cancelled) setSearch({ key: searchKey, data: null, error: e instanceof Error ? e.message : String(e) });
      },
    );
    return () => {
      cancelled = true;
    };
    // lvl and the role are part of searchKey
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchKey]);

  const counts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const a of selected ?? []) out[prefixOf(a)] = (out[prefixOf(a)] ?? 0) + 1;
    return out;
  }, [selected]);

  const toggle = useCallback((action: string, on: boolean) => {
    edited.current = true;
    setSelected((cur) => {
      const s = new Set(cur ?? []);
      if (on) s.add(action);
      else s.delete(action);
      return [...s];
    });
  }, []);

  const clearService = useCallback((id: string) => {
    edited.current = true;
    setSelected((cur) => (cur ?? []).filter((a) => prefixOf(a) !== id));
  }, []);

  function pickRole(id: string) {
    edited.current = true;
    setRoleEdit(id);
  }

  function setSessionPolicy(on: boolean) {
    edited.current = true;
    setSpEdit(on);
  }

  function reset() {
    edited.current = false;
    setRoleEdit(null);
    setSpEdit(null);
    if (applied) setSelected(applied);
  }

  const actionsChanged = selected !== null && applied !== null && !sameSet(selected, applied);
  const dirty = (shownRole !== null && appliedRole !== null && shownRole !== appliedRole) || spOn !== appliedSp || (spOn && actionsChanged);
  const fits = !spOn || (preview ? preview.fits : true);

  async function apply() {
    if (!selected) return;
    setApplying(true);
    try {
      const r = await api.applyAwsScope({ role: shownRole, sessionPolicy: spOn, actions: sorted(selected) });
      if (r.unchanged) toast('info', 'Nothing to apply', 'The proxy already uses this role and policy.');
      noteWrite(r);
      edited.current = false;
      setRoleEdit(null);
      setSpEdit(null);
      await refreshScope();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      toast('error', e instanceof ApiError && e.status === 409 ? 'The policy changed meanwhile' : 'Could not apply AWS access', msg);
    } finally {
      setApplying(false);
    }
  }

  const pick = useCallback((id: string) => {
    if (searching) setOpened(id);
    else setActive(id);
  }, [searching]);

  if (catalog.data === null || scope.data === null) {
    if ((catalog.error && !catalog.data) || scope.error) {
      return (
        <Empty
          title="Couldn’t load AWS access"
          action={
            <Button
              size="sm"
              icon={<RefreshCw size={14} aria-hidden />}
              onClick={() => {
                retryCatalog(role);
                void refreshScope();
              }}
            >
              Retry
            </Button>
          }
        >
          {catalog.error ?? scope.error?.message}
        </Empty>
      );
    }
    return <SkeletonRows rows={8} />;
  }

  const cat = catalog.data;
  // A preview for the role on screen: after a role switch the old one stays up (dimmed) until the new one arrives,
  // and its counts aren't shown against the new role
  const fresh = preview !== null && (preview.role ?? shownRole) === shownRole && preview.sessionPolicy === spOn;
  const chosen = new Set(selected ?? []);
  const nSelected = chosen.size;
  const nServices = Object.keys(counts).length;
  const result = search.key === searchKey ? search.data : null;
  const searchError = search.key === searchKey ? search.error : null;
  const listServices: ListItem[] = searching
    ? (result?.services ?? []).map((s) => ({ id: s.id, name: s.name, enabled: s.enabled, note: `${s.matches}`, noteLabel: `${s.matches} matching actions` }))
    : cat.services.map((s) => summaryItem(s, lvl));
  const panelId = searching ? opened : active;

  const applyControls = (
    <>
      <div className="aws-apply">
        {editable ? (
          <>
            <Button
              variant="primary"
              icon={<Upload size={14} aria-hidden />}
              loading={applying}
              disabled={!dirty || !fits || (spOn && (Boolean(previewError) || previewing))}
              onClick={() => void apply()}
              title={!dirty ? 'Nothing changed since the last apply' : !fits ? 'The session policy is over the 2,048-character limit' : undefined}
            >
              Apply to proxy
            </Button>
            <Button variant="ghost" icon={<Undo2 size={14} aria-hidden />} disabled={!dirty || applying} onClick={reset}>
              Reset
            </Button>
          </>
        ) : (
          <span className="form-hint">Only admins can change AWS access.</span>
        )}
      </div>
      <ApplyStatus live={false} className="aws-apply-status" />
      {status?.scope.sessionTokenUtilization != null ? (
        <p className="aws-packed tabular">
          Session token: {status.scope.sessionTokenUtilization}% of STS’s maximum size
          {status.scope.sessionTokenSize != null ? ` · ${status.scope.sessionTokenSize.toLocaleString()} bytes` : ''}.
        </p>
      ) : status?.scope.packedPolicySize != null ? (
        <p className="aws-packed tabular">STS packed the applied policy to {status.scope.packedPolicySize}% of its limit.</p>
      ) : null}
    </>
  );

  return (
    <div className="aws">
      <div className="aws-intro">
        <p>
          Sandbox AWS calls are re-signed by iron-proxy as one of four roles. The role sets what they can do; a session
          policy can narrow it to the actions you pick. iron-proxy also refuses every AWS service it isn’t told about
          before IAM sees the call, and never lets Secrets Manager through.
        </p>
        <p className="aws-current tabular">
          {status?.scope.sessionName ? (
            <>
              Current session <span className="mono">{status.scope.sessionName}</span>
              {status.scope.role ? <> · role <span className="mono">{status.scope.role}</span></> : null}
              {status.scope.sessionPolicy !== null ? ` · ${status.scope.sessionPolicy ? 'session policy on' : 'no session policy'}` : ''}
              {status.scope.expiration ? ` · expires ${fmtShortTime(status.scope.expiration)}` : ''}
              {status.scope.sourceIdentity ? ` · source identity ${status.scope.sourceIdentity}` : ''}
            </>
          ) : (
            'No session reported yet'
          )}
          {scope.data.version !== null ? ` · scope v${scope.data.version}` : ''}
          {scope.data.updatedAt ? ` set ${fmtAgo(scope.data.updatedAt, now)}` : ''}
          {scope.data.updatedBy ? ` by ${scope.data.updatedBy}` : ''}
        </p>
      </div>

      <RolePicker roles={roles} value={shownRole} applied={appliedRole} editable={editable} onPick={pickRole} />
      {shownRole === 'builder' ? (
        <p className="aws-warn" role="note">
          <AlertTriangle size={14} aria-hidden />
          <span>
            <strong>builder</strong> can create billable resources in this account. Changes apply to every sandbox on this
            proxy.
          </span>
        </p>
      ) : null}

      <SessionPolicySwitch on={spOn} editable={editable} onChange={setSessionPolicy} />

      {spOn ? (
        <>
          <div className="aws-toolbar">
            <SearchBox onChange={setQuery} />
            <Segmented<LevelFilter> label="Access level" value={level} options={LEVEL_OPTIONS} onChange={setLevel} />
            <SourceLine catalog={cat} now={now} loadingRole={catalog.stale ? shownRole : null} />
          </div>

          <div className="aws-grid">
            <nav className="aws-services" aria-label="AWS services">
              <h2 className="policy-h3 tabular">
                {searching ? `Matching services ${result ? `(${listServices.length})` : ''}` : `Services (${cat.services.length})`}
              </h2>
              {searching && !result && !searchError ? <SkeletonRows rows={5} /> : null}
              {searching && result && !listServices.length ? <p className="aws-empty">No service matches.</p> : null}
              <ServiceList items={listServices} counts={counts} active={panelId} onPick={pick} />
            </nav>

            <section className="aws-actions" aria-live="polite">
              {searching && !opened ? (
                <SearchResults
                  query={query.trim()}
                  role={shownRole}
                  level={lvl}
                  result={result}
                  error={searchError}
                  chosen={chosen}
                  editable={editable}
                  onToggle={toggle}
                />
              ) : panelId ? (
                <ServicePanel
                  id={panelId}
                  role={shownRole}
                  summary={cat.services.find((s) => s.id === panelId) ?? null}
                  level={lvl}
                  query={searching ? query.trim() : ''}
                  chosen={chosen}
                  editable={editable}
                  onToggle={toggle}
                  onClear={clearService}
                  onBack={searching ? () => setOpened(null) : undefined}
                />
              ) : null}
              {preview && preview.sessionPolicy ? (
                <div className="aws-enforce">
                  <h3 className="policy-h3">iron-proxy enforces it too</h3>
                  <dl className="aws-derived">
                    <div>
                      <dt>aws_auth allowed_services</dt>
                      <dd>
                        <HostList hosts={preview.allowedServices} />
                        {preview.allowedServices.join() === 'none' ? (
                          <span className="aws-none">No AWS service: iron-proxy refuses every AWS call.</span>
                        ) : null}
                      </dd>
                    </div>
                    <div>
                      <dt>AWS hosts on the allowlist</dt>
                      <dd>{preview.hosts.length ? <HostList hosts={preview.hosts} /> : 'none'}</dd>
                    </div>
                  </dl>
                </div>
              ) : null}
            </section>

            <section className="aws-preview" aria-labelledby="aws-preview-title">
              <div className="section-title-row">
                <h2 id="aws-preview-title" className="policy-h2">
                  Session policy
                </h2>
                <span className="policy-meta tabular">
                  {nSelected} {nSelected === 1 ? 'action' : 'actions'} · {nServices} {nServices === 1 ? 'service' : 'services'}
                </span>
              </div>
              <SizeMeter preview={preview && preview.sessionPolicy ? preview : null} limit={cat.policyChars} busy={previewing} />
              <SelectedChips
                actions={sorted(chosen)}
                role={shownRole}
                coverage={fresh && preview && preview.sessionPolicy ? preview.coverage : null}
                editable={editable}
                onRemove={(a) => toggle(a, false)}
              />
              {nSelected ? <ToneLegend role={shownRole} /> : null}
              {previewError ? (
                <p className="form-error" role="alert">
                  {previewError}
                </p>
              ) : (
                <PreviewTabs preview={preview && preview.sessionPolicy ? preview : null} baseline={cat.baseline} role={shownRole} stale={!fresh || previewing} />
              )}
              {applyControls}
            </section>
          </div>
        </>
      ) : (
        <section className="aws-open" aria-labelledby="aws-open-title">
          <h2 id="aws-open-title" className="policy-h2">
            Full {shownRole ? <span className="mono">{shownRole}</span> : 'role'} permissions
          </h2>
          <p className="aws-open-text">Sandboxes get the role’s full permissions. iron-proxy still refuses Secrets Manager.</p>
          <OpenSummary preview={preview} busy={previewing} error={previewError} />
          {applyControls}
          {preview && !preview.sessionPolicy ? <EffectiveTable preview={preview} role={shownRole} stale={!fresh || previewing} /> : null}
        </section>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- role and session policy

function RolePicker({
  roles,
  value,
  applied,
  editable,
  onPick,
}: {
  roles: AwsRole[];
  value: string | null;
  applied: string | null;
  editable: boolean;
  onPick: (id: string) => void;
}) {
  if (!roles.length) return null;
  return (
    <fieldset className="aws-roles">
      <legend className="policy-h3">Role sandbox AWS calls are signed as</legend>
      <div className="aws-role-grid" role="radiogroup" aria-label="Role">
        {roles.map((r) => {
          const checked = r.id === value;
          return (
            <label key={r.id} className={`aws-role${checked ? ' is-on' : ''}${editable ? '' : ' is-readonly'}`}>
              <input
                type="radio"
                name="aws-role"
                value={r.id}
                checked={checked}
                disabled={!editable}
                aria-label={`${r.id}${r.default ? ' (default)' : ''}: ${r.description}`}
                onChange={() => onPick(r.id)}
              />
              <span className="aws-role-head">
                <span className="aws-role-dot" aria-hidden />
                <span className="aws-role-id mono">{r.id}</span>
                {r.default ? <span className="tag">default</span> : null}
                {r.id === applied ? <span className="tag tag-quiet">in use</span> : null}
              </span>
              <span className="aws-role-desc">{r.description}</span>
            </label>
          );
        })}
      </div>
    </fieldset>
  );
}

function SessionPolicySwitch({ on, editable, onChange }: { on: boolean; editable: boolean; onChange: (on: boolean) => void }) {
  return (
    <div className="aws-switch-row">
      <button
        type="button"
        role="switch"
        id="aws-sp-switch"
        className={`switch${on ? ' is-on' : ''}`}
        aria-checked={on}
        aria-labelledby="aws-sp-label"
        aria-describedby="aws-sp-hint"
        disabled={!editable}
        onClick={() => onChange(!on)}
      >
        <span className="switch-knob" aria-hidden />
      </button>
      <span className="aws-switch-text">
        <span id="aws-sp-label" className="aws-switch-label" onClick={() => editable && onChange(!on)}>
          Narrow with a session policy
        </span>
        <span id="aws-sp-hint" className="aws-switch-hint">
          {on
            ? 'On: sandboxes can call only the actions picked below, within the role.'
            : 'Off: no session policy is sent, so sandboxes get everything the role allows.'}
        </span>
      </span>
    </div>
  );
}

/** With no session policy: what iron-proxy lets through, as counts, with the full lists behind a disclosure. */
function OpenSummary({ preview, busy, error }: { preview: AwsPreview | null; busy: boolean; error: string | null }) {
  if (error) {
    return (
      <p className="form-error" role="alert">
        {error}
      </p>
    );
  }
  if (!preview || preview.sessionPolicy) return <SkeletonRows rows={2} />;
  const nSvc = preview.allowedServices.length;
  const nHosts = preview.hosts.length;
  return (
    <div className={`aws-open-summary${busy ? ' is-busy' : ''}`}>
      <p className="tabular">
        iron-proxy lets through <strong>{nSvc.toLocaleString()}</strong> {nSvc === 1 ? 'service' : 'services'} and{' '}
        <strong>{nHosts.toLocaleString()}</strong> {nHosts === 1 ? 'host' : 'hosts'}: every AWS service the catalog can map
        and the role allows at least one action in, except Secrets Manager. Calls to any other service get iron-proxy’s
        403 before they reach AWS.
      </p>
      <details className="aws-open-details">
        <summary>List them</summary>
        <dl className="aws-derived">
          <div>
            <dt>aws_auth allowed_services ({nSvc.toLocaleString()})</dt>
            <dd>
              <HostList hosts={preview.allowedServices} />
            </dd>
          </div>
          <div>
            <dt>AWS hosts on the allowlist ({nHosts.toLocaleString()})</dt>
            <dd>
              <HostList hosts={preview.hosts} />
            </dd>
          </div>
        </dl>
      </details>
    </div>
  );
}

// ---------------------------------------------------------------- toolbar

/** Keeps the typed text local, so the long lists only re-render on the debounced value. */
function SearchBox({ onChange }: { onChange: (q: string) => void }) {
  const [value, setValue] = useState('');
  useEffect(() => {
    const t = window.setTimeout(() => onChange(value), 200);
    return () => window.clearTimeout(t);
  }, [value, onChange]);
  return (
    <div className="aws-search">
      <Search size={14} aria-hidden className="aws-search-icon" />
      <input
        type="search"
        className="input aws-search-input"
        aria-label="Search services and actions"
        placeholder="Search services and actions, e.g. GetObject, dynamodb, Describe"
        value={value}
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape' && value) {
            e.preventDefault();
            setValue('');
            onChange('');
          }
        }}
      />
      {value ? (
        <button
          type="button"
          className="aws-search-clear"
          aria-label="Clear search"
          onClick={() => {
            setValue('');
            onChange('');
          }}
        >
          <X size={13} aria-hidden />
        </button>
      ) : null}
    </div>
  );
}

function SourceLine({ catalog, now, loadingRole }: { catalog: AwsCatalog; now: number; loadingRole: string | null }) {
  const { source, ceiling } = catalog;
  const stale = source.stale || ceiling.stale;
  const fetched = source.fetchedAt ? fmtAgo(source.fetchedAt, now) : null;
  const policies = ceiling.policies.length ? ceiling.policies.join(', ') : null;
  return (
    <p className={`aws-source tabular${stale ? ' is-stale' : ''}`} title={source.url ?? undefined}>
      <span>
        Service Authorization Reference
        {source.services !== null ? ` · ${source.services.toLocaleString()} services` : ''}
        {source.actions !== null ? ` · ${source.actions.toLocaleString()} actions` : ''}
        {fetched ? ` · fetched ${fetched}` : ''}
      </span>
      <span title={policies ?? undefined}>
        {loadingRole ? (
          `evaluating for ${loadingRole}…`
        ) : ceiling.role ? (
          <>
            evaluated for <span className="mono">{ceiling.role}</span>
            {policies ? ` (${policies})` : ''}
          </>
        ) : (
          <>
            ceiling {ceiling.policy ?? 'ReadOnlyAccess'}
            {ceiling.version ? ` ${ceiling.version}` : ''}
          </>
        )}
      </span>
      {stale ? (
        <span role="status">
          Stale: the last refresh failed{source.error || ceiling.error ? ` (${source.error ?? ceiling.error})` : ''}; showing the
          cached copy.
        </span>
      ) : null}
    </p>
  );
}

// ---------------------------------------------------------------- services

interface ListItem {
  id: string;
  name: string;
  enabled: boolean;
  reason?: string | null;
  note: string;
  noteLabel: string;
}

function summaryItem(s: AwsServiceSummary, lvl: AccessLevel | null): ListItem {
  const n = lvl ? (s.levels[lvl] ?? 0) : s.actions;
  return {
    id: s.id,
    name: s.name,
    enabled: s.enabled,
    reason: s.reason,
    note: lvl ? String(n) : `${s.allowed}/${s.actions}`,
    noteLabel: lvl ? `${n} ${lvl} actions` : `${s.allowed} of ${s.actions} actions effective with the role`,
  };
}

const ServiceList = memo(function ServiceList({
  items,
  counts,
  active,
  onPick,
}: {
  items: ListItem[];
  counts: Record<string, number>;
  active: string | null;
  onPick: (id: string) => void;
}) {
  return (
    <div className="aws-svc-scroll">
      <ul className="policy-rows" role="list">
        {items.map((s) => {
          const n = counts[s.id] ?? 0;
          return (
            <li key={s.id}>
              <button
                type="button"
                className={`aws-svc${s.id === active ? ' is-active' : ''}${s.enabled ? '' : ' is-disabled'}`}
                aria-current={s.id === active ? 'true' : undefined}
                title={s.enabled ? `${s.name} (${s.id})` : `${s.name}: ${s.reason ?? 'can’t be enabled here'}`}
                onClick={() => onPick(s.id)}
              >
                <span className="aws-svc-label">{s.name}</span>
                {n ? (
                  <span className="aws-svc-count tabular is-on" aria-label={`${n} chosen`}>
                    {n} chosen
                  </span>
                ) : (
                  <span className="aws-svc-count tabular" aria-label={s.enabled ? s.noteLabel : 'no endpoint mapping'}>
                    {s.enabled ? s.note : 'no mapping'}
                  </span>
                )}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
});

function ServicePanel({
  id,
  role,
  summary,
  level,
  query,
  chosen,
  editable,
  onToggle,
  onClear,
  onBack,
}: {
  id: string;
  role: string | null;
  summary: AwsServiceSummary | null;
  level: AccessLevel | null;
  query: string;
  chosen: Set<string>;
  editable: boolean;
  onToggle: (action: string, on: boolean) => void;
  onClear: (id: string) => void;
  onBack?: () => void;
}) {
  const { data, error, loading } = useServiceDetail(id, role);
  const name = data?.name ?? summary?.name ?? id;
  const q = query.toLowerCase();
  const filterByQuery = q && !id.toLowerCase().includes(q) && !name.toLowerCase().includes(q);

  const groups = useMemo(() => {
    if (!data) return null;
    const rows = data.actions.filter(
      (a) => (!level || a.level === level) && (!filterByQuery || a.action.toLowerCase().includes(q)),
    );
    const ok = rows.filter((a) => a.allowed);
    // Outside the role: pickable, no effect with it. Blocked or unmapped: not pickable.
    const noEffect = new Map<string, AwsAction[]>();
    const blocked = new Map<string, AwsAction[]>();
    for (const a of rows) {
      if (a.allowed) continue;
      const into = a.pickable ? noEffect : blocked;
      const k = a.reason ?? 'not available here';
      into.set(k, [...(into.get(k) ?? []), a]);
    }
    const nNoEffect = [...noEffect.values()].reduce((n, xs) => n + xs.length, 0);
    return { ok, noEffect: [...noEffect.entries()], nNoEffect, blocked: [...blocked.entries()], total: rows.length };
  }, [data, level, filterByQuery, q]);

  const nChosen = [...chosen].filter((a) => prefixOf(a) === id).length;

  return (
    <>
      <div className="section-title-row">
        <h2 id="aws-actions-title" className="policy-h2">
          {name} <span className="policy-meta mono">{id}</span>
          {data?.signingNames.length && data.signingNames.join() !== id ? (
            <span className="policy-meta"> signs as <span className="mono">{data.signingNames.join(', ')}</span></span>
          ) : null}
        </h2>
        <span className="aws-bulk">
          {onBack ? (
            <Button size="sm" variant="ghost" onClick={onBack}>
              All matches
            </Button>
          ) : null}
          {editable ? (
            <Button size="sm" variant="ghost" onClick={() => onClear(id)} disabled={!nChosen}>
              Clear service
            </Button>
          ) : null}
        </span>
      </div>
      {data && !data.enabled ? <Note>{`${capitalize(data.reason ?? 'can’t be enabled here (no endpoint mapping)')}. Its actions are listed for reference.`}</Note> : null}
      {error ? (
        <Empty
          title="Couldn’t load this service"
          action={
            <Button size="sm" icon={<RefreshCw size={14} aria-hidden />} onClick={() => retryDetail(id, role)}>
              Retry
            </Button>
          }
        >
          {error}
        </Empty>
      ) : loading && !data ? (
        <SkeletonRows rows={8} />
      ) : groups ? (
        <>
          {!level && !filterByQuery && data?.wildcards.length ? (
            <>
              <ul className="aws-checks aws-wild" role="list" aria-label={`${name} wildcards`}>
                {data.wildcards.map((w) => (
                  <WildcardRow key={w.action} w={w} checked={chosen.has(w.action)} editable={editable} onToggle={onToggle} />
                ))}
              </ul>
              <ToneLegend role={role} />
            </>
          ) : null}
          <p className="aws-group-head tabular">
            {groups.ok.length.toLocaleString()} effective with {role ? <span className="mono">{role}</span> : 'the role'}
            {groups.nNoEffect ? ` · ${groups.nNoEffect.toLocaleString()} no effect` : ''}
            {groups.total - groups.ok.length - groups.nNoEffect ? ` · ${(groups.total - groups.ok.length - groups.nNoEffect).toLocaleString()} not available` : ''}
            {level ? ` · ${level} only` : ''}
            {filterByQuery ? ` · matching “${query}”` : ''}
          </p>
          {groups.ok.length ? (
            <ul className="aws-checks" role="list" aria-label={`${name} actions effective with the role`}>
              {groups.ok.map((a) => (
                <ActionRow key={a.action} a={a} role={role} label={a.name} checked={chosen.has(a.action)} editable={editable} onToggle={onToggle} />
              ))}
            </ul>
          ) : null}
          {groups.noEffect.map(([reason, rows]) => (
            <div key={reason} className="aws-noeffect">
              <p className="aws-group-head is-blocked tabular" title={reason}>
                {rows.length.toLocaleString()} no effect with {role ? <span className="mono">{role}</span> : 'the role'} ({shortReason(reason)}): can
                still be picked
              </p>
              <ul className="aws-checks" role="list" aria-label={`${name}: no effect with the role`}>
                {rows.map((a) => (
                  <ActionRow key={a.action} a={a} role={role} label={a.name} checked={chosen.has(a.action)} editable={editable} onToggle={onToggle} />
                ))}
              </ul>
            </div>
          ))}
          {groups.blocked.map(([reason, rows]) => (
            <div key={reason} className="aws-blocked">
              <p className="aws-group-head is-blocked tabular">
                {rows.length.toLocaleString()} not available: {reason}
              </p>
              <ul className="aws-checks" role="list" aria-label={`${name}: ${reason}`}>
                {rows.map((a) => (
                  <ActionRow key={a.action} a={a} role={role} label={a.name} checked={chosen.has(a.action)} editable={editable} onToggle={onToggle} />
                ))}
              </ul>
            </div>
          ))}
          {data?.hosts.length ? (
            <p className="aws-hosts">
              <span className="aws-hosts-label">Endpoints allowed with it</span>
              <HostList hosts={data.hosts} />
            </p>
          ) : null}
        </>
      ) : null}
    </>
  );
}

function SearchResults({
  query,
  role,
  level,
  result,
  error,
  chosen,
  editable,
  onToggle,
}: {
  query: string;
  role: string | null;
  level: AccessLevel | null;
  result: AwsSearchResult | null;
  error: string | null;
  chosen: Set<string>;
  editable: boolean;
  onToggle: (action: string, on: boolean) => void;
}) {
  if (error) return <p className="form-error" role="alert">{error}</p>;
  if (!result) return <SkeletonRows rows={8} />;
  return (
    <>
      <div className="section-title-row">
        <h2 id="aws-actions-title" className="policy-h2 tabular">
          {result.total.toLocaleString()} {result.total === 1 ? 'action matches' : 'actions match'} “{query}”
          {level ? <span className="policy-meta"> · {level}</span> : null}
        </h2>
      </div>
      {result.truncated ? (
        <p className="aws-group-head tabular">
          Showing {result.actions.length.toLocaleString()} of {result.total.toLocaleString()}: refine your search to see the rest.
        </p>
      ) : null}
      {result.actions.length ? (
        <ul className="aws-checks" role="list" aria-label="Matching actions">
          {result.actions.map((a) => (
            <ActionRow key={a.action} a={a} role={role} label={a.action} checked={chosen.has(a.action)} editable={editable} onToggle={onToggle} inline />
          ))}
        </ul>
      ) : (
        <p className="aws-empty">No action matches. Try part of a name, like “List” or “Bucket”.</p>
      )}
    </>
  );
}

// ---------------------------------------------------------------- rows

const ActionRow = memo(function ActionRow({
  a,
  role,
  label,
  checked,
  editable,
  onToggle,
  inline,
}: {
  a: AwsAction;
  role: string | null;
  label: string;
  checked: boolean;
  editable: boolean;
  onToggle: (action: string, on: boolean) => void;
  /** Show the short reason on the row itself (search results have no group headings). */
  inline?: boolean;
}) {
  // Not pickable (blocked, unmapped): disabled. Outside the role: pickable, marked "no effect".
  const off = !a.pickable;
  const noEffect = a.pickable && !a.allowed;
  const short = off && inline ? shortReason(a.reason) : null;
  return (
    <li>
      <label
        className={`aws-check${checked ? ' is-on' : ''}${off ? ' is-off' : ''}${noEffect ? ' is-noeffect' : ''}`}
        title={off || noEffect ? (a.reason ?? undefined) : undefined}
      >
        <input
          type="checkbox"
          checked={checked}
          // A chosen action the server no longer allows can still be unticked.
          disabled={!editable || (off && !checked)}
          aria-label={`${a.action} (${a.level})${off ? `: ${a.reason ?? 'not available'}` : noEffect ? `: no effect with ${role ?? 'the role'}` : a.limited ? ', limited' : ''}`}
          onChange={(e) => onToggle(a.action, e.target.checked)}
        />
        <span className="aws-check-action mono" title={a.action}>
          {label}
        </span>
        {short ? (
          <span className="aws-check-why" title={a.reason ?? undefined}>
            {short}
          </span>
        ) : null}
        {noEffect ? (
          <span className="aws-check-why" title={a.reason ?? undefined}>
            no effect with {role ?? 'the role'}
          </span>
        ) : null}
        {a.limited && !off ? (
          <span className="aws-limited" title="allowed only on specific resources or conditions">
            limited
          </span>
        ) : null}
        <span className={`lvl lvl-${a.level.toLowerCase()}`}>{a.level}</span>
      </label>
    </li>
  );
});

function WildcardRow({
  w,
  checked,
  editable,
  onToggle,
}: {
  w: AwsWildcard;
  checked: boolean;
  editable: boolean;
  onToggle: (action: string, on: boolean) => void;
}) {
  const off = !w.pickable;
  const count = `${w.effective} of ${w.covers} effective`;
  return (
    <li>
      <label className={`aws-check aws-wild-row${checked ? ' is-on' : ''}${off ? ' is-off' : ''}`} title={w.reason ?? undefined}>
        <input
          type="checkbox"
          checked={checked}
          disabled={!editable || (off && !checked)}
          aria-label={`${w.action} (wildcard, ${w.covers} actions)${off ? `: ${w.reason ?? 'not available'}` : `: ${count}`}`}
          onChange={(e) => onToggle(w.action, e.target.checked)}
        />
        <span className="aws-wild-text">
          <span className="aws-check-label">{w.label}</span>
          {/* The action names stay in the tooltip; the row keeps the counts */}
          {w.reason ? <span className="aws-wild-why">{w.reason.replace(/ \([^)]*\)/g, '')}</span> : null}
        </span>
        <span className="aws-check-action mono" title={w.action}>
          {w.action}
        </span>
        {off ? (
          <span className="lvl tabular">covers {w.covers}</span>
        ) : (
          <span className="lvl lvl-cov tabular" title={w.reason ?? `${count}${w.limited ? ', some limited' : ''}`}>
            <ToneDot tone={w.tone} title={w.reason ?? count} />
            {w.effective}/{w.covers}
          </span>
        )}
      </label>
    </li>
  );
}

function SelectedChips({
  actions,
  role,
  coverage,
  editable,
  onRemove,
}: {
  actions: string[];
  role: string | null;
  /** The preview's coverage per pick (null while it loads, or with no session policy) */
  coverage: AwsCoverage[] | null;
  editable: boolean;
  onRemove: (a: string) => void;
}) {
  const byAction = new Map((coverage ?? []).map((c) => [c.action, c]));
  if (!actions.length) {
    return <p className="aws-chips-empty">Nothing selected: iron-proxy will refuse every AWS call.</p>;
  }
  return (
    <div className="aws-chips-wrap">
      <p className="aws-hosts-label tabular">Selected ({actions.length})</p>
      <ul className="aws-chips" role="list" aria-label="Selected actions">
        {actions.map((a) => {
          const k = knownAction(a, role);
          // Can't be picked any more (blocked, unmapped): red. Otherwise the tone of its coverage.
          const bad = k !== null && !k.pickable;
          const c = byAction.get(a);
          const title = bad ? (k?.reason ?? undefined) : (c?.reason ?? (c ? `${c.effective} of ${c.covers} effective` : undefined));
          return (
            <li key={a} className={`aws-chip${bad ? ' is-bad' : ''}${c && c.tone === 'none' ? ' is-none' : ''}`} title={title}>
              {c && !bad ? <ToneDot tone={c.tone} title={title} /> : null}
              <span className="mono">{a}</span>
              {c && !bad && c.kind === 'wildcard' ? <span className="aws-chip-count tabular">{c.effective}/{c.covers}</span> : null}
              {c && !bad && c.kind === 'action' && c.tone === 'none' ? <span className="aws-chip-why">no effect</span> : null}
              {bad ? <span className="aws-chip-why">{shortReason(k?.reason ?? null)}</span> : null}
              {editable ? (
                <button type="button" aria-label={`Remove ${a}`} onClick={() => onRemove(a)}>
                  <X size={12} aria-hidden />
                </button>
              ) : null}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------- session policy / effective access

type PreviewTab = 'policy' | 'effective';

function PreviewTabs({
  preview,
  baseline,
  role,
  stale,
}: {
  preview: AwsPreview | null;
  baseline: Record<string, unknown> | null;
  role: string | null;
  stale: boolean;
}) {
  const [tab, setTab] = useState<PreviewTab>('policy');
  const tabs: { id: PreviewTab; label: string }[] = [
    { id: 'policy', label: 'Session policy' },
    { id: 'effective', label: 'Effective access' },
  ];
  function onKey(e: React.KeyboardEvent) {
    if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
    e.preventDefault();
    const next: PreviewTab = tab === 'policy' ? 'effective' : 'policy';
    setTab(next);
    document.getElementById(`aws-ptab-${next}`)?.focus();
  }
  return (
    <div className="aws-ptabs">
      <div className="tabs aws-ptablist" role="tablist" aria-label="Session policy or effective access" onKeyDown={onKey}>
        {tabs.map((t) => (
          <button
            key={t.id}
            id={`aws-ptab-${t.id}`}
            type="button"
            role="tab"
            className="tab"
            aria-selected={tab === t.id}
            aria-controls={`aws-ppanel-${t.id}`}
            tabIndex={tab === t.id ? 0 : -1}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`aws-ppanel-${tab}`} aria-labelledby={`aws-ptab-${tab}`}>
        {tab === 'policy' ? (
          <PolicyJson preview={preview} baseline={baseline} />
        ) : preview ? (
          <EffectiveTable preview={preview} role={role} stale={stale} />
        ) : (
          <SkeletonRows rows={4} />
        )}
      </div>
    </div>
  );
}

/**
 * Effective access per service: the role's own permissions intersected with the session policy (or the role alone,
 * with no session policy). Computed by the backend from the role's policies; a row opens to the action names.
 */
function EffectiveTable({ preview, role, stale }: { preview: AwsPreview; role: string | null; stale: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  const rows = preview.effective;
  const sp = preview.sessionPolicy;
  const nEff = rows.reduce((n, r) => n + r.effective, 0);
  const nLive = rows.filter((r) => r.effective > 0).length;
  // The table is for the preview's role; while a switched role's preview loads, say so and dim it
  const shown = preview.role ?? role;
  const roleEl = shown ? <span className="mono">{shown}</span> : 'the role';
  return (
    <div className={`aws-effective${stale ? ' is-busy' : ''}`} aria-busy={stale || undefined}>
      <h3 className="aws-eff-title">
        {sp ? (
          <>
            Role {roleEl} ∩ session policy = effective
          </>
        ) : (
          <>Role {roleEl}, no session policy: everything it allows</>
        )}
      </h3>
      <p className="aws-eff-note tabular">
        {stale && role && shown !== role ? `Evaluating for ${role}… ` : ''}
        {nEff.toLocaleString()} {nEff === 1 ? 'action' : 'actions'} in {nLive} {nLive === 1 ? 'service' : 'services'}. Computed
        here from the role’s policies: an action counts when an Allow matches it and no unconditional Deny does; “limited”
        when every matching Allow is tied to a resource or a condition. AWS has the last word.
      </p>
      {rows.length ? (
        <div className={`table-scroll aws-eff-scroll${sp ? '' : ' is-tall'}`}>
          <table className="table aws-eff-table">
            <thead>
              <tr>
                <th scope="col">Service</th>
                <th scope="col" className="num">
                  Effective
                </th>
                <th scope="col" className="num">
                  Asked
                </th>
                <th scope="col" className="num">
                  Role allows
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <EffectiveRow key={r.service} r={r} role={role} open={open === r.service} onToggle={() => setOpen(open === r.service ? null : r.service)} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="aws-empty">Nothing picked: no AWS service gets through.</p>
      )}
    </div>
  );
}

function EffectiveRow({ r, role, open, onToggle }: { r: AwsEffectiveRow; role: string | null; open: boolean; onToggle: () => void }) {
  const tone: Tone = r.asked === null ? 'all' : r.effective === 0 ? 'none' : r.effective === r.asked ? 'all' : 'partial';
  return (
    <>
      <tr className={`aws-eff-row${r.noEffect ? ' is-none' : ''}`}>
        <th scope="row">
          <button type="button" className="aws-eff-toggle" aria-expanded={open} onClick={onToggle} title={r.reason ?? undefined}>
            <ToneDot tone={tone} title={r.reason ?? undefined} />
            <span className="aws-eff-name">{r.name}</span> <span className="mono aws-eff-id">{r.service}</span>
            {r.limited ? <span className="aws-limited">{r.limited} limited</span> : null}
          </button>
          {r.noEffect ? (
            <span className="aws-eff-why" title={r.reason ?? undefined}>
              no effect with {role ?? 'the role'}: iron-proxy answers its calls with a bare 403
            </span>
          ) : null}
        </th>
        <td className="num tabular">{r.effective.toLocaleString()}</td>
        <td className="num tabular">{r.asked === null ? '–' : r.asked.toLocaleString()}</td>
        <td className="num tabular">
          {r.roleAllows.toLocaleString()}
          <span className="aws-eff-of"> / {r.total.toLocaleString()}</span>
        </td>
      </tr>
      {open ? (
        <tr className="aws-eff-detail">
          <td colSpan={4}>{r.actions ? <ActionNames names={r.actions} limited={r.limitedActions ?? []} /> : <RoleActions id={r.service} role={role} />}</td>
        </tr>
      ) : null}
    </>
  );
}

function ActionNames({ names, limited }: { names: string[]; limited: string[] }) {
  if (!names.length) return <p className="aws-empty">None of the picked actions is allowed by the role.</p>;
  const lim = new Set(limited);
  return (
    <ul className="aws-eff-names" role="list">
      {names.map((n) => (
        <li key={n}>
          <span className="mono">{n}</span>
          {lim.has(n) ? (
            <span className="aws-limited" title="allowed only on specific resources or conditions">
              limited
            </span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

/** With no session policy: the role's own allowed actions in one service, from the service's detail. */
function RoleActions({ id, role }: { id: string; role: string | null }) {
  const { data, error } = useServiceDetail(id, role);
  if (error) return <p className="form-error">{error}</p>;
  if (!data) return <SkeletonRows rows={3} />;
  const ok = data.actions.filter((a) => a.allowed);
  return <ActionNames names={ok.map((a) => a.action)} limited={ok.filter((a) => a.limited).map((a) => a.action)} />;
}

function HostList({ hosts }: { hosts: string[] }) {
  return (
    <span className="host-list mono">
      {hosts.map((h) => (
        <span key={h}>{h}</span>
      ))}
    </span>
  );
}

/** A small info button whose text shows on hover or keyboard focus and is read as its description. */
function InfoTip({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <span className="info-tip">
      <button type="button" className="info-tip-btn" aria-label={label} aria-describedby={id}>
        <Info size={13} aria-hidden />
      </button>
      <span role="tooltip" id={id} className="info-tip-text">
        {children}
      </span>
    </span>
  );
}

function SizeMeter({ preview, limit, busy }: { preview: AwsPreview | null; limit: number; busy: boolean }) {
  const chars = preview?.chars ?? 0;
  const max = preview?.limit ?? limit;
  const over = chars > max;
  const pct = Math.min(100, (chars / Math.max(1, max)) * 100);
  return (
    <div className={`size-meter${over ? ' is-over' : ''}${busy ? ' is-busy' : ''}`}>
      <div className="size-meter-head">
        <span className="size-meter-label">
          Size, minified
          <InfoTip id="size-meter-tip" label="About this limit">
            STS limit on the session policy’s plaintext. The returned session token has its own size limit; policies
            and session tags count toward it (shown after Apply).
          </InfoTip>
        </span>
        <span className="tabular">
          {preview ? chars.toLocaleString() : '–'} / {max.toLocaleString()} characters
        </span>
      </div>
      <div
        className="size-meter-track"
        role="meter"
        aria-label="Session policy size"
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={chars}
        aria-valuetext={`${chars} of ${max} characters${over ? ', over the STS limit' : ''}`}
      >
        <span style={{ width: `${pct}%` }} />
      </div>
      {over ? <p className="field-error">Over the STS limit for a session policy: drop some actions or pick a wildcard.</p> : null}
    </div>
  );
}

/**
 * The session policy, pretty-printed, with the proxy's fixed baseline statement set apart. The baseline is
 * folded to one line by default: it never changes, and its secret ARNs are long (and name the account).
 */
function PolicyJson({ preview, baseline }: { preview: AwsPreview | null; baseline: Record<string, unknown> | null }) {
  const [open, setOpen] = useState(false);
  if (!preview?.policy) return <pre className="policy-json is-empty">{preview ? preview.policyJson : '…'}</pre>;
  const policy = preview.policy;
  const stmts = Array.isArray(policy.Statement) ? (policy.Statement as Record<string, unknown>[]) : [];
  const base = baseline ? JSON.stringify(baseline) : null;
  const isBase = (s: Record<string, unknown>) =>
    (base !== null && JSON.stringify(s) === base) || (baseline?.Sid !== undefined && s.Sid === baseline.Sid);
  const indent = (text: string, n: number) =>
    text
      .split('\n')
      .map((l) => ' '.repeat(n) + l)
      .join('\n');
  const folded = (s: Record<string, unknown>) => {
    const res = Array.isArray(s.Resource) ? `[${s.Resource.length} secret ARNs]` : JSON.stringify(s.Resource ?? '');
    const first = [s.Sid !== undefined ? `"Sid": ${JSON.stringify(s.Sid)}` : null, `"Effect": ${JSON.stringify(s.Effect ?? 'Allow')}`]
      .filter(Boolean)
      .join(', ');
    return `    { ${first},\n      "Action": ${JSON.stringify(s.Action)},\n      "Resource": ${res} }`;
  };
  const head = Object.entries(policy)
    .filter(([k]) => k !== 'Statement')
    .map(([k, v]) => `  ${JSON.stringify(k)}: ${JSON.stringify(v)},`)
    .join('\n');
  const hasBase = stmts.some(isBase);
  return (
    <div className="policy-json-wrap">
      <pre className="policy-json" tabIndex={0} aria-label="Session policy JSON">
        {`{\n${head}\n  "Statement": [\n`}
        {stmts.map((s, i) => (
          <span key={i} className={isBase(s) ? 'json-fixed' : 'json-stmt'}>
            {isBase(s) && !open ? folded(s) : indent(JSON.stringify(s, null, 2), 4)}
            {i < stmts.length - 1 ? ',' : ''}
            {'\n'}
          </span>
        ))}
        {'  ]\n}'}
      </pre>
      {hasBase ? (
        <p className="json-legend">
          <span className="json-legend-mark" aria-hidden />
          <span>
            Fixed: lets iron-proxy read the secrets it injects. Always included; Secrets Manager is never an allowed
            service, so sandboxes can’t use it.{' '}
            <button type="button" className="link-button" aria-expanded={open} onClick={() => setOpen(!open)}>
              {open ? 'Fold it' : 'Show it in full'}
            </button>
          </span>
        </p>
      ) : null}
    </div>
  );
}
