import { useEffect, useId, useRef, useState } from 'react';
import { KeyRound, Plus, RefreshCw, RotateCcw, ShieldOff, Trash2 } from 'lucide-react';
import { api } from '../../lib/api';
import { fmtAgo } from '../../lib/format';
import { useNow, usePolling } from '../../lib/hooks';
import { noteWrite, usePolicy, usePollS } from '../../lib/policy';
import { toast } from '../../lib/store';
import type { PolicyCredential } from '../../lib/types';
import { Button, ConfirmDialog, Empty, KeyFob, SkeletonRows } from '../ui/primitives';
import { hostProblem } from './AllowlistSection';

type Action = 'revoke' | 'restore' | 'rotate' | 'delete';

const NAME_RE = /^[a-z][a-z0-9-]{1,31}$/;
const ENV_RE = /^[A-Z_][A-Z0-9_]{0,63}$/;
const HEADER_RE = /^[A-Za-z0-9!#$%&'*+.^_`|~-]+$/;
const MAX_VALUE = 8192;

const STATUS_LABEL: Record<PolicyCredential['status'], string> = {
  active: 'Active',
  revoking: 'Revoking…',
  revoked: 'Revoked',
  restoring: 'Restoring…',
  unknown: 'Policy unknown',
};
const STATUS_TONE: Record<PolicyCredential['status'], string> = {
  active: 'tone-run',
  revoking: 'tone-sleep',
  revoked: 'tone-deny',
  restoring: 'tone-sleep',
  unknown: 'tone-off',
};

function splitList(v: string): string[] {
  return v
    .split(/[\s,]+/)
    .map((x) => x.trim())
    .filter(Boolean);
}

function looksIn(c: PolicyCredential): string {
  const parts = [c.headers.length ? `header ${c.headers.join(', ')}` : 'every header'];
  if (c.query) parts.push('query');
  if (c.body) parts.push('body');
  return parts.join(' · ');
}

export function CredentialsSection({ editable }: { editable: boolean }) {
  const tick = usePolicy((s) => s.tick);
  const pollS = usePollS();
  // A revoke or restore settles at the sidecar's next check (up to ~30 s): poll every 5 s meanwhile, and at once
  // when the sidecar reports a new config applied
  const appliedVersion = usePolicy((s) => s.status?.config.version ?? null);
  const [fast, setFast] = useState(false);
  const creds = usePolling(api.policyCredentials, fast ? 5_000 : 20_000, true, 'policy-credentials');
  const pending = creds.data?.some((c) => c.status === 'revoking' || c.status === 'restoring') ?? false;
  useEffect(() => setFast(pending), [pending]);
  const refresh = creds.refresh;
  useEffect(() => {
    if (appliedVersion !== null && pending) void refresh();
  }, [appliedVersion]);
  useEffect(() => {
    if (tick) void refresh();
  }, [tick, refresh]);
  const now = useNow();

  const [dialog, setDialog] = useState<{ cred: PolicyCredential; action: Action } | null>(null);
  const [busy, setBusy] = useState(false);
  const [rotateError, setRotateError] = useState<string | null>(null);
  const rotateRef = useRef<HTMLInputElement>(null);

  async function run() {
    if (!dialog) return;
    const { cred, action } = dialog;
    let value = '';
    if (action === 'rotate') {
      value = rotateRef.current?.value ?? '';
      if (!value) {
        setRotateError('Enter the new value.');
        rotateRef.current?.focus();
        return;
      }
      if (value.length > MAX_VALUE) {
        setRotateError(`At most ${MAX_VALUE.toLocaleString()} characters.`);
        return;
      }
    }
    setBusy(true);
    try {
      if (action === 'rotate') {
        const r = await api.rotateCredential(cred.name, value);
        if (rotateRef.current) rotateRef.current.value = '';
        noteWrite(r);
        toast('success', `Rotated ${cred.name}`, 'iron-proxy picks up the new value within 30 s. No reload needed.');
      } else if (action === 'delete') {
        noteWrite(await api.deleteCredential(cred.name));
        toast('success', `Deleted ${cred.name}`, 'Out of the proxy config; the secret is scheduled for deletion in 7 days.');
      } else if (action === 'revoke') {
        noteWrite(await api.revokeCredential(cred.name));
        toast('success', `Revoking ${cred.name}`, `iron-proxy reloads without it at its next check (within about ${pollS} s), for every sandbox. Until then the key still works.`);
      } else {
        noteWrite(await api.restoreCredential(cred.name));
        toast('success', `Restoring ${cred.name}`, `iron-proxy reloads and fetches it again at its next check (within about ${pollS} s).`);
      }
      setDialog(null);
      setRotateError(null);
      await refresh();
    } catch (e) {
      if (rotateRef.current) rotateRef.current.value = '';
      const msg = e instanceof Error ? e.message : String(e);
      if (action === 'rotate') setRotateError(msg);
      else toast('error', `Could not ${action} ${cred.name}`, msg);
    } finally {
      setBusy(false);
    }
  }

  const d = dialog;
  return (
    <div className="policy-grid">
      <div className="policy-main">
        <div className="section-title-row">
          <h2 className="policy-h2">
            Credentials <span className="policy-count tabular">{creds.data ? creds.data.length : ''}</span>
          </h2>
          <span className="policy-meta">placeholders in sandboxes, real values only in Secrets Manager</span>
        </div>
        {creds.data === null && creds.loading ? <SkeletonRows rows={4} height="76px" /> : null}
        {creds.data === null && creds.error ? (
          <Empty
            title="Couldn’t load credentials"
            action={
              <Button size="sm" icon={<RefreshCw size={14} aria-hidden />} onClick={() => void refresh()}>
                Retry
              </Button>
            }
          >
            {creds.error.message}
          </Empty>
        ) : null}
        <ul className="cred-list" role="list">
          {creds.data?.map((c) => (
            <CredRow key={c.name} c={c} now={now} editable={editable} onAction={(action) => setDialog({ cred: c, action })} />
          ))}
        </ul>
      </div>

      <aside className="policy-side" aria-label="Add a credential">
        <AddCredentialForm editable={editable} onAdded={() => void refresh()} />
      </aside>

      <ConfirmDialog
        open={d !== null}
        title={
          !d
            ? ''
            : d.action === 'rotate'
              ? `Rotate ${d.cred.name}`
              : d.action === 'delete'
                ? `Delete ${d.cred.name}?`
                : d.action === 'revoke'
                  ? `Revoke ${d.cred.name} for every sandbox?`
                  : `Restore ${d.cred.name} for every sandbox?`
        }
        confirmLabel={
          !d ? '' : d.action === 'rotate' ? 'Rotate' : d.action === 'delete' ? 'Delete credential' : d.action === 'revoke' ? 'Revoke for all sandboxes' : 'Restore for all sandboxes'
        }
        tone={d?.action === 'revoke' || d?.action === 'delete' ? 'danger' : 'primary'}
        busy={busy}
        onConfirm={() => void run()}
        onClose={() => {
          if (busy) return;
          if (rotateRef.current) rotateRef.current.value = '';
          setRotateError(null);
          setDialog(null);
        }}
      >
        {d?.action === 'rotate' ? (
          <form
            className="dialog-form"
            onSubmit={(e) => {
              e.preventDefault();
              void run();
            }}
          >
            <p>
              The new value goes straight into Secrets Manager. It isn’t shown again. iron-proxy picks it up within{' '}
              <strong>30 s</strong> (its cache ttl), with no reload.
            </p>
            <div className="field">
              <label htmlFor="rotate-value">New value</label>
              <input
                ref={rotateRef}
                id="rotate-value"
                type="password"
                className="input"
                autoComplete="off"
                spellCheck={false}
                aria-invalid={Boolean(rotateError)}
                aria-describedby="rotate-err"
                data-1p-ignore
                data-lpignore="true"
              />
              <p id="rotate-err" className="field-error" role="alert" hidden={!rotateError}>
                {rotateError}
              </p>
            </div>
          </form>
        ) : d?.action === 'delete' ? (
          <>
            <p>
              <strong>{d.cred.name}</strong> leaves the proxy config at once, for every sandbox. Sandboxes keep their
              placeholder in <code>{d.cred.env}</code>, which then goes out unswapped.
            </p>
            <p className="dialog-faint">The secret is scheduled for deletion with a 7-day recovery window.</p>
          </>
        ) : d?.action === 'revoke' ? (
          <>
            <p>
              iron-proxy is denied the <strong>{d.cred.name}</strong> secret and reloads its config at its next check
              for changes, which drops its cached copy. It takes up to about {pollS} s, with no restart; until then the
              key is still swapped in.
            </p>
            <p>
              It affects every sandbox on this proxy, including ones you don’t own. Their placeholder goes out unswapped
              and the upstream rejects it until you restore the key.
            </p>
            <p className="dialog-faint">The secret itself is not deleted.</p>
          </>
        ) : d ? (
          <>
            <p>
              iron-proxy gets read access to the <strong>{d.cred.name}</strong> secret again and reloads, for every
              sandbox on this proxy.
            </p>
            <p className="dialog-faint">It takes up to about {pollS} s: the proxy checks for changes every {pollS} s.</p>
          </>
        ) : null}
      </ConfirmDialog>
    </div>
  );
}

function CredRow({
  c,
  now,
  editable,
  onAction,
}: {
  c: PolicyCredential;
  now: number;
  editable: boolean;
  onAction: (a: Action) => void;
}) {
  const revoked = c.status === 'revoked' || c.status === 'revoking';
  const pending = c.status === 'revoking' || c.status === 'restoring';
  return (
    <li className={`cred-row${c.status === 'revoked' ? ' is-revoked' : ''}`}>
      <div className="cred-top">
        <KeyFob name={c.name} size="md" empty={c.status === 'revoked'} />
        <span className="tag">{c.kind === 'custom' ? 'custom' : 'built-in'}</span>
        <span className={`key-state ${STATUS_TONE[c.status]}`} title={c.statusDetail ?? undefined}>
          {STATUS_LABEL[c.status]}
        </span>
        <span className="filter-spacer" />
        {editable ? (
          <span className="cred-actions">
            {c.rotatable ? (
              <Button size="sm" icon={<KeyRound size={14} aria-hidden />} onClick={() => onAction('rotate')} disabled={pending}>
                Rotate
              </Button>
            ) : null}
            {revoked ? (
              <Button size="sm" icon={<RotateCcw size={14} aria-hidden />} onClick={() => onAction('restore')} disabled={pending}>
                Restore
              </Button>
            ) : (
              <Button size="sm" variant="danger" icon={<ShieldOff size={14} aria-hidden />} onClick={() => onAction('revoke')} disabled={pending}>
                Revoke
              </Button>
            )}
            {c.deletable ? (
              <Button
                size="sm"
                variant="danger"
                iconOnly
                icon={<Trash2 size={14} aria-hidden />}
                aria-label={`Delete ${c.name}`}
                title={`Delete ${c.name}`}
                onClick={() => onAction('delete')}
              />
            ) : null}
          </span>
        ) : null}
      </div>
      {c.statusDetail && pending ? (
        <p className="key-none" role="status">
          {c.statusDetail}
        </p>
      ) : null}
      <dl className="cred-facts">
        <div>
          <dt>Env var in sandboxes</dt>
          <dd className="mono">{c.env ?? '–'}</dd>
        </div>
        <div className="cred-fact-wide">
          <dt>Placeholder</dt>
          <dd className="mono" title={c.placeholder ?? undefined}>
            {c.placeholder ?? '–'}
          </dd>
        </div>
        <div>
          <dt>Hosts</dt>
          <dd className="mono" title={c.hosts.join(', ')}>
            {c.hosts.length ? c.hosts.join(', ') : '–'}
          </dd>
        </div>
        <div>
          <dt>Looks in</dt>
          <dd title={looksIn(c)}>{looksIn(c)}</dd>
        </div>
      </dl>
      {c.onAllowlist === false ? (
        <p className="cred-warn">
          A host isn’t on the allowlist: iron-proxy refuses those requests before it could swap the placeholder.
        </p>
      ) : null}
      {c.kind === 'custom' && (c.createdAt || c.rotatedAt) ? (
        <p className="cred-meta tabular">
          {c.createdAt ? `Added ${fmtAgo(c.createdAt, now)}${c.createdBy ? ` by ${c.createdBy}` : ''}` : ''}
          {c.rotatedAt ? ` · rotated ${fmtAgo(c.rotatedAt, now)}${c.rotatedBy ? ` by ${c.rotatedBy}` : ''}` : ''}
          {c.secretName ? (
            <>
              {' · '}
              <span className="mono">{c.secretName}</span>
            </>
          ) : null}
        </p>
      ) : null}
    </li>
  );
}

function AddCredentialForm({ editable, onAdded }: { editable: boolean; onAdded: () => void }) {
  const ids = useId();
  const [name, setName] = useState('');
  const [env, setEnv] = useState('');
  const [hosts, setHosts] = useState('');
  const [headers, setHeaders] = useState('');
  const [query, setQuery] = useState(false);
  const [body, setBody] = useState(false);
  const valueRef = useRef<HTMLInputElement>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [done, setDone] = useState<{ name: string; env: string; placeholder: string | null; warnings: string[] } | null>(null);
  const [busy, setBusy] = useState(false);

  function validate(value: string): Record<string, string> {
    const e: Record<string, string> = {};
    if (!NAME_RE.test(name)) e.name = 'Lowercase letters, digits and dashes, 2 to 32, starting with a letter.';
    else if (['anthropic', 'github', 'demo'].includes(name)) e.name = 'That name is a built-in credential.';
    if (!ENV_RE.test(env)) e.env = 'Upper-case letters, digits and underscores (e.g. TEST_KEY).';
    const hs = splitList(hosts);
    if (!hs.length) e.hosts = 'At least one host.';
    else if (hs.length > 10) e.hosts = 'At most 10 hosts.';
    else {
      const bad = hs.map((h) => [h, hostProblem(h)] as const).find(([, p]) => p);
      if (bad) e.hosts = `${bad[0]}: ${bad[1]}`;
    }
    const hd = splitList(headers);
    if (hd.length > 10) e.headers = 'At most 10 header names.';
    else if (hd.some((h) => !HEADER_RE.test(h))) e.headers = 'Header names only (letters, digits, dashes).';
    if (!value) e.value = 'Enter the real value.';
    else if (value.length > MAX_VALUE) e.value = `At most ${MAX_VALUE.toLocaleString()} characters.`;
    return e;
  }

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    const value = valueRef.current?.value ?? '';
    const e = validate(value);
    setErrors(e);
    setFormError(null);
    if (Object.keys(e).length) {
      document.getElementById(`${ids}-${Object.keys(e)[0]}`)?.focus();
      return;
    }
    setBusy(true);
    try {
      const r = await api.createCredential({
        name,
        env,
        hosts: splitList(hosts).map((h) => h.toLowerCase().replace(/\.$/, '')),
        headers: splitList(headers),
        query,
        body,
        value,
      });
      noteWrite(r);
      const cred = (r.raw.credential ?? {}) as Record<string, unknown>;
      setDone({ name, env, placeholder: typeof cred.placeholder === 'string' ? cred.placeholder : null, warnings: r.warnings });
      setName('');
      setEnv('');
      setHosts('');
      setHeaders('');
      setQuery(false);
      setBody(false);
      onAdded();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : String(err));
    } finally {
      // The value lives only in the input and is cleared after every submit that reaches the server.
      if (valueRef.current) valueRef.current.value = '';
      setBusy(false);
    }
  }

  const err = (k: string) => (
    <p id={`${ids}-${k}-err`} className="field-error" hidden={!errors[k]}>
      {errors[k]}
    </p>
  );
  const fieldProps = (k: string) => ({
    id: `${ids}-${k}`,
    'aria-invalid': Boolean(errors[k]),
    'aria-describedby': `${ids}-${k}-err`,
    disabled: !editable,
  });

  return (
    <form className="policy-form" onSubmit={(e) => void submit(e)} noValidate>
      <h2 className="policy-h2">Add a credential</h2>
      <div className="field-row">
        <div className="field">
          <label htmlFor={`${ids}-name`}>Name</label>
          <input {...fieldProps('name')} className="input" value={name} placeholder="httpbin-test" autoComplete="off" spellCheck={false} maxLength={32} onChange={(e) => setName(e.target.value)} />
          {err('name')}
        </div>
        <div className="field">
          <label htmlFor={`${ids}-env`}>Env var</label>
          <input {...fieldProps('env')} className="input mono-input" value={env} placeholder="TEST_KEY" autoComplete="off" spellCheck={false} maxLength={64} onChange={(e) => setEnv(e.target.value.toUpperCase())} />
          {err('env')}
        </div>
      </div>
      <div className="field">
        <label htmlFor={`${ids}-hosts`}>Hosts</label>
        <input {...fieldProps('hosts')} className="input mono-input" value={hosts} placeholder="httpbin.org, *.example.com" autoComplete="off" spellCheck={false} onChange={(e) => setHosts(e.target.value)} />
        {err('hosts')}
      </div>
      <fieldset className="field cred-where" disabled={!editable}>
        <legend>Where iron-proxy looks for the placeholder</legend>
        <label htmlFor={`${ids}-headers`} className="sr-only">
          Header names
        </label>
        <input {...fieldProps('headers')} className="input mono-input" value={headers} placeholder="X-Test-Key (empty: every header)" autoComplete="off" spellCheck={false} onChange={(e) => setHeaders(e.target.value)} />
        {err('headers')}
        <div className="check-row">
          <label className="check">
            <input type="checkbox" checked={query} onChange={(e) => setQuery(e.target.checked)} /> URL query
          </label>
          <label className="check">
            <input type="checkbox" checked={body} onChange={(e) => setBody(e.target.checked)} /> Request body
          </label>
        </div>
      </fieldset>
      <div className="field">
        <label htmlFor={`${ids}-value`}>Real value</label>
        <input
          ref={valueRef}
          {...fieldProps('value')}
          type="password"
          className="input"
          autoComplete="off"
          spellCheck={false}
          placeholder="Write-only"
          data-1p-ignore
          data-lpignore="true"
        />
        {err('value')}
        <p className="form-hint">Goes straight into Secrets Manager. It is never shown, logged or sent to a sandbox.</p>
      </div>
      {formError ? (
        <p className="form-error" role="alert">
          {formError}
        </p>
      ) : null}
      {done ? (
        <div className="form-done" role="status">
          <p>
            Added <strong>{done.name}</strong>. New sandboxes get <code>{done.env}</code>
            {done.placeholder ? (
              <>
                {' = '}
                <code title={done.placeholder}>{done.placeholder}</code>
              </>
            ) : null}
            ; running ones keep their environment.
          </p>
          {done.warnings.map((w) => (
            <p key={w} className="form-hint is-warn">
              {w}
            </p>
          ))}
        </div>
      ) : (
        <p className="form-hint">New sandboxes get the placeholder in the env var; running ones don’t.</p>
      )}
      <div className="form-actions">
        <Button type="submit" variant="primary" icon={<Plus size={14} aria-hidden />} loading={busy} disabled={!editable}>
          Add credential
        </Button>
      </div>
      {!editable ? <p className="form-hint">Only admins can add credentials.</p> : null}
    </form>
  );
}
