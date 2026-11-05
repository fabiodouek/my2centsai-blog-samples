import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { Plus, X } from 'lucide-react';
import { createSandbox, useStore } from '../lib/store';
import { fmtLifetime } from '../lib/format';
import type { AppConfig, SizeId, SizeOption } from '../lib/types';
import { Button } from './ui/primitives';

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/;
const IDLE_OPTIONS = [1, 5, 10, 15, 30, 60, 120];
const LIFETIME_OPTIONS = [15, 30, 60, 120, 240, 480];
/** "Terminate when suspended for": null = when the lifetime ends (the default). */
const SUSPENDED_OPTIONS: (number | null)[] = [null, 0, 1, 5, 15, 30, 60, 120, 240];

// Used when the backend predates sizes (and in tests): medium only
const FALLBACK_SIZES: SizeOption[] = [
  { id: 'medium', label: 'Medium', memoryGb: 2, vcpu: 1, usdPerHour: 0.1261, available: true, unavailableReason: null, note: null, isDefault: true },
];

interface EnvRow {
  key: number;
  name: string;
  value: string;
}

function usd(n: number): string {
  return `$${n < 0.1 ? n.toFixed(3) : n.toFixed(3).replace(/0$/, '')}/h`;
}

function vcpu(n: number): string {
  return `${n} vCPU`;
}

/** Mirrors the backend's checks (app/sandbox_env.py) so mistakes show before a round trip; the server decides. */
function envProblem(name: string, value: string, cfg: AppConfig | null, seen: Set<string>): string | null {
  if (!name) return value ? 'Give the variable a name.' : null;
  const re = new RegExp(cfg?.env.namePattern ?? '^[A-Za-z_][A-Za-z0-9_]*$');
  if (!re.test(name)) return 'Letters, digits and _, not starting with a digit.';
  const upper = name.toUpperCase();
  if ((cfg?.env.reserved ?? []).some((r) => r === name || r === upper) || (cfg?.env.reservedPrefixes ?? ['AWS_']).some((p) => upper.startsWith(p)))
    return `${name} is reserved: the sandbox sets it itself.`;
  if (seen.has(name)) return `${name} is set twice.`;
  if ([...value].some((c) => c.charCodeAt(0) < 32 || c.charCodeAt(0) > 126)) return 'Printable ASCII only.';
  if ([...(cfg?.env.forbiddenChars ?? '"$\\`')].some((c) => value.includes(c))) return 'No $, backquote, " or backslash: it goes in a shell file.';
  if (value.length > (cfg?.env.valueMax ?? 2048)) return `At most ${cfg?.env.valueMax ?? 2048} characters.`;
  return null;
}

export function NewSandboxForm({ onDone }: { onDone: () => void }) {
  const config = useStore((s) => s.config);
  const quota = useStore((s) => s.quota);
  const count = useStore((s) => (s.sandboxes ?? []).filter((x) => x.state !== 'TERMINATED').length);
  const sizes = config?.sizes.length ? config.sizes : FALLBACK_SIZES;
  const quotaGb = quota?.quotaGb ?? config?.quotaGb ?? 8;
  const free = quota ? quota.freeGb : null;

  // The free memory is our estimate (Service Quotas minus what we count as live): it never blocks a size. Create
  // always reaches RunMicrovm and AWS decides; a size the estimate says won't fit only gets a hint.
  const fits = (s: SizeOption) => free === null || s.memoryGb <= free;
  const mayExceed = (s: SizeOption) =>
    !fits(s) ? `May exceed this account’s MicroVM memory quota (${free} GB free of ${quotaGb}); AWS will refuse it if so.` : null;
  const initialSize = (): SizeId => {
    const def = sizes.find((s) => s.id === (config?.defaultSize ?? 'medium'));
    if (def?.available && fits(def)) return def.id;
    return ([...sizes].reverse().find((s) => s.available && fits(s)) ?? (def?.available ? def : null) ?? sizes.find((s) => s.available) ?? sizes[0]).id;
  };

  const [name, setName] = useState('');
  const [size, setSize] = useState<SizeId>(initialSize);
  const [idle, setIdle] = useState(config?.idle.defaultMinutes ?? config?.defaultIdleMinutes ?? 15);
  const [life, setLife] = useState(config?.lifetime.defaultMinutes ?? 480);
  const [suspended, setSuspended] = useState<number | null>(null);
  const [autoResume, setAutoResume] = useState(true);
  const [env, setEnv] = useState<EnvRow[]>([]);
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const nextKey = useRef(1);
  const ids = useId();

  useEffect(() => inputRef.current?.focus(), []);

  const chosen = sizes.find((s) => s.id === size) ?? sizes[0];
  const maxLife = config?.lifetime.maxMinutes ?? 480;
  const nameError = !name.trim() ? 'Give it a name.' : !NAME_RE.test(name.trim()) ? 'Letters, digits, dot, dash or underscore; up to 40.' : null;
  const lifeError =
    !Number.isInteger(life) || life < 1 || life > maxLife
      ? `1 to ${maxLife} minutes.`
      : idle >= life
        ? 'Must be longer than the idle time before suspending.'
        : null;
  const suspendedChoice = suspended !== null && suspended > life ? null : suspended;

  const envChecks = useMemo(() => {
    const seen = new Set<string>();
    return env.map((r) => {
      const n = r.name.trim();
      const p = envProblem(n, r.value, config, seen);
      if (n) seen.add(n);
      return p;
    });
  }, [env, config]);
  const envBytes = env.reduce((n, r) => (r.name.trim() ? n + r.name.trim().length + r.value.length + 6 : n), 0);
  const payloadLimit = config?.runPayload.limit ?? 4096;
  const bytesLeft = payloadLimit - (config?.runPayload.usedBytes ?? 1300) - envBytes;
  const envError = envChecks.some(Boolean) || bytesLeft < 0 || env.length > (config?.env.maxVars ?? 32);

  // Only a missing or unbuilt image blocks a size
  const sizeBlocked = !chosen.available ? chosen.unavailableReason ?? 'not available' : null;
  const sizeWarning = sizeBlocked ? null : mayExceed(chosen);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (nameError || lifeError || envError || sizeBlocked || busy) return;
    setBusy(true);
    const vars: Record<string, string> = {};
    for (const r of env) if (r.name.trim()) vars[r.name.trim()] = r.value;
    const ok = await createSandbox({
      name: name.trim(),
      size,
      idleMinutes: idle,
      maxMinutes: life,
      suspendedMinutes: suspendedChoice,
      autoResume,
      env: vars,
    });
    setBusy(false);
    if (ok) onDone();
  }

  function setRow(key: number, patch: Partial<EnvRow>) {
    setEnv((rows) => rows.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  }

  return (
    <form id="new-sandbox" className="new-form" onSubmit={submit} noValidate>
      <div className="field">
        <label htmlFor={`${ids}-name`}>Name</label>
        <input
          ref={inputRef}
          id={`${ids}-name`}
          className="input"
          value={name}
          placeholder={`sandbox-${count + 1}`}
          autoComplete="off"
          spellCheck={false}
          maxLength={40}
          aria-invalid={touched && Boolean(nameError)}
          aria-describedby={`${ids}-name-err`}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => name && setTouched(true)}
        />
        <p id={`${ids}-name-err`} className="field-error" hidden={!(touched && nameError)}>
          {nameError}
        </p>
      </div>

      <fieldset className="field">
        <legend>Size</legend>
        <div className="size-list" role="radiogroup" aria-label="Size">
          {sizes.map((s) => {
            const reason = !s.available ? s.unavailableReason ?? 'not available' : null;
            const warn = reason ? null : mayExceed(s);
            return (
              <label key={s.id} className={`size-opt${reason ? ' is-off' : ''}${warn ? ' may-exceed' : ''}`} title={reason ?? warn ?? undefined}>
                <input
                  type="radio"
                  name={`${ids}-size`}
                  value={s.id}
                  checked={size === s.id}
                  disabled={Boolean(reason)}
                  onChange={() => setSize(s.id)}
                />
                <span className="size-main">
                  <span className="size-name">{s.label}</span>
                  <span className="size-price tabular">{usd(s.usdPerHour)}</span>
                </span>
                <span className="size-meta tabular">{reason ?? `${s.memoryGb} GB · ${vcpu(s.vcpu)}`}</span>
              </label>
            );
          })}
        </div>
        <p className="form-hint">
          Baseline memory and vCPU: what’s billed and what counts against the quota. A sandbox can burst to{' '}
          {config?.burstFactor ?? 4}x, and that ceiling is what <code>nproc</code> and <code>free</code> show inside.
        </p>
        {chosen.note && !sizeBlocked && !sizeWarning ? <p className="form-hint is-warn">{chosen.note}</p> : null}
      </fieldset>

      <div className="field-row">
        <div className="field">
          <label htmlFor={`${ids}-idle`}>Idle suspend</label>
          <select id={`${ids}-idle`} className="input" value={idle} onChange={(e) => setIdle(Number(e.target.value))}>
            {IDLE_OPTIONS.map((m) => (
              <option key={m} value={m}>
                after {m} min
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor={`${ids}-life`}>Lifetime</label>
          <div className="input-suffix">
            <input
              id={`${ids}-life`}
              className="input tabular"
              type="number"
              inputMode="numeric"
              min={1}
              max={maxLife}
              step={1}
              list={`${ids}-life-list`}
              value={Number.isNaN(life) ? '' : life}
              aria-invalid={Boolean(lifeError)}
              aria-describedby={`${ids}-life-err`}
              onChange={(e) => setLife(e.target.value === '' ? NaN : Math.round(Number(e.target.value)))}
            />
            <span className="suffix" aria-hidden>
              min
            </span>
          </div>
          <datalist id={`${ids}-life-list`}>
            {LIFETIME_OPTIONS.filter((m) => m <= maxLife).map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </div>
      </div>
      <p id={`${ids}-life-err`} className={lifeError ? 'field-error' : 'form-hint'}>
        {lifeError ?? `${fmtLifetime(life * 60)}, suspended time included.`}
      </p>

      <div className="field">
        <label htmlFor={`${ids}-susp`}>Terminate when suspended</label>
        <select
          id={`${ids}-susp`}
          className="input"
          value={suspendedChoice === null ? 'life' : String(suspendedChoice)}
          onChange={(e) => setSuspended(e.target.value === 'life' ? null : Number(e.target.value))}
        >
          {SUSPENDED_OPTIONS.filter((m) => m === null || m <= life).map((m) => (
            <option key={m ?? 'life'} value={m === null ? 'life' : m}>
              {m === null ? 'when the lifetime ends' : m === 0 ? 'right away' : `after ${m} min suspended`}
            </option>
          ))}
        </select>
      </div>

      <p className="form-hint">
        {suspendedChoice === null
          ? 'Suspended time counts toward the lifetime.'
          : 'Counts from when it suspends itself after idling; after Suspend it stays until the lifetime ends.'}
      </p>

      <label className="check">
        <input type="checkbox" checked={autoResume} onChange={(e) => setAutoResume(e.target.checked)} />
        Resume on connect
      </label>
      <p className="form-hint">
        {autoResume
          ? 'Opening its terminal wakes a suspended sandbox.'
          : 'Off: a suspended sandbox stays suspended until you press Resume; the terminal won’t wake it.'}
      </p>

      <fieldset className="field">
        <legend>Environment variables</legend>
        {env.map((r, i) => (
          <div key={r.key} className="env-row">
            <input
              className="input mono-input"
              placeholder="NAME"
              aria-label={`Variable ${i + 1} name`}
              value={r.name}
              spellCheck={false}
              autoComplete="off"
              maxLength={128}
              aria-invalid={Boolean(envChecks[i])}
              onChange={(e) => setRow(r.key, { name: e.target.value })}
            />
            <input
              className="input mono-input"
              placeholder="value"
              aria-label={`Variable ${i + 1} value`}
              value={r.value}
              spellCheck={false}
              autoComplete="off"
              aria-invalid={Boolean(envChecks[i])}
              onChange={(e) => setRow(r.key, { value: e.target.value })}
            />
            <Button
              size="sm"
              variant="ghost"
              iconOnly
              icon={<X size={14} aria-hidden />}
              aria-label={`Remove variable ${i + 1}`}
              onClick={() => setEnv((rows) => rows.filter((x) => x.key !== r.key))}
            />
            {envChecks[i] ? <p className="field-error env-err">{envChecks[i]}</p> : null}
          </div>
        ))}
        <div className="env-foot">
          <Button
            size="sm"
            variant="ghost"
            icon={<Plus size={14} aria-hidden />}
            disabled={env.length >= (config?.env.maxVars ?? 32)}
            onClick={() => setEnv((rows) => [...rows, { key: nextKey.current++, name: '', value: '' }])}
          >
            Add variable
          </Button>
          <span className={`form-hint tabular${bytesLeft < 0 ? ' is-warn' : ''}`}>
            {bytesLeft < 0 ? `${-bytesLeft} bytes over` : `${bytesLeft.toLocaleString()} bytes left`}
          </span>
        </div>
        {env.length ? (
          <p className="form-hint is-warn">
            Visible inside the sandbox and stored by the app: not for secrets. Keys go under Proxy policy → Credentials.
          </p>
        ) : null}
      </fieldset>

      <div className="new-form-foot">
        <p className={`form-hint${sizeBlocked || sizeWarning ? ' is-warn' : ''}`} role={sizeWarning ? 'status' : undefined}>
          {sizeBlocked
            ? `${chosen.label} isn’t available: ${sizeBlocked}.`
            : sizeWarning
              ? `${chosen.label}: ${sizeWarning} Suspended sandboxes count too.`
              : `Uses ${chosen.memoryGb} GB of the ${quotaGb} GB account quota, suspended or not. Egress goes through iron-proxy.`}
        </p>
        <div className="form-actions">
          <Button type="submit" variant="primary" loading={busy} disabled={Boolean(sizeBlocked)}>
            Create sandbox
          </Button>
        </div>
      </div>
    </form>
  );
}
