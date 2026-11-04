import { useEffect, useState, type ReactNode } from 'react';
import { CircleAlert, LoaderCircle, Moon, Play, Plug, Power, RotateCw, SquareTerminal, WifiOff } from 'lucide-react';
import { endReason } from '../lib/format';
import type { TermStatus } from '../lib/terminalConnection';
import type { Sandbox } from '../lib/types';
import { Button, shownState } from './ui/primitives';

interface Props {
  status: TermStatus;
  sandbox: Sandbox;
  now: number;
  busy: boolean;
  /** Set while a resume this browser started is in flight. */
  resumeAt: number | null;
  onResume: () => void;
  onAttach: () => void;
  onRetry: () => void;
}

/** Connection state over the terminal. Nothing renders while attached. */
export function TerminalOverlay({ status, sandbox, now, busy, resumeAt, onResume, onAttach, onRetry }: Props) {
  const quiet = status.state === 'suspended' || status.state === 'connecting' || status.state === 'idle' || status.state === 'detached';
  if (resumeAt !== null && quiet) {
    return <ResumeProgress since={resumeAt} />;
  }
  const view = describe(status, sandbox, now, busy, { onResume, onAttach, onRetry });
  if (!view) return null;
  return (
    <div className={`overlay overlay-${view.tone}`} role="status" aria-live="polite">
      <div className="overlay-card">
        <div className="overlay-icon" aria-hidden>
          {view.icon}
        </div>
        <div className="overlay-text">
          <p className="overlay-title">{view.title}</p>
          {view.body ? <p className="overlay-body">{view.body}</p> : null}
        </div>
        {view.action ? <div className="overlay-action">{view.action}</div> : null}
      </div>
    </div>
  );
}

/** Compact progress while a resume runs: resuming takes about a second, so show the clock, not prose. */
function ResumeProgress({ since }: { since: number }) {
  const [t, setT] = useState(() => Date.now());
  useEffect(() => {
    const i = window.setInterval(() => setT(Date.now()), 100);
    return () => window.clearInterval(i);
  }, []);
  const secs = Math.max(0, (t - since) / 1000);
  return (
    <div className="overlay overlay-sleep" role="status" aria-live="polite">
      <div className="overlay-pill">
        <LoaderCircle size={14} className="spin" aria-hidden />
        <span>Resuming…</span>
        <span className="overlay-pill-time tabular" aria-hidden>
          {secs.toFixed(1)} s
        </span>
      </div>
    </div>
  );
}

interface View {
  tone: 'neutral' | 'sleep' | 'deny';
  icon: ReactNode;
  title: string;
  body?: ReactNode;
  action?: ReactNode;
}

function describe(
  status: TermStatus,
  sb: Sandbox,
  now: number,
  busy: boolean,
  a: { onResume: () => void; onAttach: () => void; onRetry: () => void },
): View | null {
  const spin = <LoaderCircle size={18} className="spin" />;
  switch (status.state) {
    case 'attached':
      return null;
    case 'idle':
    case 'connecting':
      return {
        tone: 'neutral',
        icon: spin,
        title: shownState(sb) === 'PENDING' ? 'Starting the sandbox' : shownState(sb) === 'RESUMING' ? 'Resuming the sandbox' : 'Connecting',
        body: status.detail || 'Opening a shell in the sandbox',
      };
    case 'reconnecting': {
      const secs = status.retryAt ? Math.max(0, Math.ceil((status.retryAt - now) / 1000)) : null;
      const givesUp = status.givesUpAt ? Math.max(0, Math.ceil((status.givesUpAt - now) / 1000)) : null;
      const byServer = status.retryBy === 'server';
      return {
        tone: 'neutral',
        icon: secs === 0 ? spin : <WifiOff size={18} />,
        title: secs ? `Reconnecting in ${secs} s` : 'Reconnecting',
        body: (
          <>
            {status.detail || (byServer ? 'The workspace lost the sandbox’s shell and is retrying.' : 'Lost the connection to the workspace.')}
            {byServer && givesUp ? ` It keeps trying for another ${givesUp} s.` : ''}
            {status.attempt ? <span className="overlay-faint"> Attempt {status.attempt}.</span> : null}
          </>
        ),
        action: byServer ? undefined : (
          <Button size="sm" icon={<RotateCw size={14} aria-hidden />} onClick={a.onRetry}>
            Retry now
          </Button>
        ),
      };
    }
    case 'suspended':
      return {
        tone: 'sleep',
        icon: <Moon size={18} />,
        title: 'Sandbox suspended',
        body: (
          <>
            Nothing is lost. Resume takes about a second and brings back the same screen, with tmux and anything
            running in it (Claude Code too) where it stopped.
            <span className="overlay-faint"> Memory and disk are kept while suspended.</span>
          </>
        ),
        action: (
          <Button variant="primary" size="sm" icon={<Play size={14} aria-hidden />} loading={busy} onClick={a.onResume} autoFocus>
            Resume
          </Button>
        ),
      };
    case 'detached':
      return {
        tone: 'neutral',
        icon: <Plug size={18} />,
        title: 'Detached',
        body:
          sb.state === 'RUNNING'
            ? `tmux keeps your session. With no terminal attached, the sandbox suspends after ${sb.idleMinutes} min without traffic.`
            : 'tmux keeps your session.',
        action: (
          <Button variant="primary" size="sm" icon={<Plug size={14} aria-hidden />} onClick={a.onAttach}>
            Attach
          </Button>
        ),
      };
    case 'exited':
      return {
        tone: 'neutral',
        icon: <SquareTerminal size={18} />,
        title: 'Shell exited',
        body: status.detail && !/shell exited/i.test(status.detail) ? status.detail : 'The tmux session ended. A new shell starts a fresh session.',
        action:
          sb.state === 'RUNNING' ? (
            <Button variant="primary" size="sm" icon={<SquareTerminal size={14} aria-hidden />} onClick={a.onAttach}>
              Start a new shell
            </Button>
          ) : undefined,
      };
    case 'terminated':
      return {
        tone: 'neutral',
        icon: <Power size={18} />,
        title: 'Sandbox terminated',
        body: `${endReason(sb.stateReason) ? `${endReason(sb.stateReason)}. ` : ''}It stays in the list for an hour, then disappears.`,
      };
    case 'error':
      return {
        tone: 'deny',
        icon: <CircleAlert size={18} />,
        title: 'Can’t open the terminal',
        body: status.detail,
        action: (
          <Button size="sm" icon={<RotateCw size={14} aria-hidden />} onClick={a.onRetry}>
            Try again
          </Button>
        ),
      };
  }
}
