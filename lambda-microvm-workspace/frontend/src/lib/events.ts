import { useEffect, useSyncExternalStore } from 'react';
import { getIdToken } from './auth';
import { wsUrl } from './config';
import { toMs, toState } from './api';
import { backoff, Heartbeat, openSocket, type SocketLike } from './socket';
import type { AwsAnnotation, DnsEvent, LifecycleEvent, ProxyEvent } from './types';

// One /ws/events connection for the whole app. Buffers proxy and DNS events (newest first)
// and per-sandbox lifecycle history, and notifies React at most once per animation frame.
//
// Lifecycle events always flow. The backend tails the proxy and DNS logs (CloudWatch Logs Live Tail,
// billed per minute) only while some page tells it {"type":"feeds","on":true}: this page does so while a
// Proxy or DNS panel is mounted (the dock is open on that tab) and the page is visible.

export type HubStatus = 'connecting' | 'live' | 'reconnecting';

const CAP = 1000;
const LIFECYCLE_KEY = 'ws.lifecycle.v1';

type Listener = () => void;
type LifecycleListener = (e: LifecycleEvent) => void;

class EventsHub {
  proxy: ProxyEvent[] = [];
  dns: DnsEvent[] = [];
  lifecycle: Record<string, LifecycleEvent[]> = loadLifecycle();
  status: HubStatus = 'connecting';
  /** How the backend reads the logs: idle (no page shows a feed), starting (backfilling, opening the
   *  session), live_tail, polling, waiting (no log group yet) or disabled. */
  source: string | null = null;
  missingLogGroups: string[] = [];
  retryAt: number | null = null;
  version = 0;

  private seq = 0;
  private ws: SocketLike | null = null;
  private hb: Heartbeat | null = null;
  private attempt = 0;
  private timer: number | null = null;
  private listeners = new Set<Listener>();
  private lifecycleListeners = new Set<LifecycleListener>();
  private scheduled = false;
  private started = false;
  /** Mounted Proxy/DNS panels. */
  private feedHolders = 0;
  /** The socket that has sent auth (messages before that would be taken as the auth message). */
  private authed: SocketLike | null = null;
  /** What this socket was last told; null = nothing yet. */
  private feedsSent: boolean | null = null;
  private feedsSyncQueued = false;

  start() {
    if (this.started) return;
    this.started = true;
    this.connect();
    window.addEventListener('online', () => {
      if (this.status === 'reconnecting') this.connect();
    });
    document.addEventListener('visibilitychange', () => {
      this.queueFeedsSync();
      this.bump();
    });
  }

  /** This page needs the proxy/DNS feed now: a feed panel is mounted and the page is visible. */
  get feedsWanted(): boolean {
    return this.feedHolders > 0 && document.visibilityState === 'visible';
  }

  /**
   * The shown feed isn't streaming yet: 'connecting' while the socket (re)connects, 'starting' while the
   * backend's tail is idle (we just asked for it) or starting (backfilling and opening the session).
   * null once it streams, or when this page doesn't need the feed.
   */
  get feedPending(): 'connecting' | 'starting' | null {
    if (!this.feedsWanted) return null;
    if (this.status !== 'live') return 'connecting';
    if (this.source === null || this.source === 'idle' || this.source === 'starting') return 'starting';
    return null;
  }

  /** Called by a feed panel on mount; the returned function releases it on unmount. */
  holdFeeds(): () => void {
    this.feedHolders++;
    this.queueFeedsSync();
    let released = false;
    return () => {
      if (released) return;
      released = true;
      this.feedHolders--;
      this.queueFeedsSync();
    };
  }

  /** Coalesced, so switching Proxy -> DNS (unmount, then mount in the same commit) sends nothing. */
  private queueFeedsSync() {
    if (this.feedsSyncQueued) return;
    this.feedsSyncQueued = true;
    queueMicrotask(() => {
      this.feedsSyncQueued = false;
      this.syncFeeds(false);
    });
  }

  private syncFeeds(force: boolean) {
    const ws = this.ws;
    if (!ws || this.authed !== ws) return; // sent right after auth instead
    const on = this.feedsWanted;
    if (!force && on === this.feedsSent) return;
    try {
      ws.send(JSON.stringify({ type: 'feeds', on }));
      this.feedsSent = on;
    } catch {
      /* closing; the next connection re-sends */
    }
    this.bump();
  }

  private connect() {
    if (this.timer) window.clearTimeout(this.timer);
    this.timer = null;
    this.hb?.stop();
    const old = this.ws;
    this.ws = null;
    this.authed = null;
    this.feedsSent = null;
    // Until the new connection's first status arrives, don't show what the old one said.
    this.source = null;
    old?.close();
    const ws = openSocket(wsUrl('/ws/events'));
    this.ws = ws;
    const hb = new Heartbeat(ws, () => {
      // Silent for too long: treat as dropped without waiting for the close handshake.
      if (this.ws !== ws) return;
      ws.onclose = null;
      try {
        ws.close();
      } catch {
        /* already closed */
      }
      this.onDropped(1006);
    });
    this.hb = hb;
    ws.onopen = async () => {
      const token = await getIdToken(this.authFailed);
      this.authFailed = false;
      if (this.ws !== ws) return;
      ws.send(JSON.stringify({ type: 'auth', token }));
      this.authed = ws;
      // Interest is per connection on the backend: tell every new connection where this page stands.
      this.syncFeeds(true);
      this.attempt = 0;
      this.retryAt = null;
      this.setStatus('live');
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data !== 'string') {
        hb.heard(null);
        return;
      }
      let msg: Record<string, unknown> | null = null;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        /* ignore malformed frames */
      }
      if (hb.heard(msg) || !msg) return;
      this.ingest(msg);
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.onDropped(ev.code);
    };
  }

  private authFailed = false;

  private onDropped(code: number) {
    if (code === 4401) this.authFailed = true;
    this.hb?.stop();
    this.ws = null;
    const delay = code === 4401 ? 2 : backoff(this.attempt++);
    this.retryAt = Date.now() + delay * 1000;
    this.setStatus('reconnecting');
    this.timer = window.setTimeout(() => this.connect(), delay * 1000);
  }

  retryNow() {
    this.connect();
  }

  private ingest(m: Record<string, unknown>) {
    const ts = toMs(m.ts) ?? Date.now();
    if (m.type === 'proxy') {
      const e: ProxyEvent = {
        key: ++this.seq,
        ts,
        action: String(m.action ?? 'allow'),
        status: typeof m.status === 'number' ? m.status : m.status ? Number(m.status) : null,
        method: String(m.method ?? ''),
        host: String(m.host ?? ''),
        path: String(m.path ?? ''),
        rejectedBy: m.rejected_by ? String(m.rejected_by) : null,
        secrets: Array.isArray(m.secrets) ? m.secrets.map(String) : [],
        mode: m.mode ? String(m.mode) : null,
        durationMs: typeof m.duration_ms === 'number' ? m.duration_ms : null,
        aws: awsAnnotation(m.aws),
      };
      if (this.seen(`p|${e.ts}|${e.method}|${e.host}|${e.path}|${e.status}`)) return;
      this.proxy = insertNewestFirst(this.proxy, e);
    } else if (m.type === 'dns') {
      const e: DnsEvent = {
        key: ++this.seq,
        ts,
        query: String(m.query ?? '').replace(/\.$/, ''),
        qtype: String(m.qtype ?? ''),
        rcode: String(m.rcode ?? ''),
        srcaddr: String(m.srcaddr ?? ''),
      };
      if (this.seen(`d|${e.ts}|${e.query}|${e.qtype}|${e.rcode}|${e.srcaddr}`)) return;
      this.dns = insertNewestFirst(this.dns, e);
    } else if (m.type === 'lifecycle') {
      const e: LifecycleEvent = {
        key: ++this.seq,
        ts,
        id: String(m.id ?? ''),
        state: toState(m.state),
        prev: m.prev ? toState(m.prev) : null,
        name: m.name ? String(m.name) : null,
      };
      // `initial` snapshots (sent on every connect) are current state, not transitions: they
      // update the sandbox but only enter the timeline when there is no history yet.
      const history = this.lifecycle[e.id] ?? [];
      const isNew = !m.initial || history.length === 0 || history[0].state !== e.state;
      if (isNew) {
        const list = [e, ...history].slice(0, 60);
        this.lifecycle = { ...this.lifecycle, [e.id]: list };
        saveLifecycle(this.lifecycle);
      }
      this.lifecycleListeners.forEach((l) => l(e));
    } else if (m.type === 'status') {
      this.source = typeof m.source === 'string' ? m.source : null;
      this.missingLogGroups = Array.isArray(m.missing_log_groups) ? m.missing_log_groups.map(String) : [];
    } else {
      return;
    }
    this.bump();
  }

  /** Replays after a reconnect overlap what we already have; drop exact repeats. */
  private seenKeys = new Set<string>();
  private seen(sig: string): boolean {
    if (this.seenKeys.has(sig)) return true;
    if (this.seenKeys.size > 5000) this.seenKeys.clear();
    this.seenKeys.add(sig);
    return false;
  }

  private setStatus(s: HubStatus) {
    this.status = s;
    this.bump();
  }

  private bump() {
    if (this.scheduled) return;
    this.scheduled = true;
    requestAnimationFrame(() => {
      this.scheduled = false;
      this.version++;
      this.listeners.forEach((l) => l());
    });
  }

  subscribe = (l: Listener) => {
    this.listeners.add(l);
    return () => this.listeners.delete(l);
  };

  onLifecycle(l: LifecycleListener) {
    this.lifecycleListeners.add(l);
    return () => this.lifecycleListeners.delete(l);
  }
}

/** Keeps the list sorted newest first; replayed events can be older than what is on top. */
function insertNewestFirst<T extends { ts: number }>(list: T[], e: T): T[] {
  if (list.length === 0 || e.ts >= list[0].ts) return [e, ...list].slice(0, CAP);
  let lo = 0;
  let hi = list.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (list[mid].ts > e.ts) lo = mid + 1;
    else hi = mid;
  }
  if (lo >= CAP) return list;
  const next = list.slice();
  next.splice(lo, 0, e);
  return next.slice(0, CAP);
}

function loadLifecycle(): Record<string, LifecycleEvent[]> {
  try {
    const raw = localStorage.getItem(LIFECYCLE_KEY);
    return raw ? (JSON.parse(raw) as Record<string, LifecycleEvent[]>) : {};
  } catch {
    return {};
  }
}

function saveLifecycle(v: Record<string, LifecycleEvent[]>) {
  try {
    localStorage.setItem(LIFECYCLE_KEY, JSON.stringify(v));
  } catch {
    /* storage unavailable; history stays in memory */
  }
}

export const hub = new EventsHub();

/** Re-renders on every batched hub change. Read fields off `hub` after calling it. */
export function useHub(): number {
  return useSyncExternalStore(hub.subscribe, () => hub.version);
}

/** For panels that show the proxy or DNS feed: while one is mounted and the page is visible, the backend
 *  keeps its log tail running. */
export function useFeedPanel() {
  useEffect(() => hub.holdFeeds(), []);
}

/** The proxy event's `aws` object (aws_auth annotations), or null when aws_auth didn't act. */
function awsAnnotation(v: unknown): AwsAnnotation | null {
  if (!v || typeof v !== 'object') return null;
  const o = v as Record<string, unknown>;
  const service = typeof o.service === 'string' ? o.service : '';
  const rejected = typeof o.rejected === 'string' && o.rejected ? o.rejected : null;
  if (!service && !rejected) return null;
  return {
    service: service || 'aws',
    region: typeof o.region === 'string' && o.region ? o.region : null,
    signed: o.signed === true,
    rejected,
  };
}
