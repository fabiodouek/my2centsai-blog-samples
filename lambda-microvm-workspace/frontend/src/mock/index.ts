import { setupWorker } from 'msw/browser';
import { setSocketFactory } from '../lib/socket';
import { startSimulation } from './db';
import { FakeSocket } from './fakeSocket';
import { handlers } from './handlers';
import { EventsServer, ShellServer } from './servers';

/** Starts MSW for REST and swaps WebSockets for in-page fakes. Only imported when VITE_MOCK=1. */
export async function startMock() {
  startSimulation();
  setSocketFactory(
    (url) =>
      new FakeSocket(url, (sock) => {
        const path = new URL(url).pathname;
        if (path === '/ws/events') return new EventsServer(sock);
        const m = path.match(/^\/ws\/terminal\/(.+)$/);
        if (m) return new ShellServer(sock, decodeURIComponent(m[1]));
        return null;
      }),
  );
  const worker = setupWorker(...handlers);
  await worker.start({ onUnhandledRequest: 'bypass', quiet: true });
}
