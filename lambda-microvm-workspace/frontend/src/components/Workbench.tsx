import { useCallback, useEffect, useRef, useState } from 'react';
import { Terminal, type ITheme } from '@xterm/xterm';
import { FitAddon } from '@xterm/addon-fit';
import { WebLinksAddon } from '@xterm/addon-web-links';
import { Copy, Menu, Moon, PanelRightClose, PanelRightOpen, Play, Plug, Power, SquareTerminal, Unplug } from 'lucide-react';
import { api } from '../lib/api';
import { TerminalConnection, type TermStatus } from '../lib/terminalConnection';
import { getState, observeState, runAction, setTerminalLink, toast, useStore } from '../lib/store';
import { fmtDuration } from '../lib/format';
import { useNow } from '../lib/hooks';
import type { Pane, Sandbox } from '../lib/types';
import { Button, ConfirmDialog, StatePill, shownState } from './ui/primitives';
import { TerminalOverlay } from './TerminalOverlay';

const THEME: ITheme = {
  background: '#0e0c09',
  foreground: '#e4e1da',
  cursor: '#e1b761',
  cursorAccent: '#0e0c09',
  selectionBackground: 'rgba(225, 183, 97, 0.30)',
  selectionInactiveBackground: 'rgba(225, 183, 97, 0.16)',
  scrollbarSliderBackground: 'rgba(184, 179, 172, 0.16)',
  scrollbarSliderHoverBackground: 'rgba(184, 179, 172, 0.28)',
  scrollbarSliderActiveBackground: 'rgba(225, 183, 97, 0.4)',
  black: '#1a1713',
  red: '#e66f64',
  green: '#69c898',
  yellow: '#e1b761',
  blue: '#7fa6e8',
  magenta: '#c792d9',
  cyan: '#6cc4c4',
  white: '#d6d2ca',
  brightBlack: '#6b665f',
  brightRed: '#f08a80',
  brightGreen: '#8adbb0',
  brightYellow: '#ecc77a',
  brightBlue: '#9dbcf0',
  brightMagenta: '#d9aee6',
  brightCyan: '#8fd8d8',
  brightWhite: '#f7f5f1',
};

const IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform);

/** A pane sample older than this doesn't block the button: the click checks the pane again anyway. */
const PANE_FRESH_MS = 12_000;
/** After typing `claude`, how long to look for it in the pane before giving up (and re-enabling the button). */
const CLAUDE_WATCH_MS = 15_000;
const CLAUDE_POLL_MS = 1_500;
const sleep = (ms: number) => new Promise((r) => window.setTimeout(r, ms));

/**
 * Why Start Claude Code is off right now, or null when it can be pressed. Only a recent pane sample that shows Claude
 * Code running blocks it; anything else is checked on click, so a stale sample can never leave it stuck.
 */
function startClaudeBlocker(attached: boolean, pane: Pane | null, paneAt: number | null, starting: boolean, now: number): string | null {
  if (!attached) return 'Attach a terminal first';
  if (starting) return 'Starting Claude Code…';
  if (pane && paneAt && now - paneAt < PANE_FRESH_MS && pane.claudeRunning) return 'Claude Code is already running';
  return null;
}

export function Workbench({
  sandbox,
  pane,
  paneAt,
  panelOpen,
  onTogglePanel,
  onOpenNav,
}: {
  sandbox: Sandbox;
  /** Latest live sample of the terminal's tmux pane (vitals?live=1, every 5 s while attached). */
  pane: Pane | null;
  paneAt: number | null;
  panelOpen: boolean;
  onTogglePanel: () => void;
  onOpenNav?: () => void;
}) {
  const id = sandbox.id;
  const hostRef = useRef<HTMLDivElement>(null);
  const termRef = useRef<Terminal | null>(null);
  const connRef = useRef<TerminalConnection | null>(null);
  const resumeRequested = useRef(false);
  /** A suspend this browser asked for: trust the polled SUSPENDED over the still-open socket. */
  const suspendRequested = useRef(false);
  const stateRef = useRef(sandbox.state);
  stateRef.current = sandbox.state;

  const [status, setStatus] = useState<TermStatus>({ state: 'idle', detail: '', retryAt: null, retryBy: null });
  const [hasSelection, setHasSelection] = useState(false);
  const [confirmTerminate, setConfirmTerminate] = useState(false);
  /** When the user pressed Resume; drives the compact "Resuming… 0.8 s" overlay until attached. */
  const [resumeAt, setResumeAt] = useState<number | null>(null);
  /** Start Claude Code is checking the pane, typing, or watching for Claude to show up (always ends). */
  const [starting, setStarting] = useState(false);
  const startingRef = useRef(false);
  const pending = useStore((s) => s.pending[id]);
  const now = useNow();

  // ---- xterm + connection, one pair per sandbox (the component is keyed by id)
  useEffect(() => {
    const host = hostRef.current!;
    const term = new Terminal({
      fontFamily: 'ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "DejaVu Sans Mono", monospace',
      fontSize: 13,
      lineHeight: 1.18,
      cursorBlink: true,
      cursorStyle: 'block',
      cursorInactiveStyle: 'outline',
      scrollback: 5000,
      macOptionIsMeta: true,
      rightClickSelectsWord: true,
      allowProposedApi: false,
      theme: THEME,
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.loadAddon(new WebLinksAddon());
    term.open(host);
    termRef.current = term;
    try {
      fit.fit();
    } catch {
      /* not laid out yet */
    }

    const conn = new TerminalConnection(id, {
      onOutput: (d) => term.write(d),
      onStatus: (s) => {
        setStatus(s);
        // The terminal is the fastest witness of the VM's state; let it settle the pill.
        if (s.state === 'attached') observeState(id, 'RUNNING');
        else if (s.state === 'suspended') observeState(id, 'SUSPENDED');
        if (s.state === 'attached' || s.state === 'error' || s.state === 'terminated' || s.state === 'exited') setResumeAt(null);
        if (s.state === 'attached') {
          const active = document.activeElement;
          if (!active || active === document.body || host.contains(active) || active.closest('.toolbar, .overlay')) term.focus();
        }
      },
      getSize: () => ({ rows: term.rows, cols: term.cols }),
    });
    connRef.current = conn;

    const subs = [
      term.onData((d) => conn.send(d)),
      term.onBinary((d) => conn.send(Uint8Array.from(d, (c) => c.charCodeAt(0) & 0xff))),
      term.onResize(({ rows, cols }) => conn.resize(rows, cols)),
      term.onSelectionChange(() => setHasSelection(term.hasSelection())),
    ];

    // Copy/paste: Cmd+C / Cmd+V work natively on macOS. Elsewhere Ctrl+C must stay SIGINT,
    // so Ctrl+Shift+C / Ctrl+Shift+V copy and paste.
    term.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown' || IS_MAC || !e.ctrlKey || !e.shiftKey) return true;
      if (e.code === 'KeyC') {
        if (term.hasSelection()) void navigator.clipboard?.writeText(term.getSelection());
        return false;
      }
      if (e.code === 'KeyV') {
        void navigator.clipboard?.readText().then((t) => t && term.paste(t));
        return false;
      }
      return true;
    });

    // Fit on every size change of the terminal's box (window, sidebar, panel splitter).
    let raf = 0;
    const ro = new ResizeObserver(() => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        if (host.clientWidth < 40 || host.clientHeight < 40) return;
        try {
          fit.fit();
        } catch {
          /* ignore transient layout */
        }
      });
    });
    ro.observe(host);

    const st = stateRef.current;
    if (st === 'SUSPENDED' || st === 'SUSPENDING') conn.markSuspended();
    else if (st === 'TERMINATED' || st === 'TERMINATING') conn.markTerminated();
    else if (getState().autoSelected) conn.detach(); // not picked by the user: offer Attach
    else conn.connect(st === 'PENDING' ? 'Waiting for the sandbox to start' : undefined);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      subs.forEach((s) => s.dispose());
      conn.dispose();
      term.dispose();
      connRef.current = null;
      termRef.current = null;
      setTerminalLink(id, false, false);
    };
  }, [id]);

  useEffect(() => {
    const s = status.state;
    setTerminalLink(id, s === 'attached', s === 'attached' || s === 'connecting' || s === 'reconnecting');
  }, [id, status.state]);

  // ---- react to lifecycle changes from /ws/events and the list poll
  useEffect(() => {
    const conn = connRef.current;
    if (!conn) return;
    const st = sandbox.state;
    if (st === 'TERMINATED' || st === 'TERMINATING') {
      if (conn.state !== 'terminated') conn.markTerminated(sandbox.stateReason ?? '');
    } else if (st === 'SUSPENDED' || st === 'SUSPENDING') {
      // A live terminal knows better than a polled state that may lag behind it.
      const s = conn.state;
      const live = s === 'attached' || s === 'connecting';
      if (s !== 'detached' && s !== 'exited' && (!live || (st === 'SUSPENDED' && suspendRequested.current))) {
        conn.markSuspended();
      }
    } else if (st === 'RUNNING' && conn.state === 'suspended') {
      if (conn.isOpen) return; // the backend reattaches this socket by itself
      if (resumeRequested.current) conn.connect('Reattaching tmux');
      else conn.detach(); // resumed elsewhere: offer Attach instead of waking into it silently
    }
    if (st === 'RUNNING') {
      resumeRequested.current = false;
      suspendRequested.current = false;
    }
  }, [sandbox.state, sandbox.stateReason]);

  // ---- actions
  const resume = useCallback(async () => {
    resumeRequested.current = true;
    setResumeAt(Date.now());
    const ok = await runAction(id, 'resume');
    if (!ok) {
      resumeRequested.current = false;
      setResumeAt(null);
      return;
    }
    const conn = connRef.current;
    // With an open socket the backend reattaches on its own; otherwise connecting also resumes.
    if (conn && !conn.isOpen) conn.connect('Resuming the sandbox');
  }, [id]);

  const attach = useCallback(() => {
    if (stateRef.current === 'SUSPENDED') void resume();
    else connRef.current?.connect();
  }, [resume]);

  const detach = useCallback(() => connRef.current?.detach(), []);

  /**
   * Checks what runs in the tmux pane first (a fresh sample from the in-VM agent), refuses if it isn't a shell
   * prompt, clears whatever is typed at the prompt (C-e, C-u), types `claude` and Enter, then watches the pane for up
   * to 15 s. Whatever happens, the button comes back.
   */
  const startClaude = useCallback(async () => {
    if (startingRef.current) return;
    startingRef.current = true;
    setStarting(true);
    try {
      let p: Pane | null = null;
      try {
        p = (await api.vitals(id, true)).pane;
      } catch {
        p = null; // the agent didn't answer: type anyway, and the watch below says if it didn't work
      }
      if (p?.claudeRunning) {
        toast('info', 'Claude Code is already running', 'It’s in the terminal’s tmux pane.');
        return;
      }
      if (p && !p.atShell && p.command) {
        toast('error', `Something is running in the terminal (${p.command})`, 'Exit it first, then press Start Claude Code again.');
        return;
      }
      // C-e C-u: to the end of the line, then delete back to the prompt, so text already typed there doesn't run
      // together with `claude`
      if (!connRef.current?.send('\x05\x15claude\r')) {
        toast('error', 'The terminal isn’t connected', 'Attach it, then try again.');
        return;
      }
      termRef.current?.focus();
      const until = Date.now() + CLAUDE_WATCH_MS;
      let last: Pane | null = p;
      while (Date.now() < until) {
        await sleep(CLAUDE_POLL_MS);
        try {
          last = (await api.vitals(id, true)).pane;
        } catch {
          /* keep watching */
        }
        if (last?.claudeRunning) return;
      }
      toast(
        'error',
        'Claude Code didn’t start',
        last?.command
          ? `After ${CLAUDE_WATCH_MS / 1000} s the terminal still shows ${last.command}. Check it for an error, then try again.`
          : `No sign of it after ${CLAUDE_WATCH_MS / 1000} s. Check the terminal, then try again.`,
      );
    } finally {
      startingRef.current = false;
      setStarting(false);
    }
  }, [id]);

  const copySelection = useCallback(async () => {
    const t = termRef.current;
    if (!t?.hasSelection()) return;
    try {
      await navigator.clipboard.writeText(t.getSelection());
      toast('success', 'Copied selection');
    } catch {
      toast('error', 'Clipboard is blocked', 'Use the keyboard shortcut instead.');
    }
    t.focus();
  }, []);

  const terminate = useCallback(async () => {
    const ok = await runAction(id, 'terminate');
    if (ok) setConfirmTerminate(false);
  }, [id]);

  // ---- derived UI
  const st = sandbox.state;
  const attached = status.state === 'attached';
  const linked = attached || status.state === 'connecting' || status.state === 'reconnecting';
  const ended = st === 'TERMINATED' || st === 'TERMINATING';
  const expiresAt = sandbox.expiresAt ?? (sandbox.startedAt ? sandbox.startedAt + sandbox.maxDurationS * 1000 : null);
  const left = expiresAt ? Math.max(0, expiresAt - now) : null;
  const copyKeys = IS_MAC ? '⌘C / ⌘V' : 'Ctrl+Shift+C / Ctrl+Shift+V';
  const claudeBlocked = startClaudeBlocker(attached, pane, paneAt, starting, now);

  return (
    <section className="workbench" aria-label={`Sandbox ${sandbox.name}`}>
      <div className="toolbar">
        {onOpenNav ? (
          <Button variant="ghost" size="sm" iconOnly icon={<Menu size={16} aria-hidden />} aria-label="Show sandboxes" onClick={onOpenNav} />
        ) : null}
        <div className="toolbar-id">
          <h1 className="toolbar-name" title={`${sandbox.id} · ${sandbox.sizeLabel}, ${sandbox.memoryGb} GB, ${sandbox.vcpu} vCPU`}>
            {sandbox.name}
          </h1>
          <StatePill state={pending === 'resume' ? 'RESUMING' : pending === 'suspend' ? 'SUSPENDING' : shownState(sandbox)} />
          {left !== null && !ended ? (
            <span className={`toolbar-meta tabular${left < 30 * 60_000 ? ' meta-warn' : ''}`} title={expiresAt ? `Stops at ${new Date(expiresAt).toLocaleTimeString()}` : undefined}>
              {fmtDuration(left)} left
            </span>
          ) : null}
        </div>

        <div className="toolbar-actions">
          <Button
            variant="primary"
            size="sm"
            icon={<SquareTerminal size={14} aria-hidden />}
            onClick={claudeBlocked ? undefined : () => void startClaude()}
            loading={starting}
            disabled={!attached}
            // Blocked while attached: aria-disabled keeps the tooltip (and focus) that a disabled button may lose.
            aria-disabled={attached && claudeBlocked ? true : undefined}
            title={
              claudeBlocked ??
              (pane && !pane.atShell && pane.command
                ? `${pane.command} is running in the terminal: exit it first`
                : 'Clears the shell prompt, then types `claude` and Enter')
            }
            className="btn-collapsible"
            aria-label="Start Claude Code"
          >
            Start Claude Code
          </Button>
          <span className="toolbar-sep" aria-hidden />
          {st === 'SUSPENDED' || pending === 'resume' ? (
            <Button
              size="sm"
              icon={<Play size={14} aria-hidden />}
              onClick={() => void resume()}
              loading={pending === 'resume'}
              disabled={resumeAt !== null}
              className="btn-collapsible"
              aria-label="Resume sandbox"
              title="Resume sandbox"
            >
              Resume
            </Button>
          ) : (
            <Button
              size="sm"
              icon={<Moon size={14} aria-hidden />}
              onClick={() => {
                suspendRequested.current = true;
                void runAction(id, 'suspend').then((ok) => {
                  if (!ok) suspendRequested.current = false;
                });
              }}
              disabled={st !== 'RUNNING'}
              loading={pending === 'suspend'}
              className="btn-collapsible"
              aria-label="Suspend"
              title="Suspend now. Memory and disk are kept; open connections close."
            >
              Suspend
            </Button>
          )}
          {linked ? (
            <Button size="sm" variant="ghost" icon={<Unplug size={14} aria-hidden />} onClick={detach} className="btn-collapsible btn-collapse-early" aria-label="Detach" title="Close this terminal. tmux keeps the session, and the sandbox can go idle.">
              Detach
            </Button>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              icon={<Plug size={14} aria-hidden />}
              onClick={attach}
              disabled={ended || st === 'PENDING' || status.state === 'suspended'}
              className="btn-collapsible btn-collapse-early"
              aria-label="Attach"
              title={status.state === 'suspended' ? 'Resume the sandbox to attach' : 'Open a terminal in this sandbox'}
            >
              Attach
            </Button>
          )}
          <Button
            size="sm"
            variant="ghost"
            iconOnly
            icon={<Copy size={14} aria-hidden />}
            onClick={() => void copySelection()}
            disabled={!hasSelection}
            aria-label="Copy selection"
            title={`Copy selection (${copyKeys})`}
          />
          <Button
            size="sm"
            variant="danger"
            icon={<Power size={14} aria-hidden />}
            onClick={() => setConfirmTerminate(true)}
            disabled={ended}
            loading={pending === 'terminate'}
            className="btn-collapsible"
            aria-label="Terminate"
          >
            Terminate
          </Button>
          <span className="toolbar-sep" aria-hidden />
          <Button
            size="sm"
            variant="ghost"
            iconOnly
            icon={panelOpen ? <PanelRightClose size={16} aria-hidden /> : <PanelRightOpen size={16} aria-hidden />}
            onClick={onTogglePanel}
            aria-label={panelOpen ? 'Hide side panel' : 'Show side panel'}
            aria-pressed={panelOpen}
            title={panelOpen ? 'Hide side panel' : 'Show side panel'}
          />
        </div>
      </div>

      <div className={`term-wrap${attached ? '' : ' is-dimmed'}`}>
        <div className="term-host" ref={hostRef} />
        <TerminalOverlay
          status={status}
          sandbox={sandbox}
          now={now}
          busy={pending === 'resume'}
          resumeAt={resumeAt}
          onResume={() => void resume()}
          onAttach={attach}
          onRetry={() => connRef.current?.retryNow()}
        />
      </div>

      <ConfirmDialog
        open={confirmTerminate}
        title={`Terminate ${sandbox.name}?`}
        confirmLabel="Terminate sandbox"
        busy={pending === 'terminate'}
        onConfirm={() => void terminate()}
        onClose={() => setConfirmTerminate(false)}
      >
        <p>
          The MicroVM stops for good. Anything in <code>/root/work</code> that isn’t pushed somewhere is lost. This
          frees {sandbox.memoryGb} GB of the account quota.
        </p>
      </ConfirmDialog>
    </section>
  );
}
