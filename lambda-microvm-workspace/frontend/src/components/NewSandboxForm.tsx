import { useEffect, useId, useRef, useState } from 'react';
import { createSandbox, useStore } from '../lib/store';
import { Button } from './ui/primitives';

const IDLE_OPTIONS = [5, 10, 15, 30, 60];
const LIFETIME_OPTIONS = [1, 2, 4, 8];
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$/;

export function NewSandboxForm({ onDone }: { onDone: () => void }) {
  const config = useStore((s) => s.config);
  const quota = useStore((s) => s.quota);
  const count = useStore((s) => (s.sandboxes ?? []).filter((x) => x.state !== 'TERMINATED').length);
  const [name, setName] = useState('');
  const [idle, setIdle] = useState(config?.defaultIdleMinutes ?? 15);
  const [hours, setHours] = useState(8);
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const ids = useId();

  useEffect(() => inputRef.current?.focus(), []);

  const vmGb = quota?.vmMemoryGb ?? config?.vmMemoryGb ?? 2;
  const free = quota ? quota.quotaGb - quota.usedGb : null;
  const full = free !== null && free < vmGb;
  const nameError = !name.trim() ? 'Give it a name.' : !NAME_RE.test(name.trim()) ? 'Letters, digits, dot, dash or underscore; up to 40.' : null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    if (nameError || full || busy) return;
    setBusy(true);
    const ok = await createSandbox({ name: name.trim(), idleMinutes: idle, maxHours: hours });
    setBusy(false);
    if (ok) onDone();
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
          <select id={`${ids}-life`} className="input" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
            {LIFETIME_OPTIONS.map((h) => (
              <option key={h} value={h}>
                {h} h
              </option>
            ))}
          </select>
        </div>
      </div>
      <p className={`form-hint${full ? ' is-warn' : ''}`}>
        {full
          ? `The account’s ${quota!.quotaGb} GB MicroVM quota is full. Terminate a sandbox to free ${vmGb} GB; suspended ones still count.`
          : `Uses ${vmGb} GB of the ${quota?.quotaGb ?? 8} GB account quota, suspended or not. The lifetime counts suspended time.`}
      </p>
      <div className="form-actions">
        <Button type="submit" variant="primary" loading={busy} disabled={full}>
          Create sandbox
        </Button>
      </div>
    </form>
  );
}
