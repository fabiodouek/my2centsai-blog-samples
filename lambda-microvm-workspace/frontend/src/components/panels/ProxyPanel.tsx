import { useMemo, useRef, useState } from 'react';
import { ArrowUp, Ban, Check, Pause, Play } from 'lucide-react';
import { hub, useFeedPanel, useHub } from '../../lib/events';
import { fmtTime } from '../../lib/format';
import { useNow } from '../../lib/hooks';
import type { ProxyEvent } from '../../lib/types';
import { HostColumns } from '../charts/HostColumns';
import { Button, Empty, KeyFob, Note, Segmented } from '../ui/primitives';
import { FeedPendingEmpty, FeedStatus } from './FeedStatus';

type Filter = 'all' | 'denied' | 'secret';
const RENDER_CAP = 300;

export function ProxyPanel() {
  useHub();
  useFeedPanel();
  const now = useNow();
  const [filter, setFilter] = useState<Filter>('all');
  const [paused, setPaused] = useState<ProxyEvent[] | null>(null);
  const [pausedByScroll, setPausedByScroll] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const all = hub.proxy;
  const shown = paused ?? all;
  // What arrived since the freeze = rows above the newest frozen row (by key: the buffer is capped).
  let newer = 0;
  if (paused) {
    const i = paused.length ? all.findIndex((e) => e.key === paused[0].key) : -1;
    newer = i >= 0 ? i : all.length;
  }

  const counts = useMemo(
    () => ({
      all: shown.length,
      denied: shown.filter((e) => e.action !== 'allow').length,
      secret: shown.filter((e) => e.secrets.length > 0).length,
    }),
    [shown],
  );
  const rows = useMemo(() => {
    const f =
      filter === 'denied' ? shown.filter((e) => e.action !== 'allow') : filter === 'secret' ? shown.filter((e) => e.secrets.length > 0) : shown;
    return f.slice(0, RENDER_CAP);
  }, [shown, filter]);

  function onScroll() {
    const el = listRef.current;
    if (!el) return;
    // Scrolling away from the top freezes the list so rows don't move under the pointer;
    // scrolling back to the top picks up live updates again.
    if (el.scrollTop > 24 && !paused) {
      setPaused(all);
      setPausedByScroll(true);
    } else if (el.scrollTop <= 2 && paused && pausedByScroll) {
      setPaused(null);
      setPausedByScroll(false);
    }
  }

  function resume() {
    setPaused(null);
    setPausedByScroll(false);
    listRef.current?.scrollTo({ top: 0 });
  }

  return (
    <div className="pane">
      <div className="pane-head">
        <Note>
          Every sandbox on this proxy, not just this one. iron-proxy sees all requests arriving from the load
          balancer’s addresses, so it can’t tell sandboxes apart.
        </Note>
        <HostColumns events={all} now={now} loaded={shown.length} />
        <div className="filter-row">
          <Segmented
            label="Filter requests"
            value={filter}
            onChange={setFilter}
            options={[
              { value: 'all', label: 'All', count: counts.all },
              { value: 'denied', label: 'Denied', count: counts.denied },
              { value: 'secret', label: 'Key swapped', count: counts.secret },
            ]}
          />
          <span className="filter-spacer" />
          <FeedStatus paused={Boolean(paused)} />
          <Button
            size="sm"
            variant="ghost"
            iconOnly
            icon={paused ? <Play size={14} aria-hidden /> : <Pause size={14} aria-hidden />}
            aria-label={paused ? 'Resume live updates' : 'Pause live updates'}
            title={paused ? 'Resume live updates' : 'Pause live updates'}
            onClick={() => {
              if (paused) resume();
              else {
                setPaused(all);
                setPausedByScroll(false);
              }
            }}
          />
        </div>
      </div>

      <div className="feed" ref={listRef} onScroll={onScroll}>
        {paused && newer > 0 ? (
          <button type="button" className="feed-newer" onClick={resume}>
            <ArrowUp size={13} aria-hidden /> {newer} new {newer === 1 ? 'request' : 'requests'}
          </button>
        ) : null}
        {rows.length === 0 && shown.length === 0 && hub.feedPending ? (
          <FeedPendingEmpty what="requests" />
        ) : rows.length === 0 ? (
          <Empty title={shown.length === 0 ? 'Waiting for traffic' : 'Nothing matches this filter'}>
            {shown.length === 0
              ? 'Requests appear here as sandboxes make them through iron-proxy.'
              : filter === 'denied'
                ? 'No request was denied in what’s loaded.'
                : 'No request had a key swapped in.'}
          </Empty>
        ) : (
          <ul className="feed-rows" role="list" aria-label="Proxy requests, newest first">
            {rows.map((e) => (
              <ProxyRow key={e.key} e={e} open={open === e.key} onToggle={() => setOpen(open === e.key ? null : e.key)} />
            ))}
          </ul>
        )}
        {shown.length > RENDER_CAP && rows.length === RENDER_CAP ? (
          <p className="feed-foot">Showing the latest {RENDER_CAP} of {shown.length.toLocaleString()} requests.</p>
        ) : null}
      </div>
    </div>
  );
}

function ProxyRow({ e, open, onToggle }: { e: ProxyEvent; open: boolean; onToggle: () => void }) {
  const denied = e.action !== 'allow';
  const statusClass = e.status !== null && e.status >= 400 ? 'is-deny' : '';
  return (
    <li className={`feed-row${denied ? ' is-denied' : ''}${open ? ' is-open' : ''}`}>
      <button type="button" className="feed-main" onClick={onToggle} aria-expanded={open}>
        <span className="feed-time tabular">{fmtTime(e.ts)}</span>
        <span className={`feed-action ${denied ? 'tone-deny' : 'tone-run'}`} title={denied ? `Denied${e.rejectedBy ? ` by ${e.rejectedBy}` : ''}` : 'Allowed'}>
          {denied ? <Ban size={12} aria-label="Denied" /> : <Check size={12} aria-label="Allowed" />}
        </span>
        <span className="feed-method">{e.method}</span>
        <span className="feed-target" title={`${e.host}${e.path}`}>
          <span className="feed-host">{e.host}</span>
          <span className="feed-path">{e.path}</span>
        </span>
        <span className={`feed-status tabular ${statusClass}`}>{e.status ?? '–'}</span>
        <span className="feed-badges">
          {denied && !(e.aws?.rejected && e.rejectedBy === 'aws_auth') ? <span className="badge badge-deny">{e.rejectedBy ?? 'denied'}</span> : null}
          {e.aws ? (
            <span
              className={`badge badge-aws${e.aws.rejected ? ' is-deny' : ''}`}
              title={
                e.aws.rejected
                  ? `aws_auth refused it: ${e.aws.rejected.replace(/_/g, ' ')}`
                  : `aws_auth re-signed this ${e.aws.service} call with the proxy's scoped role`
              }
            >
              aws {e.aws.service}
            </span>
          ) : null}
          {e.secrets.map((s) => (
            <KeyFob key={s} name={s} title={`iron-proxy swapped the ${s} placeholder for the real key`} />
          ))}
        </span>
      </button>
      {open ? (
        <dl className="feed-detail">
          <dt>Time</dt>
          <dd className="tabular">{new Date(e.ts).toISOString().replace('T', ' ').replace('Z', ' UTC')}</dd>
          <dt>Request</dt>
          <dd className="mono wrap">
            {e.method} https://{e.host}
            {e.path}
          </dd>
          <dt>Result</dt>
          <dd>
            {denied ? `Denied by ${e.rejectedBy ?? 'policy'}` : 'Allowed'} · status {e.status ?? 'none'}
            {e.durationMs !== null ? ` · ${Math.round(e.durationMs)} ms` : ''}
            {e.mode ? ` · ${e.mode}` : ''}
          </dd>
          <dt>Keys</dt>
          <dd>{e.secrets.length ? e.secrets.join(', ') : 'none swapped'}</dd>
          {e.aws ? (
            <>
              <dt>AWS</dt>
              <dd>
                <span className="mono">{e.aws.service}</span>
                {e.aws.region ? <> in <span className="mono">{e.aws.region}</span></> : null}
                {' · '}
                {e.aws.rejected
                  ? `refused by aws_auth: ${e.aws.rejected.replace(/_/g, ' ')}`
                  : e.aws.signed
                    ? 're-signed with the scoped role'
                    : 'not signed'}
              </dd>
            </>
          ) : null}
        </dl>
      ) : null}
    </li>
  );
}
