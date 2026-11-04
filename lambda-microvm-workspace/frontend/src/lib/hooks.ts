import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';

// ---------- one shared 1 s clock for every countdown on the page ----------

let now = Date.now();
const clockListeners = new Set<() => void>();
let clockTimer: number | null = null;

function subscribeClock(l: () => void) {
  clockListeners.add(l);
  if (clockTimer === null) {
    clockTimer = window.setInterval(() => {
      now = Date.now();
      clockListeners.forEach((fn) => fn());
    }, 1000);
  }
  return () => {
    clockListeners.delete(l);
    if (clockListeners.size === 0 && clockTimer !== null) {
      window.clearInterval(clockTimer);
      clockTimer = null;
    }
  };
}

export function useNow(): number {
  return useSyncExternalStore(subscribeClock, () => now);
}

// ---------- polling ----------

export interface Polled<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  updatedAt: number | null;
  refresh: () => Promise<void>;
}

/**
 * Calls `fn` now and every `intervalMs` while `enabled`. Keeps the last good data on error.
 * `key` resets the data when it changes (e.g. a different sandbox).
 */
export function usePolling<T>(fn: () => Promise<T>, intervalMs: number, enabled: boolean, key: string): Polled<T> {
  const [state, setState] = useState<{ key: string; data: T | null; error: Error | null; loading: boolean; updatedAt: number | null }>(
    { key, data: null, error: null, loading: enabled, updatedAt: null },
  );
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const keyRef = useRef(key);
  keyRef.current = key;
  const inflight = useRef(false);

  if (state.key !== key) {
    setState({ key, data: null, error: null, loading: enabled, updatedAt: null });
  }

  const run = useCallback(async () => {
    if (inflight.current) return;
    inflight.current = true;
    const k = keyRef.current;
    try {
      const data = await fnRef.current();
      if (keyRef.current === k) setState({ key: k, data, error: null, loading: false, updatedAt: Date.now() });
    } catch (e) {
      if (keyRef.current === k) {
        setState((s) => ({ ...s, error: e instanceof Error ? e : new Error(String(e)), loading: false }));
      }
    } finally {
      inflight.current = false;
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      setState((s) => (s.loading ? { ...s, loading: false } : s));
      return;
    }
    void run();
    const t = window.setInterval(() => {
      if (!document.hidden) void run();
    }, intervalMs);
    return () => window.clearInterval(t);
  }, [enabled, intervalMs, key, run]);

  return { data: state.key === key ? state.data : null, error: state.error, loading: state.loading, updatedAt: state.updatedAt, refresh: run };
}

// ---------- persisted UI preferences (best effort) ----------

export function readPref<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : (JSON.parse(raw) as T);
  } catch {
    return fallback;
  }
}

export function writePref(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable */
  }
}

export function usePref<T>(key: string, fallback: T): [T, (v: T) => void] {
  const [v, setV] = useState<T>(() => readPref(key, fallback));
  const set = useCallback(
    (nv: T) => {
      setV(nv);
      writePref(key, nv);
    },
    [key],
  );
  return [v, set];
}

export function useMediaQuery(q: string): boolean {
  return useSyncExternalStore(
    (cb) => {
      const m = window.matchMedia(q);
      m.addEventListener('change', cb);
      return () => m.removeEventListener('change', cb);
    },
    () => window.matchMedia(q).matches,
  );
}
