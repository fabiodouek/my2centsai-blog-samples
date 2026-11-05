import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Lock, Plus, RefreshCw, X } from 'lucide-react';
import { api } from '../../lib/api';
import { usePolling } from '../../lib/hooks';
import { noteWrite, usePolicy } from '../../lib/policy';
import { toast } from '../../lib/store';
import type { AllowHost } from '../../lib/types';
import { Button, Empty, SkeletonRows } from '../ui/primitives';

/** Mirrors the backend's checks so obvious mistakes are caught before a round trip; the server has the last word. */
export function hostProblem(raw: string): string | null {
  const h = raw.trim().toLowerCase().replace(/\.$/, '');
  if (!h) return 'Enter a host name.';
  if (/^[a-z]+:\/\//.test(h) || h.includes('/')) return 'Just the host name: no scheme or path.';
  if (h === '*') return 'A bare * would allow every host.';
  if (/^\[?[0-9a-f:]+\]?$/.test(h) && h.includes(':')) return 'IP addresses aren’t allowed; use a host name.';
  if (/^\d{1,3}(\.\d{1,3}){3}$/.test(h)) return 'IP addresses aren’t allowed; use a host name.';
  if (h.includes(':')) return 'No port: iron-proxy matches host names only.';
  if (h.includes('*') && !/^\*\.[^*]+$/.test(h)) return 'Wildcards only as a leading *. (like *.example.com).';
  if (h.startsWith('*.') && h.slice(2).split('.').length < 2) return 'That wildcard covers a whole top-level domain.';
  if (!/^(\*\.)?[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(h)) return 'Use letters, digits, dots and dashes (e.g. example.com).';
  if (h === 'amazonaws.com' || h.endsWith('.amazonaws.com')) return 'AWS endpoints follow AWS access; choose the service there.';
  return null;
}

export function AllowlistSection({ editable }: { editable: boolean }) {
  const tick = usePolicy((s) => s.tick);
  const list = usePolling(api.allowlist, 30_000, true, 'policy-allowlist');
  const [value, setValue] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [fresh, setFresh] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const ids = useId();

  const refresh = list.refresh;
  useEffect(() => {
    if (tick) void refresh();
  }, [tick, refresh]);

  const { custom, aws } = useMemo(() => {
    const hosts = list.data?.hosts ?? [];
    return { custom: hosts.filter((h) => h.managed !== 'aws'), aws: hosts.filter((h) => h.managed === 'aws') };
  }, [list.data]);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    const problem = hostProblem(value);
    if (problem) {
      setError(problem);
      inputRef.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const host = value.trim().toLowerCase().replace(/\.$/, '');
      const r = await api.allowHost(host);
      noteWrite(r);
      setFresh(String(r.raw.host ?? host));
      setValue('');
      inputRef.current?.focus();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      inputRef.current?.focus();
    } finally {
      setBusy(false);
    }
  }

  async function remove(h: AllowHost) {
    setRemoving(h.host);
    try {
      const r = await api.removeHost(h.host);
      noteWrite(r);
      toast('success', `Removed ${h.host}`, 'Sandboxes can’t reach it once the proxy reloads.');
    } catch (err) {
      toast('error', `Could not remove ${h.host}`, err instanceof Error ? err.message : String(err));
    } finally {
      setRemoving(null);
    }
  }

  return (
    <div className="policy-grid is-list">
      <div className="policy-main">
        <div className="section-title-row">
          <h2 className="policy-h2">
            Allowed hosts <span className="policy-count tabular">{list.data ? list.data.hosts.length : ''}</span>
          </h2>
          {list.data?.version !== null && list.data?.version !== undefined ? (
            <span className="policy-meta tabular">config v{list.data.version}</span>
          ) : null}
        </div>
        {list.data === null && list.loading ? <SkeletonRows rows={8} /> : null}
        {list.data === null && list.error ? (
          <Empty
            title="Couldn’t load the allowlist"
            action={
              <Button size="sm" icon={<RefreshCw size={14} aria-hidden />} onClick={() => void list.refresh()}>
                Retry
              </Button>
            }
          >
            {list.error.message}
          </Empty>
        ) : null}
        {list.data ? (
          <>
            <h3 className="policy-h3">Hosts sandboxes may reach</h3>
            {custom.length === 0 ? <p className="policy-empty">No hosts: iron-proxy refuses everything outside AWS.</p> : null}
            <ul className="policy-rows" role="list" aria-label="Allowed hosts">
              {custom.map((h) => (
                <li key={h.host} className={`policy-row${fresh === h.host ? ' is-fresh' : ''}`}>
                  <span className="policy-host mono" title={h.host}>
                    {h.host}
                  </span>
                  {h.host.startsWith('*.') ? <span className="tag">and subdomains</span> : null}
                  <span className="filter-spacer" />
                  {editable ? (
                    <Button
                      size="sm"
                      variant="ghost"
                      iconOnly
                      icon={<X size={14} aria-hidden />}
                      aria-label={`Remove ${h.host}`}
                      title={`Remove ${h.host}`}
                      loading={removing === h.host}
                      onClick={() => void remove(h)}
                    />
                  ) : null}
                </li>
              ))}
            </ul>
            {aws.length ? (
              <>
                <h3 className="policy-h3">
                  <Lock size={12} aria-hidden /> AWS endpoints, set by AWS access
                </h3>
                <ul className="policy-rows" role="list" aria-label="AWS endpoints managed by AWS access">
                  {aws.map((h) => (
                    <li key={h.host} className="policy-row is-managed">
                      <span className="policy-host mono" title={h.host}>
                        {h.host}
                      </span>
                      <span className="filter-spacer" />
                      {h.service ? (
                        <span className="tag mono-tag" title={`Allowed while ${h.service} is chosen under AWS access`}>
                          {h.service}
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
          </>
        ) : null}
      </div>

      <aside className="policy-side" aria-label="Add a host">
        <form className="policy-form" onSubmit={(e) => void add(e)} noValidate>
          <h2 className="policy-h2">Allow a host</h2>
          <div className="field">
            <label htmlFor={`${ids}-host`}>Host name</label>
            <div className="field-inline">
              <input
                ref={inputRef}
                id={`${ids}-host`}
                className="input mono-input"
                value={value}
                placeholder="example.com"
                autoComplete="off"
                spellCheck={false}
                autoCapitalize="none"
                disabled={!editable}
                aria-invalid={Boolean(error)}
                aria-describedby={`${ids}-host-err ${ids}-host-hint`}
                onChange={(e) => {
                  setValue(e.target.value);
                  if (error) setError(null);
                }}
              />
              <Button type="submit" variant="primary" icon={<Plus size={14} aria-hidden />} loading={busy} disabled={!editable || !value.trim()}>
                Add
              </Button>
            </div>
            <p id={`${ids}-host-err`} className="field-error" role="alert" hidden={!error}>
              {error}
            </p>
          </div>
          <p id={`${ids}-host-hint`} className="form-hint">
            An exact name, or <code>*.example.com</code> for a domain and its subdomains. No IP addresses, ports or
            paths. AWS endpoints aren’t added here: they follow the services under AWS access.
          </p>
          {!editable ? <p className="form-hint">Only admins can change the allowlist.</p> : null}
        </form>
      </aside>
    </div>
  );
}
