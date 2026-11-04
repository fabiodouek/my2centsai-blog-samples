import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { defineConfig, loadEnv, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';

const require = createRequire(import.meta.url);

/**
 * Serves (dev) or emits (build) MSW's service worker, only in mock mode, so the
 * production bundle never ships it and the repo never carries a generated copy.
 */
function mswWorker(enabled: boolean): Plugin {
  const workerPath = require.resolve('msw/mockServiceWorker.js');
  return {
    name: 'msw-worker',
    configureServer(server) {
      if (!enabled) return;
      server.middlewares.use('/mockServiceWorker.js', (_req, res) => {
        res.setHeader('Content-Type', 'application/javascript');
        res.end(readFileSync(workerPath));
      });
    },
    generateBundle() {
      if (!enabled) return;
      this.emitFile({ type: 'asset', fileName: 'mockServiceWorker.js', source: readFileSync(workerPath) });
    },
  };
}

export default defineConfig(({ mode }) => {
  const env = { ...loadEnv(mode, process.cwd(), ''), ...process.env };
  const mock = env.VITE_MOCK === '1';
  // For `npm run dev` against a local backend (uvicorn on :8000 by default).
  const backend = env.BACKEND_URL || 'http://localhost:8000';
  const originHeader: Record<string, string> = env.ORIGIN_SECRET ? { 'X-Origin-Verify': env.ORIGIN_SECRET } : {};

  return {
    plugins: [react(), mswWorker(mock)],
    server: {
      port: 5173,
      proxy: mock
        ? undefined
        : {
            '/api': { target: backend, changeOrigin: true, headers: originHeader },
            '/ws': { target: backend.replace(/^http/, 'ws'), ws: true, changeOrigin: true, headers: originHeader },
          },
    },
    build: {
      target: 'es2022',
      sourcemap: true,
      chunkSizeWarningLimit: 900,
    },
  };
});
