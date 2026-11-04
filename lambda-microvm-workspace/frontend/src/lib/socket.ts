/** The subset of the WebSocket interface the app uses, so mock mode can swap in a fake. */
export interface SocketLike {
  binaryType: BinaryType;
  readonly readyState: number;
  onopen: ((ev: Event) => void) | null;
  onmessage: ((ev: MessageEvent) => void) | null;
  onclose: ((ev: CloseEvent) => void) | null;
  onerror: ((ev: Event) => void) | null;
  send(data: string | ArrayBufferLike | ArrayBufferView | Blob): void;
  close(code?: number, reason?: string): void;
}

export const OPEN = 1;

let factory: (url: string) => SocketLike = (url) => new WebSocket(url);

export function setSocketFactory(f: (url: string) => SocketLike): void {
  factory = f;
}

export function openSocket(url: string): SocketLike {
  return factory(url);
}

/** Backoff schedule shared by both sockets, in seconds. */
export const BACKOFF_S = [1, 2, 4, 8, 15, 30];
export function backoff(attempt: number): number {
  return BACKOFF_S[Math.min(attempt, BACKOFF_S.length - 1)];
}

/**
 * App-level heartbeat. CloudFront closes idle origin connections after 60 s, so we send
 * {"type":"ping"} every 25 s; the backend answers with {"type":"pong"} and also sends its own
 * {"type":"heartbeat"} every 25 s. The watchdog arms once the server has proven it speaks the
 * heartbeat; after that, 60 s of silence counts as a dead connection.
 */
export const PING_EVERY_MS = 25_000;
export const SILENCE_LIMIT_MS = 60_000;

export class Heartbeat {
  private pingTimer: number | null = null;
  private watchTimer: number | null = null;
  private serverSpeaksHeartbeat = false;
  private lastHeard = Date.now();

  constructor(
    private readonly ws: SocketLike,
    private readonly onDead: () => void,
  ) {
    this.pingTimer = window.setInterval(() => {
      if (ws.readyState === OPEN) ws.send(JSON.stringify({ type: 'ping', ts: Date.now() }));
    }, PING_EVERY_MS);
    this.watchTimer = window.setInterval(() => {
      if (this.serverSpeaksHeartbeat && Date.now() - this.lastHeard > SILENCE_LIMIT_MS) {
        this.stop();
        this.onDead();
      }
    }, 5_000);
  }

  /** Call for every frame received. Returns true if the frame was a heartbeat and is consumed. */
  heard(msg?: { type?: unknown } | null): boolean {
    this.lastHeard = Date.now();
    if (!msg) return false;
    if (msg.type === 'pong' || msg.type === 'heartbeat') {
      this.serverSpeaksHeartbeat = true;
      return true;
    }
    if (msg.type === 'ping') {
      this.serverSpeaksHeartbeat = true;
      if (this.ws.readyState === OPEN) this.ws.send(JSON.stringify({ type: 'pong', ts: Date.now() }));
      return true;
    }
    return false;
  }

  stop() {
    if (this.pingTimer) window.clearInterval(this.pingTimer);
    if (this.watchTimer) window.clearInterval(this.watchTimer);
    this.pingTimer = this.watchTimer = null;
  }
}
