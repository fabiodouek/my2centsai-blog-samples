import { hub, useHub } from '../../lib/events';
import { useNow } from '../../lib/hooks';
import { Empty, SkeletonRows } from '../ui/primitives';

const SOURCE_LABEL: Record<string, string> = {
  live_tail: 'Live tail',
  starting: 'Starting',
  polling: 'Polling, 3 s',
  waiting: 'Waiting for logs',
  idle: 'Idle',
  disabled: 'Logs disabled',
};

/** Connection state of /ws/events plus how the backend reads the log groups. */
export function FeedStatus({ paused }: { paused?: boolean }) {
  useHub();
  const now = useNow();
  if (paused) return <span className="feed-status-chip is-paused">Paused</span>;
  if (hub.status === 'reconnecting') {
    const s = hub.retryAt ? Math.max(0, Math.ceil((hub.retryAt - now) / 1000)) : null;
    return (
      <button type="button" className="feed-status-chip is-warn" onClick={() => hub.retryNow()} title="Reconnect now">
        Reconnecting{s ? ` in ${s} s` : '…'}
      </button>
    );
  }
  if (hub.status === 'connecting') return <span className="feed-status-chip">Connecting…</span>;
  // The backend tails the logs only while a page shows this panel, so opening it can find the tail idle:
  // it then backfills what was logged meanwhile (up to 10 minutes) and opens a Live Tail session.
  if (hub.feedPending === 'starting') {
    return (
      <span className="feed-status-chip" title="Starting the log tail; backfilling recent lines from CloudWatch Logs">
        Starting…
      </span>
    );
  }
  const src = hub.source ? (SOURCE_LABEL[hub.source] ?? hub.source) : 'Live';
  const degraded = hub.source === 'disabled' || hub.source === 'waiting' || hub.missingLogGroups.length > 0;
  return (
    <span
      className={`feed-status-chip ${degraded ? 'is-warn' : 'is-live'}`}
      title={hub.missingLogGroups.length ? `Missing log groups: ${hub.missingLogGroups.join(', ')}` : 'Streaming from CloudWatch Logs'}
    >
      <span className="live-dot" aria-hidden />
      {src}
    </span>
  );
}

/**
 * What an empty feed shows while the feed isn't streaming yet, instead of "waiting for traffic";
 * null once it streams (then an empty list really means nothing was logged).
 */
export function FeedPendingEmpty({ what }: { what: string /* lowercase noun: "requests" */ }) {
  useHub();
  const pending = hub.feedPending;
  if (!pending) return null;
  return (
    <div role="status">
      <Empty title={pending === 'connecting' ? 'Connecting to the feed…' : 'Starting the feed…'}>
        {pending === 'connecting'
          ? `New ${what} appear once the connection to the workspace backend is back.`
          : `Backfilling recent ${what} from CloudWatch Logs (up to 10 minutes back), then streaming live.`}
      </Empty>
      <SkeletonRows rows={5} />
    </div>
  );
}
