import { useEffect, useRef } from 'react';
import { Menu, RefreshCw, ShieldCheck, SquareTerminal } from 'lucide-react';
import { holdStatus, refreshOverview, usePolicy } from '../../lib/policy';
import { closePolicy, POLICY_SECTIONS, setPolicySection, useStore, type PolicySection } from '../../lib/store';
import { Button, Empty, Note, SkeletonRows } from '../ui/primitives';
import { ApplyStatus } from './ApplyStatus';
import { AllowlistSection } from './AllowlistSection';
import { CredentialsSection } from './CredentialsSection';
import { AwsSection } from './AwsSection';
import { ChangeLogSection } from './ChangeLogSection';
import { ConfigSection } from './ConfigSection';

const LABEL: Record<PolicySection, string> = {
  allowlist: 'Allowlist',
  credentials: 'Credentials',
  aws: 'AWS access',
  changes: 'Change log',
  config: 'Config',
};

/** The global proxy policy (allowlist, credentials, AWS access, change log; for admins, the config). Not per sandbox. */
export function PolicyPage({ onOpenNav }: { onOpenNav?: () => void }) {
  const chosen = useStore((s) => s.policySection);
  const me = useStore((s) => s.me);
  const pollS = usePolicy((s) => s.status?.sidecar.pollS ?? null);
  const overview = usePolicy((s) => s.overview);
  const overviewError = usePolicy((s) => s.overviewError);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});

  useEffect(() => {
    void refreshOverview();
    headingRef.current?.focus();
  }, []);
  useEffect(() => (overview?.enabled ? holdStatus() : undefined), [overview?.enabled]);

  const editable = Boolean(overview?.editable ?? me?.isAdmin);
  const sections = editable ? POLICY_SECTIONS : POLICY_SECTIONS.filter((s) => s !== 'config');
  const section = sections.includes(chosen) ? chosen : 'allowlist';

  function onTabKey(e: React.KeyboardEvent) {
    const i = sections.indexOf(section);
    let next = -1;
    if (e.key === 'ArrowRight') next = (i + 1) % sections.length;
    else if (e.key === 'ArrowLeft') next = (i - 1 + sections.length) % sections.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = sections.length - 1;
    if (next < 0) return;
    e.preventDefault();
    setPolicySection(sections[next]);
    tabRefs.current[sections[next]]?.focus();
  }

  let body: React.ReactNode;
  if (!overview && overviewError) {
    body = (
      <Empty
        title="Couldn’t load the proxy policy"
        action={
          <Button size="sm" icon={<RefreshCw size={14} aria-hidden />} onClick={() => void refreshOverview()}>
            Retry
          </Button>
        }
      >
        {overviewError}
      </Empty>
    );
  } else if (!overview) {
    body = (
      <div className="policy-body">
        <SkeletonRows rows={6} />
      </div>
    );
  } else if (!overview.enabled) {
    body = (
      <Empty icon={<ShieldCheck size={16} aria-hidden />} title="The proxy control plane is off">
        {overview.reason ?? 'Apply part 2 with control_plane=true to manage the proxy policy from here.'}
      </Empty>
    );
  } else {
    body = (
      <div className="policy-body" role="tabpanel" id={`policy-panel-${section}`} aria-labelledby={`policy-tab-${section}`}>
        <Note>
          Changes apply to every sandbox on this proxy, other users’ included, at the proxy’s next check for changes
          (every {pollS ?? 30}&nbsp;s), when iron-proxy reloads its config without a restart.
          {editable ? '' : ' Only admins can change the policy; you can look.'}
        </Note>
        {section === 'allowlist' ? <AllowlistSection editable={editable} /> : null}
        {section === 'credentials' ? <CredentialsSection editable={editable} /> : null}
        {section === 'aws' ? <AwsSection editable={editable} /> : null}
        {section === 'changes' ? <ChangeLogSection /> : null}
        {section === 'config' && editable ? <ConfigSection /> : null}
      </div>
    );
  }

  return (
    <section className="policy" aria-labelledby="policy-title">
      <div className="toolbar">
        {onOpenNav ? (
          <Button variant="ghost" size="sm" iconOnly icon={<Menu size={16} aria-hidden />} aria-label="Show sandboxes" onClick={onOpenNav} />
        ) : null}
        <div className="toolbar-id">
          <ShieldCheck size={16} className="policy-title-icon" aria-hidden />
          <h1 id="policy-title" className="toolbar-name" tabIndex={-1} ref={headingRef}>
            Proxy policy
          </h1>
          <span className="toolbar-meta">iron-proxy · every sandbox{editable ? '' : ' · read-only'}</span>
        </div>
        <div className="toolbar-actions">
          <Button size="sm" variant="ghost" icon={<SquareTerminal size={14} aria-hidden />} onClick={closePolicy}>
            Back to terminal
          </Button>
        </div>
      </div>
      {overview?.enabled ? (
        <div className="policy-tabs">
          <div className="tabs policy-tablist" role="tablist" aria-label="Proxy policy sections" onKeyDown={onTabKey}>
            {sections.map((id) => (
              <button
                key={id}
                ref={(el) => {
                  tabRefs.current[id] = el;
                }}
                type="button"
                role="tab"
                id={`policy-tab-${id}`}
                aria-selected={section === id}
                aria-controls={`policy-panel-${id}`}
                tabIndex={section === id ? 0 : -1}
                className="tab"
                onClick={() => setPolicySection(id)}
              >
                {LABEL[id]}
              </button>
            ))}
          </div>
          <ApplyStatus />
        </div>
      ) : null}
      <div className="policy-scroll">{body}</div>
    </section>
  );
}
