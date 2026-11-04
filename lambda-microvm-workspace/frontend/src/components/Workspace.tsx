import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react';
import { hub } from '../lib/events';
import { useMediaQuery, usePref } from '../lib/hooks';
import { isTransitionalState, loadInitial, refreshQuota, refreshSandboxes, useStore, wireLifecycle, wireRoute } from '../lib/store';
import { Toasts } from './ui/primitives';
import { Sidebar } from './Sidebar';
import { Workbench } from './Workbench';
import { RightPanel } from './RightPanel';
import { useSandboxLive } from '../lib/live';
import { EmptyWorkbench } from './EmptyWorkbench';
import { PolicyPage } from './policy/PolicyPage';

const PANEL_MIN = 320;
const PANEL_MAX = 680;

function defaultPanelWidth() {
  return Math.round(Math.min(560, Math.max(380, window.innerWidth * 0.29)));
}

export function Workspace() {
  const sandboxes = useStore((s) => s.sandboxes);
  const selectedId = useStore((s) => s.selectedId);
  const selected = sandboxes?.find((s) => s.id === selectedId) ?? null;
  const live = useSandboxLive(selected);
  const policy = useStore((s) => s.view === 'policy');

  const narrow = useMediaQuery('(max-width: 1099px)');
  const tiny = useMediaQuery('(max-width: 759px)');
  const [panelPref, setPanelPref] = usePref<number | null>('ws.panelWidth.v1', null);
  const [panelOpenWide, setPanelOpenWide] = usePref<boolean>('ws.panelOpen.v1', true);
  const [panelOpenNarrow, setPanelOpenNarrow] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const [panelWidth, setPanelWidth] = useState(() => panelPref ?? defaultPanelWidth());
  const panelOpen = narrow ? panelOpenNarrow : panelOpenWide;
  const setPanelOpen = narrow ? setPanelOpenNarrow : setPanelOpenWide;

  useEffect(() => {
    wireLifecycle();
    wireRoute();
    hub.start();
    void loadInitial();
    const list = window.setInterval(() => {
      if (!document.hidden) void refreshSandboxes();
    }, 10_000);
    const quota = window.setInterval(() => {
      if (!document.hidden) void refreshQuota();
    }, 20_000);
    return () => {
      window.clearInterval(list);
      window.clearInterval(quota);
    };
  }, []);

  // While any sandbox is mid-transition, poll the list (control plane only, never the VM) every second
  // so pills and overlays settle as soon as the state does.
  const transitioning = useStore(
    (s) => Boolean(s.sandboxes?.some((sb) => isTransitionalState(sb.state))) || Object.values(s.pending).some(Boolean),
  );
  useEffect(() => {
    if (!transitioning) return;
    const t = window.setInterval(() => void refreshSandboxes(), 1000);
    return () => window.clearInterval(t);
  }, [transitioning]);

  // Close the nav drawer after picking a sandbox on small screens.
  useEffect(() => {
    setNavOpen(false);
  }, [selectedId]);

  // ---- panel splitter
  const dragging = useRef(false);
  const onSplitterDown = useCallback(
    (e: React.PointerEvent) => {
      dragging.current = true;
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      document.body.classList.add('resizing');
    },
    [],
  );
  const onSplitterMove = useCallback((e: React.PointerEvent) => {
    if (!dragging.current) return;
    const w = Math.min(PANEL_MAX, Math.max(PANEL_MIN, window.innerWidth - e.clientX));
    setPanelWidth(w);
  }, []);
  const onSplitterUp = useCallback(() => {
    if (!dragging.current) return;
    dragging.current = false;
    document.body.classList.remove('resizing');
    setPanelPref(panelWidth);
  }, [panelWidth, setPanelPref]);
  const onSplitterKey = useCallback(
    (e: React.KeyboardEvent) => {
      const step = e.shiftKey ? 40 : 16;
      let w = panelWidth;
      if (e.key === 'ArrowLeft') w += step;
      else if (e.key === 'ArrowRight') w -= step;
      else return;
      e.preventDefault();
      w = Math.min(PANEL_MAX, Math.max(PANEL_MIN, w));
      setPanelWidth(w);
      setPanelPref(w);
    },
    [panelWidth, setPanelPref],
  );

  const style = { '--panel-w': `${panelWidth}px` } as CSSProperties;

  return (
    <div
      className="app"
      style={style}
      data-panel={panelOpen ? 'open' : 'closed'}
      data-layout={tiny ? 'tiny' : narrow ? 'narrow' : 'wide'}
      data-nav={navOpen ? 'open' : 'closed'}
      data-view={policy ? 'policy' : 'terminal'}
    >
      <Sidebar onClose={() => setNavOpen(false)} />
      {tiny && navOpen ? <div className="scrim" onClick={() => setNavOpen(false)} aria-hidden /> : null}

      <main className="main" aria-label={policy ? 'Proxy policy' : 'Terminal'}>
        {/* The terminal stays mounted (hidden) while the policy page is open, so its WebSocket and tmux
            attachment survive the detour. Its ResizeObserver skips fits while it has no size. */}
        {selected ? (
          <div className="main-slot" hidden={policy}>
            <Workbench
              key={selected.id}
              sandbox={selected}
              pane={live.vitals.data?.pane ?? null}
              paneAt={live.vitals.data?.pane ? live.vitals.updatedAt : null}
              panelOpen={panelOpen}
              onTogglePanel={() => setPanelOpen(!panelOpen)}
              onOpenNav={tiny ? () => setNavOpen(true) : undefined}
            />
          </div>
        ) : policy ? null : (
          <EmptyWorkbench
            loading={sandboxes === null}
            panelOpen={panelOpen}
            onTogglePanel={() => setPanelOpen(!panelOpen)}
            onOpenNav={tiny ? () => setNavOpen(true) : undefined}
          />
        )}
        {policy ? <PolicyPage onOpenNav={tiny ? () => setNavOpen(true) : undefined} /> : null}
      </main>

      {!narrow && panelOpen && !policy ? (
        <div
          className="splitter"
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize side panel"
          aria-valuemin={PANEL_MIN}
          aria-valuemax={PANEL_MAX}
          aria-valuenow={panelWidth}
          tabIndex={0}
          onPointerDown={onSplitterDown}
          onPointerMove={onSplitterMove}
          onPointerUp={onSplitterUp}
          onPointerCancel={onSplitterUp}
          onKeyDown={onSplitterKey}
          onDoubleClick={() => {
            const w = defaultPanelWidth();
            setPanelWidth(w);
            setPanelPref(w);
          }}
        />
      ) : null}

      {panelOpen && !policy ? (
        <>
          {narrow ? <div className="scrim scrim-panel" onClick={() => setPanelOpen(false)} aria-hidden /> : null}
          <RightPanel sandbox={selected} live={live} onClose={narrow ? () => setPanelOpen(false) : undefined} />
        </>
      ) : null}

      <Toasts />
    </div>
  );
}
