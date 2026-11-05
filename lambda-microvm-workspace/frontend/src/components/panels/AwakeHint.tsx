import { Sun } from 'lucide-react';

/** Quiet reminder that polling the in-VM agent keeps the sandbox from idling. */
export function AwakeHint({ what, label = 'Live stats keep this sandbox awake' }: { what: string; label?: string }) {
  return (
    <span className="awake-hint" title={`${what} while attached. Each read goes to the sandbox, which keeps it awake and stops idle suspend.`}>
      <Sun size={12} aria-hidden />
      {label}
    </span>
  );
}
