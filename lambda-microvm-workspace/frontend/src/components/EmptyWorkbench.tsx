import { Menu, PanelRightClose, PanelRightOpen, Plus } from 'lucide-react';
import { setCreating, useStore } from '../lib/store';
import { Button, Empty } from './ui/primitives';

export function EmptyWorkbench({
  loading,
  panelOpen,
  onTogglePanel,
  onOpenNav,
}: {
  loading: boolean;
  panelOpen: boolean;
  onTogglePanel: () => void;
  onOpenNav?: () => void;
}) {
  const creating = useStore((s) => s.creating);
  return (
    <section className="workbench" aria-label="No sandbox selected">
      <div className="toolbar">
        {onOpenNav ? (
          <Button variant="ghost" size="sm" iconOnly icon={<Menu size={16} aria-hidden />} aria-label="Show sandboxes" onClick={onOpenNav} />
        ) : null}
        <div className="toolbar-id">
          <h1 className="toolbar-name">{loading ? ' ' : 'No sandbox selected'}</h1>
        </div>
        <div className="toolbar-actions">
          <Button
            size="sm"
            variant="ghost"
            iconOnly
            icon={panelOpen ? <PanelRightClose size={16} aria-hidden /> : <PanelRightOpen size={16} aria-hidden />}
            onClick={onTogglePanel}
            aria-label={panelOpen ? 'Hide side panel' : 'Show side panel'}
            aria-pressed={panelOpen}
          />
        </div>
      </div>
      <div className="term-wrap term-empty">
        {loading ? null : (
          <Empty
            title="Start with a sandbox"
            action={
              creating ? null : (
                <Button
                  variant="primary"
                  icon={<Plus size={14} aria-hidden />}
                  onClick={() => {
                    setCreating(true);
                    onOpenNav?.();
                  }}
                >
                  New sandbox
                </Button>
              )
            }
          >
            <p>
              A sandbox is a Lambda MicroVM with Claude Code, tmux and git ready in <code>/root/work</code>. Its terminal
              opens here. API keys inside it are placeholders: iron-proxy swaps in the real ones on the way out, and the
              panel on the right shows every swap.
            </p>
          </Empty>
        )}
      </div>
    </section>
  );
}
