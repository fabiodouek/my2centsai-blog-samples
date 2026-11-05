import { getIdToken } from './auth';
import { wsUrl } from './config';
import { backoff, Heartbeat, openSocket, OPEN, type SocketLike } from './socket';
import type { TermState } from './types';

// One browser-side terminal session for a sandbox: /ws/terminal/{id}.
// Binary frames carry terminal bytes both ways; text frames carry JSON control messages.

export interface TermStatus {
  state: TermState;
  detail: string;
  /** Epoch ms of the next automatic retry (reconnecting only). */
  retryAt: number | null;
  /** Who is retrying: the backend (upstream MicroVM dropped) or this browser (our socket dropped). */
  retryBy: 'server' | 'client' | null;
  /** Epoch ms when the backend stops retrying (reconnecting, from gives_up_in_s). */
  givesUpAt?: number | null;
  attempt?: number | null;
}

interface Handlers {
  onOutput: (data: Uint8Array) => void;
  onStatus: (s: TermStatus) => void;
  getSize: () => { rows: number; cols: number };
}

const MAX_CLIENT_ATTEMPTS = 8;
const encoder = new TextEncoder();

export class TerminalConnection {
  private ws: SocketLike | null = null;
  private hb: Heartbeat | null = null;
  private status: TermStatus = { state: 'idle', detail: '', retryAt: null, retryBy: null };
  private attempt = 0;
  private timer: number | null = null;
  private authRetried = false;
  private disposed = false;
  private onlineHandler = () => {
    if (this.status.state === 'reconnecting' && this.status.retryBy === 'client') this.connect();
  };

  constructor(
    private readonly sandboxId: string,
    private readonly h: Handlers,
  ) {
    window.addEventListener('online', this.onlineHandler);
  }

  get state(): TermState {
    return this.status.state;
  }

  private set(state: TermState, detail = '', extra: Partial<TermStatus> = {}) {
    this.status = { state, detail, retryAt: null, retryBy: null, ...extra };
    if (!this.disposed) this.h.onStatus(this.status);
  }

  connect(detail = 'Opening a shell in the sandbox') {
    if (this.disposed) return;
    this.clearTimer();
    this.closeSocket();
    this.set('connecting', detail);
    const ws = openSocket(wsUrl(`/ws/terminal/${encodeURIComponent(this.sandboxId)}`));
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    const hb = new Heartbeat(ws, () => {
      // No heartbeat for too long: the path through CloudFront is dead. Don't wait for the close handshake.
      if (this.ws !== ws) return;
      ws.onclose = null;
      this.ws = null;
      try {
        ws.close();
      } catch {
        /* already closed */
      }
      this.onSocketClosed(1006, 'The connection went quiet.');
    });
    this.hb = hb;

    ws.onopen = async () => {
      const token = await getIdToken(this.authRetried);
      if (this.ws !== ws || ws.readyState !== OPEN) return;
      ws.send(JSON.stringify({ type: 'auth', token }));
      const { rows, cols } = this.h.getSize();
      ws.send(JSON.stringify({ type: 'resize', rows, cols }));
    };

    ws.onmessage = (ev) => {
      if (this.ws !== ws) return;
      if (typeof ev.data === 'string') {
        let msg: Record<string, unknown> | null = null;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          hb.heard(null);
          this.h.onOutput(encoder.encode(ev.data));
          return;
        }
        if (hb.heard(msg)) return;
        if (msg && msg.type === 'status') this.onServerStatus(msg);
        return;
      }
      hb.heard(null);
      if (ev.data instanceof ArrayBuffer) this.h.onOutput(new Uint8Array(ev.data));
      else if (ev.data instanceof Blob) void ev.data.arrayBuffer().then((b) => this.h.onOutput(new Uint8Array(b)));
    };

    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      this.onSocketClosed(ev.code, ev.reason);
    };
  }

  private onServerStatus(msg: Record<string, unknown>) {
    const state = String(msg.state ?? '');
    const detail = typeof msg.detail === 'string' ? msg.detail : '';
    const retryIn = Number(msg.retry_in_s ?? NaN);
    const givesUp = Number(msg.gives_up_in_s ?? NaN);
    const attempt = Number(msg.attempt ?? NaN);
    switch (state) {
      case 'connecting':
        this.set('connecting', detail || 'Opening a shell in the sandbox');
        break;
      case 'attached': {
        this.attempt = 0;
        this.authRetried = false;
        this.set('attached', detail);
        // Re-send the size: the server may have reattached tmux at a default size.
        const { rows, cols } = this.h.getSize();
        this.resize(rows, cols);
        break;
      }
      case 'reconnecting':
        this.set('reconnecting', detail || 'The sandbox connection dropped. The playground is retrying.', {
          retryBy: 'server',
          retryAt: Number.isFinite(retryIn) ? Date.now() + retryIn * 1000 : null,
          givesUpAt: Number.isFinite(givesUp) ? Date.now() + givesUp * 1000 : null,
          attempt: Number.isFinite(attempt) ? attempt : null,
        });
        break;
      case 'suspended':
        // The backend keeps this socket open and reattaches tmux on its own once the sandbox
        // is RUNNING again (after POST /resume), so we only change the overlay.
        this.set('suspended', detail);
        break;
      case 'exited':
        this.set('exited', detail);
        break;
      default:
        break;
    }
  }

  private onSocketClosed(code: number, reason: string) {
    this.hb?.stop();
    this.hb = null;
    if (this.disposed) return;
    const s = this.status.state;
    if (s === 'detached' || s === 'exited' || s === 'error' || s === 'terminated') return;
    if (code === 4410) {
      this.set('terminated', reason || 'This sandbox has been terminated.');
      return;
    }
    if (s === 'suspended') return; // stays suspended; Resume reconnects if the socket is gone
    if (code === 1000 && /exit/i.test(reason)) {
      this.set('exited', reason);
      return;
    }
    if (code === 1011) {
      this.set('error', reason || 'The playground gave up reconnecting to the sandbox.');
      return;
    }
    if (code === 4401) {
      if (!this.authRetried) {
        this.authRetried = true;
        this.connect('Refreshing your sign-in');
        return;
      }
      this.set('error', 'The playground rejected your sign-in. Reload the page to sign in again.');
      return;
    }
    if (code === 4403 || code === 4404) {
      this.set('error', reason || 'This sandbox was not found, or it belongs to another user.');
      return;
    }
    if (this.attempt >= MAX_CLIENT_ATTEMPTS) {
      this.set('error', 'Could not reach the playground after several tries.');
      return;
    }
    const delay = backoff(this.attempt++);
    this.set('reconnecting', reason || 'Lost the connection to the playground.', {
      retryBy: 'client',
      retryAt: Date.now() + delay * 1000,
    });
    this.timer = window.setTimeout(() => this.connect('Reconnecting'), delay * 1000);
  }

  /** Keystrokes and pasted text, as binary frames. */
  send(data: string | Uint8Array) {
    if (this.status.state !== 'attached' || !this.ws || this.ws.readyState !== OPEN) return false;
    this.ws.send(typeof data === 'string' ? encoder.encode(data) : data);
    return true;
  }

  resize(rows: number, cols: number) {
    if (!this.ws || this.ws.readyState !== OPEN || rows < 2 || cols < 2) return;
    this.ws.send(JSON.stringify({ type: 'resize', rows, cols }));
  }

  retryNow() {
    this.attempt = 0;
    this.connect('Reconnecting');
  }

  /** Close on purpose: tmux keeps the session, and the sandbox can go idle. */
  detach() {
    this.clearTimer();
    this.set('detached', '');
    this.closeSocket(1000, 'detached');
  }

  /** Mark suspended locally (a lifecycle event arrived first, or there is no socket). Keeps any open socket. */
  markSuspended(detail = '') {
    if (this.status.state === 'suspended' || this.status.state === 'terminated') return;
    this.clearTimer();
    this.set('suspended', detail);
  }

  markTerminated(detail = '') {
    this.clearTimer();
    this.set('terminated', detail);
    this.closeSocket(1000, 'terminated');
  }

  /** True while a socket is open or opening (the backend will reattach by itself after a resume). */
  get isOpen(): boolean {
    return this.ws !== null && this.ws.readyState <= OPEN;
  }

  dispose() {
    this.disposed = true;
    this.clearTimer();
    this.closeSocket(1000, 'closed');
    window.removeEventListener('online', this.onlineHandler);
  }

  private clearTimer() {
    if (this.timer) window.clearTimeout(this.timer);
    this.timer = null;
  }

  private closeSocket(code = 1000, reason = '') {
    this.hb?.stop();
    this.hb = null;
    const ws = this.ws;
    this.ws = null;
    if (ws && ws.readyState <= OPEN) {
      try {
        ws.close(code, reason);
      } catch {
        /* already closing */
      }
    }
  }
}
