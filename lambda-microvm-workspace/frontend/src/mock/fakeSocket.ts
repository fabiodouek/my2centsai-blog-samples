import type { SocketLike } from '../lib/socket';

/** Server side of a fake socket: what the mock "backend" implements per connection. */
export interface FakeServer {
  onMessage(data: string | Uint8Array): void;
  onClientClose(): void;
}

/**
 * Minimal in-page WebSocket stand-in. The mock backend drives it through serverSend /
 * serverClose; the app sees the normal WebSocket event surface.
 */
export class FakeSocket implements SocketLike {
  binaryType: BinaryType = 'blob';
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: CloseEvent) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  server: FakeServer | null = null;

  constructor(
    readonly url: string,
    attach: (sock: FakeSocket) => FakeServer | null,
  ) {
    window.setTimeout(
      () => {
        this.server = attach(this);
        if (!this.server) {
          this.serverClose(1006, '');
          return;
        }
        this.readyState = 1;
        this.onopen?.(new Event('open'));
      },
      60 + Math.random() * 120,
    );
  }

  send(data: string | ArrayBufferLike | ArrayBufferView | Blob): void {
    if (this.readyState !== 1 || !this.server) return;
    const server = this.server;
    if (typeof data === 'string') queueMicrotask(() => server.onMessage(data));
    else if (data instanceof Blob) void data.arrayBuffer().then((b) => server.onMessage(new Uint8Array(b)));
    else if (ArrayBuffer.isView(data)) {
      const copy = new Uint8Array(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength));
      queueMicrotask(() => server.onMessage(copy));
    } else queueMicrotask(() => server.onMessage(new Uint8Array(data as ArrayBuffer)));
  }

  close(code = 1000, reason = ''): void {
    if (this.readyState >= 2) return;
    this.readyState = 2;
    this.server?.onClientClose();
    window.setTimeout(() => {
      this.readyState = 3;
      this.onclose?.(new CloseEvent('close', { code, reason, wasClean: true }));
    }, 0);
  }

  serverSend(data: string | Uint8Array): void {
    if (this.readyState !== 1) return;
    let payload: string | ArrayBuffer | Blob;
    if (typeof data === 'string') payload = data;
    else {
      const buf = data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength) as ArrayBuffer;
      payload = this.binaryType === 'arraybuffer' ? buf : new Blob([buf]);
    }
    this.onmessage?.(new MessageEvent('message', { data: payload }));
  }

  serverClose(code: number, reason: string): void {
    if (this.readyState >= 2) return;
    this.readyState = 3;
    this.server?.onClientClose();
    window.setTimeout(() => this.onclose?.(new CloseEvent('close', { code, reason, wasClean: code === 1000 })), 0);
  }
}
