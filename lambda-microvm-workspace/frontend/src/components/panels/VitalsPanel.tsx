import { useMemo } from 'react';
import { Plug } from 'lucide-react';
import { hub, useHub } from '../../lib/events';
import { endReason, fmtDateTime, fmtDuration, fmtLifetime, fmtMb, fmtShortTime, fmtTime } from '../../lib/format';
import { useStore } from '../../lib/store';
import { useNow } from '../../lib/hooks';
import type { SandboxLive } from '../../lib/live';
import type { Sandbox, SandboxState } from '../../lib/types';
import { LifeBar } from '../charts/LifeBar';
import { LineChart } from '../charts/LineChart';
import { Empty, STATE_LABEL, StateDot, StatePill, shownState, stateTone } from '../ui/primitives';
import { AwakeHint } from './AwakeHint';

/** "Terminated after 15 min suspended", "terminated as soon as it suspends", "until the lifetime ends". */
function suspendedText(s: number | null, maxS: number): string {
  if (s === null || s >= maxS) return 'stays suspended until the lifetime ends';
  // Lambda applies suspendedDurationSeconds to idle suspends only (measured): Suspend keeps it until the lifetime ends
  if (s === 0) return 'terminated as soon as it suspends (a Suspend from here keeps it until the lifetime ends)';
  return `terminated after ${fmtLifetime(s)} suspended (after Suspend, it stays until the lifetime ends)`;
}

export function egressText(egress: string): string {
  if (egress === 'proxy') return 'Through iron-proxy (part 2): allowlisted hosts only, keys swapped in';
  if (egress === 'internet') return 'Direct internet (Lambda’s managed connector), not through iron-proxy';
  if (egress === 'none') return 'None';
  return egress;
}

export function VitalsPanel({ sandbox, live }: { sandbox: Sandbox | null; live: SandboxLive }) {
  useHub();
  const now = useNow();
  const config = useStore((s) => s.config);
  if (!sandbox) return <Empty title="No sandbox selected">Pick a sandbox to see its state, timers and live stats.</Empty>;

  const v = live.vitals.data;
  const state = sandbox.state;
  const startedAt = v?.startedAt ?? sandbox.startedAt;
  const maxS = v?.maxDurationS ?? sandbox.maxDurationS;
  const expiresAt = v?.expiresAt ?? sandbox.expiresAt ?? (startedAt ? startedAt + maxS * 1000 : null);
  const left = expiresAt ? Math.max(0, expiresAt - now) : null;
  const idle = v?.idlePolicy;
  const stats = v?.stats ?? null;
  const liveOn = live.attached && state === 'RUNNING';
  const ended = state === 'TERMINATED' || state === 'TERMINATING';
  const latest = live.samples[live.samples.length - 1];
  const memTotal = stats?.memTotalMb ?? latest?.memTotal ?? 0;
  const baselineMb = sandbox.memoryGb * 1024;
  const burst = config?.burstFactor ?? 4;
  const autoResume = idle ? idle.autoResumeEnabled : sandbox.autoResume;
  const suspendedS = idle?.suspendedDurationS ?? sandbox.suspendedDurationS;

  return (
    <div className="pane">
      <div className="pane-body">
        <section className="section" aria-labelledby="vitals-timers">
          <h2 id="vitals-timers" className="section-title">
            State and timers
          </h2>
          <dl className="facts">
            <div>
              <dt>State</dt>
              <dd>
                <StatePill state={shownState(sandbox)} />
                {endReason(sandbox.stateReason) ? <span className="fact-sub">{endReason(sandbox.stateReason)}</span> : null}
              </dd>
            </div>
            <div>
              <dt>{ended ? 'Started' : 'Uptime'}</dt>
              <dd className="tabular">
                {ended ? (startedAt ? fmtDateTime(startedAt) : '–') : stats ? fmtDuration(stats.uptimeS * 1000) : startedAt ? fmtDuration(now - startedAt) : '–'}
                {!ended && startedAt ? <span className="fact-sub">started {fmtDateTime(startedAt)}</span> : null}
              </dd>
            </div>
            {ended ? null : (
            <div className="fact-wide">
              <dt>Lifetime</dt>
              <dd>
                <LifeBar startedAt={startedAt} maxDurationS={maxS} now={now} tone={stateTone(state)} height={6} />
                <span className={`fact-line tabular${left !== null && left < 1_800_000 ? ' meta-warn' : ''}`}>
                  {left !== null ? `${fmtDuration(left)} left of ${fmtDuration(maxS * 1000)}` : `${fmtDuration(maxS * 1000)} cap`}
                  {expiresAt ? <span className="fact-sub"> · stops at {fmtShortTime(expiresAt)}, suspended time counts</span> : null}
                </span>
              </dd>
            </div>
            )}
            {ended ? null : (
              <div className="fact-wide">
                <dt>Idle policy</dt>
                <dd>
                  Suspends after {idle ? Math.round(idle.maxIdleDurationS / 60) : sandbox.idleMinutes} min without inbound
                  traffic, then {suspendedText(suspendedS, maxS)}.
                  <span className="fact-line">
                    {autoResume
                      ? 'Auto-resume on: traffic to it (the terminal, the agent) wakes it.'
                      : 'Auto-resume off: only Resume wakes it; the terminal shows it suspended.'}
                  </span>
                </dd>
              </div>
            )}
          </dl>
        </section>

        <section className="section" aria-labelledby="vitals-settings">
          <h2 id="vitals-settings" className="section-title">
            Settings
          </h2>
          <dl className="facts">
            <div>
              <dt>Size</dt>
              <dd>
                {sandbox.sizeLabel} <span className="tabular">· {sandbox.memoryGb} GB · {sandbox.vcpu} vCPU</span>
                <span className="fact-sub tabular">
                  baseline (billed, counts against the quota); bursts to {sandbox.vcpu * burst} vCPU and{' '}
                  {sandbox.memoryGb * burst} GB, which is what nproc and free show
                  {sandbox.usdPerHour !== null ? ` · about $${sandbox.usdPerHour.toFixed(3)}/h running` : ''}
                </span>
              </dd>
            </div>
            <div>
              <dt>Lifetime</dt>
              <dd className="tabular">
                {fmtLifetime(maxS)}
                <span className="fact-sub">{Math.round(maxS / 60)} min, suspended time included</span>
              </dd>
            </div>
            <div className="fact-wide">
              <dt>Egress</dt>
              <dd>{egressText(sandbox.egress)}</dd>
            </div>
            <div className="fact-wide">
              <dt>Environment variables</dt>
              <dd>
                {sandbox.envNames.length ? (
                  <span className="mono env-names">{sandbox.envNames.join(' ')}</span>
                ) : (
                  <span className="fact-sub">None besides the placeholders and the CA settings</span>
                )}
              </dd>
            </div>
          </dl>
        </section>

        <section className="section" aria-labelledby="vitals-live">
          <div className="section-title-row">
            <h2 id="vitals-live" className="section-title">
              CPU and memory
            </h2>
            {liveOn ? <AwakeHint what="Stats are sampled every 5 s" /> : null}
          </div>
          {ended ? (
            <p className="pane-note">The sandbox is gone, so there are no more stats to read.</p>
          ) : !liveOn && live.samples.length === 0 ? (
            <Empty icon={<Plug size={18} aria-hidden />} title={state === 'RUNNING' ? 'Attach a terminal for live stats' : `Live stats need a running sandbox`}>
              {state === 'RUNNING'
                ? 'Stats come from an agent inside the sandbox, and reading them keeps it awake. So they are sampled only while a terminal is attached.'
                : (v?.statsError ?? 'The playground won’t wake a suspended sandbox just to read stats.')}
            </Empty>
          ) : (
            <>
              {!liveOn ? (
                <p className="pane-note">
                  {state === 'SUSPENDED' || state === 'SUSPENDING'
                    ? 'Suspended: showing the last samples.'
                    : state === 'RUNNING'
                      ? 'Paused: no terminal attached. Showing the last samples.'
                      : 'Paused: showing the last samples.'}
                </p>
              ) : null}
              {liveOn && v?.statsError ? <p className="pane-note is-warn">{v.statsError}</p> : null}
              <LineChart
                title="CPU"
                points={live.samples.map((s) => ({ t: s.t, v: s.cpu }))}
                yMax={() => 100}
                fmt={(n) => `${Math.round(n)}%`}
                describe={(n) => `${n.toFixed(0)}%${stats ? ` of ${stats.cpuCount} vCPU (${sandbox.vcpu} baseline) · load ${stats.load1.toFixed(2)}` : ''}`}
                dimmed={!liveOn}
              />
              <LineChart
                title="Memory"
                points={live.samples.map((s) => ({ t: s.t, v: s.memUsed }))}
                yMax={(m) => niceMb(Math.max(baselineMb * 1.25, m * 1.15))}
                fmt={(n) => (n === 0 ? '0' : `${+(n / 1024).toFixed(1)} GB`)}
                yIncrs={[256, 512, 1024, 2048, 4096]}
                describe={(n) => `${fmtMb(n)} used${memTotal ? ` of ${fmtMb(memTotal)}` : ''}`}
                refLines={[
                  { value: baselineMb, label: `${sandbox.memoryGb} GB baseline` },
                  ...(memTotal > baselineMb * 1.5 ? [{ value: memTotal, label: `${fmtMb(memTotal)} burst ceiling` }] : []),
                ]}
                dimmed={!liveOn}
              />
              {stats ? (
                <dl className="facts facts-inline">
                  <div>
                    <dt>Disk</dt>
                    <dd className="tabular">
                      {fmtMb(stats.diskUsedMb)} of {fmtMb(stats.diskTotalMb)}
                    </dd>
                  </div>
                  <div>
                    <dt>Processes</dt>
                    <dd className="tabular">{stats.procs}</dd>
                  </div>
                  <div>
                    <dt>Sampled</dt>
                    <dd className="tabular">{live.vitals.updatedAt ? fmtTime(live.vitals.updatedAt) : '–'}</dd>
                  </div>
                </dl>
              ) : null}
            </>
          )}
        </section>

        <section className="section" aria-labelledby="vitals-timeline">
          <h2 id="vitals-timeline" className="section-title">
            Lifecycle
          </h2>
          <Timeline sandbox={sandbox} />
        </section>
      </div>
    </div>
  );
}

function niceMb(mb: number) {
  const step = mb > 4096 ? 1024 : 512;
  return Math.ceil(mb / step) * step;
}

const VERB: Partial<Record<SandboxState, string>> = {
  PENDING: 'Starting',
  RUNNING: 'Running',
  SUSPENDING: 'Suspending',
  SUSPENDED: 'Suspended',
  RESUMING: 'Resuming',
  TERMINATING: 'Terminating',
  TERMINATED: 'Terminated',
};

function Timeline({ sandbox }: { sandbox: Sandbox }) {
  const events = hub.lifecycle[sandbox.id] ?? [];
  const items = useMemo(() => {
    // `events` is newest first; `n` keeps that order for entries that share a timestamp.
    const list = events.map((e, n) => ({
      n,
      ts: e.ts,
      state: e.state,
      label:
        e.prev === 'SUSPENDED' && e.state === 'RUNNING'
          ? 'Resumed'
          : e.prev === 'SUSPENDED' && e.state === 'PENDING'
            ? 'Resuming'
            : e.prev === null
            ? e.state === 'PENDING'
              ? 'Created'
              : `${STATE_LABEL[e.state]} when first seen`
            : (VERB[e.state] ?? e.state),
    }));
    const created = list.find((i) => i.label === 'Created');
    if (created) {
      // This browser saw the creation. The control plane's startedAt is the same moment give or take a second, and
      // can land after the observed "Created": nothing comes before Created, so pin it to the earlier of the two.
      if (sandbox.startedAt) created.ts = Math.min(created.ts, sandbox.startedAt);
    } else if (sandbox.startedAt) {
      list.push({ n: list.length, ts: sandbox.startedAt, state: 'PENDING' as SandboxState, label: 'Started' });
    }
    return list.sort((a, b) => b.ts - a.ts || a.n - b.n);
  }, [events, sandbox.startedAt]);

  if (items.length === 0) return <p className="pane-note">Run, suspend and resume events appear here as they happen.</p>;
  return (
    <ol className="timeline">
      {items.map((i, n) => (
        <li key={`${i.ts}-${i.state}-${n}`} className="timeline-item">
          <StateDot state={i.state} still />
          <span className="timeline-label">{i.label}</span>
          <span className="timeline-time tabular" title={new Date(i.ts).toLocaleString()}>
            {fmtDateTime(i.ts)}
          </span>
        </li>
      ))}
    </ol>
  );
}
