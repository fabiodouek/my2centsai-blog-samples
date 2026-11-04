import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@xterm/xterm/css/xterm.css';
import 'uplot/dist/uPlot.min.css';
import './styles/tokens.css';
import './styles/app.css';
import { Boot } from './components/Boot';
import { MOCK } from './lib/config';

async function start() {
  if (MOCK) {
    const { startMock } = await import('./mock');
    await startMock();
  }
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <Boot />
    </StrictMode>,
  );
}

void start();
