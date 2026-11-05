import { useEffect, useRef, useState } from 'react';
import { api } from './api';
import { usePolling, type Polled } from './hooks';
import { useStore } from './store';
import type { ClaudeUsage, Sandbox, Vitals } from './types';

// Per-sandbox polling that respects the idle rule: the in-VM agent (vitals?live=1 and
// /claude-usage) is only asked while a terminal for that sandbox is attached in this browser,
// because any request to the MicroVM endpoint keeps it awake. Control-plane vitals (live=0)
// are always safe.

export interface Sample {
  t: number; // epoch s
  cpu: number;
  memUsed: number; // MB
  memTotal: number; // MB
}

export interface SandboxLive {
  attached: boolean;
  vitals: Polled<Vitals>;
  usage: Polled<ClaudeUsage>;
  /** Last usage seen for this sandbox, kept after detaching so the panel can show it as stale. */
  lastUsage: { data: ClaudeUsage; at: number } | null;
  samples: Sample[];
}

const MAX_SAMPLES = 120; // 10 minutes at 5 s
const sampleCache = new Map<string, Sample[]>();
const usageCache = new Map<string, { data: ClaudeUsage; at: number }>();

export function useSandboxLive(sb: Sandbox | null): SandboxLive {
  const id = sb?.id ?? '';
  const attached = useStore((s) => (id ? Boolean(s.attached[id]) : false));
  const running = sb?.state === 'RUNNING';
  const alive = Boolean(sb) && sb?.state !== 'TERMINATED';
  const liveStats = attached && running;

  const attachedRef = useRef(liveStats);
  attachedRef.current = liveStats;

  const vitals = usePolling(() => api.vitals(id, attachedRef.current), liveStats ? 5000 : 15000, alive, id);
  const usage = usePolling(() => api.claudeUsage(id), 10_000, liveStats, id);

  const [samples, setSamples] = useState<Sample[]>(() => sampleCache.get(id) ?? []);
  useEffect(() => {
    setSamples(sampleCache.get(id) ?? []);
  }, [id]);

  const v = vitals.data;
  useEffect(() => {
    if (!v?.stats || !id) return;
    const st = v.stats;
    const prev = sampleCache.get(id) ?? [];
    const t = (vitals.updatedAt ?? Date.now()) / 1000;
    if (prev.length && t - prev[prev.length - 1].t < 1) return;
    const next = [...prev, { t, cpu: st.cpuPct, memUsed: st.memUsedMb, memTotal: st.memTotalMb }].slice(-MAX_SAMPLES);
    sampleCache.set(id, next);
    setSamples(next);
  }, [v, id, vitals.updatedAt]);

  if (usage.data && id) usageCache.set(id, { data: usage.data, at: usage.updatedAt ?? Date.now() });

  return { attached, vitals, usage, lastUsage: usageCache.get(id) ?? null, samples };
}
