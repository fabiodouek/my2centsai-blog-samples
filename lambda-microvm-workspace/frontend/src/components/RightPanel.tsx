import { useRef } from 'react';
import { X } from 'lucide-react';
import { usePref } from '../lib/hooks';
import type { SandboxLive } from '../lib/live';
import type { Sandbox } from '../lib/types';
import { ProxyPanel } from './panels/ProxyPanel';
import { CredentialsPanel } from './panels/CredentialsPanel';
import { ClaudePanel } from './panels/ClaudePanel';
import { VitalsPanel } from './panels/VitalsPanel';
import { DnsPanel } from './panels/DnsPanel';

const TABS = [
  { id: 'proxy', label: 'Proxy' },
  { id: 'credentials', label: 'Credentials' },
  { id: 'claude', label: 'Claude Code' },
  { id: 'vitals', label: 'Vitals' },
  { id: 'dns', label: 'DNS' },
] as const;
type TabId = (typeof TABS)[number]['id'];

export function RightPanel({ sandbox, live, onClose }: { sandbox: Sandbox | null; live: SandboxLive; onClose?: () => void }) {
  const [tab, setTab] = usePref<TabId>('ws.tab.v1', 'proxy');
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const active = TABS.some((t) => t.id === tab) ? tab : 'proxy';

  function onKey(e: React.KeyboardEvent) {
    const i = TABS.findIndex((t) => t.id === active);
    let next = -1;
    if (e.key === 'ArrowRight') next = (i + 1) % TABS.length;
    else if (e.key === 'ArrowLeft') next = (i - 1 + TABS.length) % TABS.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = TABS.length - 1;
    if (next < 0) return;
    e.preventDefault();
    setTab(TABS[next].id);
    refs.current[TABS[next].id]?.focus();
  }

  return (
    <aside className="panel" aria-label="Activity and details">
      <div className="tabs" role="tablist" aria-label="Panels" onKeyDown={onKey}>
        {TABS.map((t) => (
          <button
            key={t.id}
            ref={(el) => {
              refs.current[t.id] = el;
            }}
            type="button"
            role="tab"
            id={`tab-${t.id}`}
            aria-selected={active === t.id}
            aria-controls={`tabpanel-${t.id}`}
            tabIndex={active === t.id ? 0 : -1}
            className="tab"
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
        {onClose ? (
          <button type="button" className="panel-close" aria-label="Close panel" onClick={onClose}>
            <X size={16} aria-hidden />
          </button>
        ) : null}
      </div>
      <div className="tabpanel" role="tabpanel" id={`tabpanel-${active}`} aria-labelledby={`tab-${active}`}>
        {active === 'proxy' ? <ProxyPanel /> : null}
        {active === 'credentials' ? <CredentialsPanel /> : null}
        {active === 'claude' ? <ClaudePanel sandbox={sandbox} live={live} /> : null}
        {active === 'vitals' ? <VitalsPanel sandbox={sandbox} live={live} /> : null}
        {active === 'dns' ? <DnsPanel /> : null}
      </div>
    </aside>
  );
}
