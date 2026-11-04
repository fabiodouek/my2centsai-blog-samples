import { forwardRef, useEffect, useRef, type ButtonHTMLAttributes, type ReactNode } from 'react';
import { Info, LoaderCircle, X } from 'lucide-react';
import type { SandboxState } from '../../lib/types';
import { dismissToast, useStore } from '../../lib/store';

// ---------------------------------------------------------------- Button

type Variant = 'primary' | 'secondary' | 'ghost' | 'danger';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  icon?: ReactNode;
  loading?: boolean;
  size?: 'sm' | 'md';
  iconOnly?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', icon, loading, size = 'md', iconOnly, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type="button"
      className={['btn', `btn-${variant}`, `btn-${size}`, iconOnly ? 'btn-icon' : '', className ?? ''].join(' ')}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? <LoaderCircle className="spin" size={14} aria-hidden /> : icon}
      {children !== undefined && !iconOnly ? <span>{children}</span> : null}
    </button>
  );
});

// ---------------------------------------------------------------- state

export const STATE_LABEL: Record<SandboxState, string> = {
  PENDING: 'Starting',
  RUNNING: 'Running',
  SUSPENDING: 'Suspending',
  SUSPENDED: 'Suspended',
  RESUMING: 'Resuming',
  TERMINATING: 'Terminating',
  TERMINATED: 'Terminated',
  UNKNOWN: 'Unknown',
};

/**
 * The backend reports a resume as SUSPENDED -> PENDING -> RUNNING. A PENDING sandbox that started
 * more than a minute ago is resuming, not starting, so label it that way.
 */
export function shownState(sb: { state: SandboxState; startedAt: number | null }): SandboxState {
  if (sb.state === 'PENDING' && sb.startedAt && Date.now() - sb.startedAt > 60_000) return 'RESUMING';
  return sb.state;
}

export function stateTone(s: SandboxState): 'run' | 'sleep' | 'deny' | 'off' {
  if (s === 'RUNNING' || s === 'PENDING' || s === 'RESUMING') return 'run';
  if (s === 'SUSPENDED' || s === 'SUSPENDING') return 'sleep';
  if (s === 'TERMINATING') return 'deny';
  return 'off';
}

export function isTransitional(s: SandboxState) {
  return s === 'PENDING' || s === 'SUSPENDING' || s === 'RESUMING' || s === 'TERMINATING';
}

export function StateDot({ state, still }: { state: SandboxState; still?: boolean }) {
  return <span className={`dot tone-${stateTone(state)}${isTransitional(state) && !still ? ' pulse' : ''}`} aria-hidden />;
}

export function StatePill({ state }: { state: SandboxState }) {
  return (
    <span className={`state-pill tone-${stateTone(state)}`}>
      <StateDot state={state} />
      {STATE_LABEL[state]}
    </span>
  );
}

// ---------------------------------------------------------------- key fob (the signature)

/** A credential drawn as a brass key tag. `empty` is the hook with no key on it (revoked). */
export function KeyFob({ name, empty, size = 'sm', title }: { name: string; empty?: boolean; size?: 'sm' | 'md'; title?: string }) {
  return (
    <span className={`fob fob-${size}${empty ? ' fob-empty' : ''}`} title={title}>
      <span className="fob-hole" aria-hidden />
      <span className="fob-name">{name}</span>
    </span>
  );
}

// ---------------------------------------------------------------- segmented filter

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  label,
}: {
  value: T;
  options: { value: T; label: string; count?: number }[];
  onChange: (v: T) => void;
  label: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          className="segmented-item"
          onClick={() => onChange(o.value)}
        >
          {o.label}
          {o.count !== undefined ? <span className="segmented-count">{o.count}</span> : null}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- notes and empty states

export function Note({ children }: { children: ReactNode }) {
  return (
    <p className="note">
      <Info size={13} aria-hidden />
      <span>{children}</span>
    </p>
  );
}

export function Empty({ icon, title, children, action }: { icon?: ReactNode; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="empty">
      {icon ? <div className="empty-icon">{icon}</div> : null}
      <p className="empty-title">{title}</p>
      {children ? <div className="empty-body">{children}</div> : null}
      {action ? <div className="empty-action">{action}</div> : null}
    </div>
  );
}

export function SkeletonRows({ rows = 4, height = 'var(--row)' }: { rows?: number; height?: string }) {
  return (
    <div className="skeleton-rows" aria-hidden>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="skeleton" style={{ height, width: `${92 - ((i * 17) % 35)}%` }} />
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- confirm dialog

export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  tone = 'danger',
  busy,
  onConfirm,
  onClose,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  confirmLabel: string;
  tone?: 'danger' | 'primary';
  busy?: boolean;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) {
      d.showModal();
      cancelRef.current?.focus();
    } else if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-labelledby="dialog-title"
      onCancel={(e) => {
        e.preventDefault();
        if (!busy) onClose();
      }}
      onClick={(e) => {
        if (e.target === ref.current && !busy) onClose();
      }}
    >
      <div className="dialog-inner">
        <h2 id="dialog-title" className="dialog-title">
          {title}
        </h2>
        <div className="dialog-body">{children}</div>
        <div className="dialog-actions">
          <Button ref={cancelRef} variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button variant={tone === 'danger' ? 'danger' : 'primary'} className="btn-solid" loading={busy} onClick={onConfirm}>
            {confirmLabel}
          </Button>
        </div>
      </div>
    </dialog>
  );
}

// ---------------------------------------------------------------- toasts

export function Toasts() {
  const toasts = useStore((s) => s.toasts);
  return (
    <div className="toasts" role="region" aria-label="Notifications" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.kind}`} role={t.kind === 'error' ? 'alert' : 'status'}>
          <div className="toast-text">
            <p className="toast-title">{t.title}</p>
            {t.body ? <p className="toast-body">{t.body}</p> : null}
          </div>
          <button type="button" className="toast-close" aria-label="Dismiss" onClick={() => dismissToast(t.id)}>
            <X size={14} aria-hidden />
          </button>
        </div>
      ))}
    </div>
  );
}
