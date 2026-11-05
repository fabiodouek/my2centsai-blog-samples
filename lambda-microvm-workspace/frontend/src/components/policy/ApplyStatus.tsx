import { AlertTriangle, CheckCircle2, LoaderCircle } from 'lucide-react';
import { fmtShortTime } from '../../lib/format';
import { useNow } from '../../lib/hooks';
import { isPending, nextCheckIn, usePolicy } from '../../lib/policy';

/** 2.3 s, 17 s */
export function fmtSeconds(s: number): string {
  return s < 10 ? `${s.toFixed(1)} s` : `${Math.round(s)} s`;
}

/**
 * One line that says whether the proxy runs the latest policy: waiting for the sidecar's next check (spinner,
 * countdown), applied (time since the change, session, expiry), rejected by iron-proxy, or a sidecar that stopped
 * reporting. `live` makes it the page's aria-live region; the copy next to the AWS Apply button is a quiet duplicate.
 */
export function ApplyStatus({ live = true, className }: { live?: boolean; className?: string }) {
  const status = usePolicy((s) => s.status);
  const statusError = usePolicy((s) => s.statusError);
  const target = usePolicy((s) => s.target);
  const now = useNow();
  const pending = isPending(status, target);

  let tone = 'is-muted';
  let icon: React.ReactNode = null;
  let text: React.ReactNode = null;

  if (!status && statusError) {
    tone = 'is-deny';
    icon = <AlertTriangle size={13} aria-hidden />;
    text = <>Couldn’t read the apply status: {statusError}</>;
  } else if (!status) {
    text = <>Reading the apply status…</>;
  } else if (status.reload.ok === false && !pending) {
    tone = 'is-deny';
    icon = <AlertTriangle size={13} aria-hidden />;
    text = (
      <>
        iron-proxy rejected config v{status.target.configVersion ?? status.config.version ?? '?'}
        {status.reload.error ? `: ${status.reload.error}` : ''}. It keeps the previous config.
      </>
    );
  } else if (status.scope.error && !pending) {
    tone = 'is-deny';
    icon = <AlertTriangle size={13} aria-hidden />;
    text = <>Couldn’t start the scoped AWS session: {status.scope.error}</>;
  } else if (status.sidecar.stale) {
    tone = 'is-deny';
    icon = <AlertTriangle size={13} aria-hidden />;
    const age = status.sidecar.heartbeatAgeS;
    text = (
      <>
        The proxy’s control sidecar hasn’t reported{age !== null ? ` for ${Math.round(age)} s` : ''}; changes may not
        apply.
      </>
    );
  } else if (pending) {
    tone = 'is-pending';
    icon = <LoaderCircle className="spin" size={13} aria-hidden />;
    const cfg = target?.configVersion ?? status.target.configVersion;
    const scope = target?.scopeVersion ?? status.target.scopeVersion;
    const scopeChanged = scope !== null && scope !== status.scope.version;
    const since = target?.requestedAt ?? status.target.requestedAt;
    const left = nextCheckIn(status, now);
    // The aria-live text changes only with the state, not every second: the countdown is aria-hidden
    text = (
      <>
        Config v{cfg ?? '?'}
        {scopeChanged ? ` and AWS scope v${scope} go` : ' goes'} live at the proxy’s next check
        <span className="apply-faint tabular" aria-hidden>
          {left === null ? '' : left >= 1 ? ` · in ${Math.ceil(left)} s` : ' · due now'}
          {since ? ` · ${Math.max(0, Math.round((now - since) / 1000))} s since the change` : ''}
        </span>
      </>
    );
  } else if (status.applied) {
    tone = 'is-ok';
    icon = <CheckCircle2 size={13} aria-hidden />;
    const parts: string[] = [];
    parts.push(status.latencyS !== null ? `Applied ${fmtSeconds(status.latencyS)} after the change` : 'Applied');
    if (status.scope.role) parts.push(status.scope.role);
    if (status.scope.sessionName) parts.push(`session ${status.scope.sessionName}`);
    if (status.scope.sessionPolicy !== null) parts.push(status.scope.sessionPolicy ? 'session policy' : 'no session policy');
    if (status.scope.sessionTokenUtilization !== null) parts.push(`token ${status.scope.sessionTokenUtilization}%`);
    else if (status.scope.packedPolicySize !== null) parts.push(`packed ${status.scope.packedPolicySize}%`);
    if (status.scope.expiration) parts.push(`expires ${fmtShortTime(status.scope.expiration)}`);
    text = (
      <>
        <span className="tabular">{parts.join(' · ')}</span>
        <span className="apply-faint tabular">
          {' '}
          · config v{status.config.version ?? '?'}, scope v{status.scope.version ?? '?'}
        </span>
      </>
    );
  } else {
    text = <>Waiting for the proxy to report</>;
  }

  return (
    <p
      className={`apply-status ${tone}${className ? ` ${className}` : ''}`}
      aria-live={live ? 'polite' : undefined}
      aria-hidden={live ? undefined : true}
      data-testid={live ? 'apply-status' : undefined}
      title={
        status
          ? [
              status.sidecar.pollS ? `The proxy checks for changes every ${status.sidecar.pollS} s.` : null,
              status.sidecar.task ? `Reported by proxy task ${status.sidecar.task}.` : null,
            ]
              .filter(Boolean)
              .join(' ') || undefined
          : undefined
      }
    >
      {icon}
      <span className="apply-text">{text}</span>
    </p>
  );
}
