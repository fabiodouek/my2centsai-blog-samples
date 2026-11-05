import { useMemo } from 'react';
import { ChevronRight, LogOut, Plus, RefreshCw, ShieldCheck, X } from 'lucide-react';
import { signOut } from '../lib/auth';
import { MOCK } from '../lib/config';
import { endReason, fmtAgo, fmtClock, fmtDuration } from '../lib/format';
import { useNow } from '../lib/hooks';
import { closePolicy, idleDeadline, openPolicy, refreshSandboxes, select, setCreating, useStore } from '../lib/store';
import type { Sandbox } from '../lib/types';
import { NewSandboxForm } from './NewSandboxForm';
import { QuotaMeter } from './QuotaMeter';
import { Button, Empty, SkeletonRows, STATE_LABEL, StateDot, shownState, stateTone } from './ui/primitives';
import { LifeBar } from './charts/LifeBar';
import { LogoMark, Wordmark } from './ui/Logo';

export function Sidebar({ onClose }: { onClose: () => void }) {
  const sandboxes = useStore((s) => s.sandboxes);
  const error = useStore((s) => s.sandboxesError);
  const me = useStore((s) => s.me);
  const creating = useStore((s) => s.creating);

  const { active, ended } = useMemo(() => {
    const list = sandboxes ?? [];
    const order = (s: Sandbox) => s.createdAt ?? 0;
    return {
      active: list.filter((s) => s.state !== 'TERMINATED').sort((a, b) => order(a) - order(b)),
      ended: list.filter((s) => s.state === 'TERMINATED').sort((a, b) => order(b) - order(a)),
    };
  }, [sandboxes]);

  return (
    <aside className="sidebar" aria-label="Sandboxes">
      <header className="sidebar-head">
        <div className="brand" title="My2cents.ai Lambda MicroVM Playground">
          <LogoMark className="brand-logo" size={20} />
          <span className="brand-text">
            <Wordmark className="brand-name" />
            <span className="brand-sub">Lambda MicroVM Playground</span>
          </span>
        </div>
        {/* Icon-only so the whole name fits beside it at 252 px */}
        <Button
          variant={creating ? 'ghost' : 'secondary'}
          size="sm"
          iconOnly
          icon={creating ? <X size={14} aria-hidden /> : <Plus size={14} aria-hidden />}
          onClick={() => setCreating(!creating)}
          aria-expanded={creating}
          aria-controls="new-sandbox"
          aria-label={creating ? 'Cancel new sandbox' : 'New sandbox'}
          title={creating ? 'Cancel' : 'New sandbox'}
        />
        <button type="button" className="sidebar-close" aria-label="Close sandbox list" onClick={onClose}>
          <X size={16} aria-hidden />
        </button>
      </header>

      {creating ? <NewSandboxForm onDone={() => setCreating(false)} /> : null}

      <nav className="sandbox-list" aria-label="Your sandboxes">
        {sandboxes === null && !error ? <SkeletonRows rows={3} height="48px" /> : null}
        {error && sandboxes === null ? (
          <Empty title="Couldn’t load sandboxes" action={<Button size="sm" icon={<RefreshCw size={14} aria-hidden />} onClick={() => void refreshSandboxes()}>Retry</Button>}>
            {error}
          </Empty>
        ) : null}
        {sandboxes !== null && active.length === 0 && !creating ? (
          <Empty title="No sandboxes yet" />
        ) : null}
        <ul className="rows" role="list">
          {active.map((sb) => (
            <SandboxRow key={sb.id} sb={sb} />
          ))}
        </ul>
        {ended.length ? (
          <>
            <h3 className="list-heading">Ended in the last hour</h3>
            <ul className="rows" role="list">
              {ended.map((sb) => (
                <SandboxRow key={sb.id} sb={sb} />
              ))}
            </ul>
          </>
        ) : null}
      </nav>

      <footer className="sidebar-foot">
        <PolicyEntry />
        <QuotaMeter />
        <div className="account">
          <span className="account-email" title={me?.email}>
            {me?.email || ' '}
          </span>
          {MOCK ? <span className="mock-badge" title="All data on this page is synthetic">Mock data</span> : null}
          <Button variant="ghost" size="sm" iconOnly icon={<LogOut size={14} aria-hidden />} aria-label="Sign out" title="Sign out" onClick={() => void signOut()} />
        </div>
      </footer>
    </aside>
  );
}

/** The global proxy policy page: one entry for everyone (read-only unless in the admins group). */
function PolicyEntry() {
  const open = useStore((s) => s.view === 'policy');
  const me = useStore((s) => s.me);
  return (
    <button
      type="button"
      className={`nav-row${open ? ' is-selected' : ''}`}
      aria-current={open ? 'page' : undefined}
      onClick={() => (open ? closePolicy() : openPolicy())}
    >
      <ShieldCheck size={14} aria-hidden className="nav-row-icon" />
      <span className="nav-row-label">Proxy policy</span>
      <span className="nav-row-meta">{me && !me.isAdmin ? 'read-only' : 'all sandboxes'}</span>
      <ChevronRight size={14} aria-hidden className="nav-row-chevron" />
    </button>
  );
}

function SandboxRow({ sb }: { sb: Sandbox }) {
  const view = useStore((s) => s.view);
  const selected = useStore((s) => s.selectedId === sb.id) && view === 'terminal';
  const attachedHere = useStore((s) => Boolean(s.linked[sb.id]));
  // Subscribed so the row re-renders when the idle estimate's inputs change.
  useStore((s) => s.activityAt[sb.id]);
  const pending = useStore((s) => s.pending[sb.id]);
  const now = useNow();
  const ended = sb.state === 'TERMINATED';
  const shown = shownState(sb);

  const expiresAt = sb.expiresAt ?? (sb.startedAt ? sb.startedAt + sb.maxDurationS * 1000 : null);
  const left = expiresAt ? expiresAt - now : null;
  const idleAt = idleDeadline(sb);
  const lowTime = left !== null && left < 30 * 60_000;

  let meta: React.ReactNode;
  if (ended) {
    // "Success." (the control plane's reason for a terminate) says nothing; when it ended does.
    const reason = endReason(sb.stateReason);
    meta = (
      <>
        {sb.terminatedAt ? <span>ended {fmtAgo(sb.terminatedAt, now)}</span> : null}
        {reason ? <span>{reason}</span> : null}
      </>
    );
  }
  else if (shown === 'PENDING') meta = <span>Starting, about 2 s</span>;
  else if (shown === 'RESUMING') meta = <span>Resuming, about a second</span>;
  else {
    meta = (
      <>
        <span className={lowTime ? 'meta-warn' : undefined}>{left !== null ? `${fmtDuration(left)} left` : '8 h max'}</span>
        {attachedHere ? (
          <span className="meta-live">attached</span>
        ) : sb.attachedAnywhere && sb.state === 'RUNNING' ? (
          <span>attached elsewhere</span>
        ) : idleAt !== null ? (
          <span className="tabular" title="Estimated time until idle suspend">
            idle in {fmtClock(idleAt - now)}
          </span>
        ) : null}
      </>
    );
  }

  return (
    <li>
      <button
        type="button"
        className={`sb-row${selected ? ' is-selected' : ''}${ended ? ' is-ended' : ''}`}
        aria-current={selected ? 'true' : undefined}
        onClick={() => select(sb.id)}
      >
        <span className="sb-line">
          <StateDot state={pending ? (pending === 'resume' ? 'RESUMING' : pending === 'suspend' ? 'SUSPENDING' : 'TERMINATING') : shown} />
          <span className="sb-name">{sb.name}</span>
          <span className={`sb-state tone-${stateTone(shown)}`}>{STATE_LABEL[shown]}</span>
        </span>
        {!ended ? <LifeBar startedAt={sb.startedAt} maxDurationS={sb.maxDurationS} now={now} tone={stateTone(sb.state)} /> : null}
        <span className="sb-meta">{ended && sb.startedAt ? <span>started {fmtAgo(sb.startedAt, now)}</span> : null}{meta}</span>
      </button>
    </li>
  );
}
