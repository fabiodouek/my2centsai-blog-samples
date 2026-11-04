import { delay, http, HttpResponse } from 'msw';
import { credentialMeta, currentAllowlist, IS_ADMIN, MOCK_EMAIL, policyHandlers } from './policy';
import {
  GB_PER_SANDBOX,
  MAX_DURATION_S,
  QUOTA_GB,
  createSandbox,
  db,
  findSandbox,
  stats,
  transition,
  usageFor,
  usedGb,
  type MSandbox,
} from './db';

const latency = () => delay(90 + Math.random() * 260);

function view(sb: MSandbox) {
  return {
    id: sb.id,
    name: sb.name,
    state: sb.state,
    created_at: sb.created_at,
    started_at: sb.started_at,
    max_duration_s: sb.max_duration_s,
    idle_minutes: sb.idle_minutes,
    image_version: sb.image_version,
    expires_at: new Date(Date.parse(sb.started_at) + sb.max_duration_s * 1000).toISOString(),
    state_reason: sb.terminatedAt ? 'Success.' : null,
    terminated_at: sb.terminatedAt ? new Date(sb.terminatedAt).toISOString() : null,
    attached: sb.attached > 0,
    last_activity_at: new Date(sb.lastActivity).toISOString(),
  };
}

function notFound() {
  return HttpResponse.json({ detail: 'Sandbox not found' }, { status: 404 });
}

export const handlers = [
  http.get('/api/me', async () => {
    await latency();
    // ?viewer=1 shows the workspace as a non-admin (read-only proxy policy, no revoke).
    return HttpResponse.json({ sub: 'mock-sub-7f3c', email: MOCK_EMAIL, groups: IS_ADMIN ? ['admins'] : [], is_admin: IS_ADMIN });
  }),

  http.get('/api/config', async () => {
    await latency();
    return HttpResponse.json({
      proxy_mode: 'explicit',
      quota_gb: QUOTA_GB,
      max_duration_s: MAX_DURATION_S,
      pricing: {
        source: 'Anthropic',
        as_of: '2026-11-01',
        note: 'Estimated from Anthropic list prices as of 2026-11-01 (mock)',
        unit: 'USD per million tokens',
        models: {},
      },
      proxy_allowlist: currentAllowlist(),
      sandbox_cidrs: ['10.50.20.0/24', '10.50.21.0/24'],
      vm_memory_gb: GB_PER_SANDBOX,
      default_idle_minutes: 15,
      secrets: credentialMeta().map((c) => c.name),
      feed_scope_note: 'iron-proxy sees every request as coming from the NLB, so the feed covers all sandboxes.',
    });
  }),

  http.get('/api/sandboxes', async () => {
    await latency();
    return HttpResponse.json(db.sandboxes.map(view));
  }),

  http.post('/api/sandboxes', async ({ request }) => {
    await latency();
    const body = (await request.json()) as { name?: string; idle_minutes?: number; max_hours?: number };
    const name = (body.name ?? '').trim();
    if (!name) return HttpResponse.json({ detail: 'Name is required.' }, { status: 422 });
    if (usedGb() + GB_PER_SANDBOX > QUOTA_GB) {
      return HttpResponse.json(
        {
          detail: `The account's MicroVM memory quota is full (${usedGb()} of ${QUOTA_GB} GB; suspended sandboxes count). Terminate one to free 2 GB.`,
        },
        { status: 409 },
      );
    }
    const sb = createSandbox(name, body.idle_minutes ?? 15, body.max_hours ?? 8);
    return HttpResponse.json(view(sb), { status: 201 });
  }),

  http.post('/api/sandboxes/:id/suspend', async ({ params }) => {
    await latency();
    const sb = findSandbox(String(params.id));
    if (!sb) return notFound();
    if (sb.state !== 'RUNNING') return HttpResponse.json({ detail: `Cannot suspend a ${sb.state} sandbox.` }, { status: 409 });
    transition(sb, 'SUSPENDING', 'SUSPENDED', 500);
    return HttpResponse.json(view(sb));
  }),

  http.post('/api/sandboxes/:id/resume', async ({ params }) => {
    await latency();
    const sb = findSandbox(String(params.id));
    if (!sb) return notFound();
    if (sb.state !== 'SUSPENDED') return HttpResponse.json({ detail: `Cannot resume a ${sb.state} sandbox.` }, { status: 409 });
    // Like the backend: a resume shows SUSPENDED -> PENDING -> RUNNING.
    transition(sb, 'PENDING', 'RUNNING', 800);
    return HttpResponse.json(view(sb));
  }),

  http.delete('/api/sandboxes/:id', async ({ params }) => {
    await latency();
    const sb = findSandbox(String(params.id));
    if (!sb) return notFound();
    transition(sb, 'TERMINATING', 'TERMINATED', 1500);
    return HttpResponse.json(view(sb));
  }),

  http.get('/api/sandboxes/:id/vitals', async ({ params, request }) => {
    await latency();
    const sb = findSandbox(String(params.id));
    if (!sb) return notFound();
    const live = new URL(request.url).searchParams.get('live') === '1';
    const started = Date.parse(sb.started_at);
    if (live && sb.state === 'RUNNING') sb.lastActivity = Date.now(); // polling the agent keeps it awake
    const elapsed = (Date.now() - started) / 1000;
    const running = sb.state === 'RUNNING';
    return HttpResponse.json({
      ...view(sb),
      sampled_at: new Date().toISOString(),
      remaining_s: Math.max(0, Math.round(sb.max_duration_s - elapsed)),
      uptime_s: Math.round(elapsed),
      idle_policy: { auto_resume: true, max_idle_s: sb.idle_minutes * 60, suspended_duration_s: sb.max_duration_s },
      stats: live && running ? stats(sb) : null,
      stats_error: !live ? null : running ? null : `The sandbox is ${sb.state}; live stats would wake it, so they are skipped.`,
    });
  }),

  http.get('/api/sandboxes/:id/claude-usage', async ({ params }) => {
    await latency();
    const sb = findSandbox(String(params.id));
    if (!sb) return notFound();
    if (sb.state !== 'RUNNING') return HttpResponse.json({ detail: `The sandbox is ${sb.state}.` }, { status: 409 });
    sb.lastActivity = Date.now();
    return HttpResponse.json(usageFor(sb.id));
  }),

  http.get('/api/quota', async () => {
    await latency();
    const mine = db.sandboxes.filter((s) => s.state !== 'TERMINATED').length;
    return HttpResponse.json({
      used_gb: usedGb(),
      quota_gb: QUOTA_GB,
      vm_memory_gb: GB_PER_SANDBOX,
      user_microvms: mine,
      user_gb: mine * GB_PER_SANDBOX,
      account_microvms: mine,
      scope: 'account',
    });
  }),

  http.get('/api/credentials', async () => {
    await latency();
    const since = Date.now() - 15 * 60_000;
    const recent = db.proxy.filter((r) => r.ts >= since);
    return HttpResponse.json(
      credentialMeta().map(({ name, kind, status, placeholder }) => {
        const uses = recent.filter((r) => r.secrets.includes(name));
        const hosts: Record<string, number> = {};
        for (const u of uses) hosts[u.host] = (hosts[u.host] ?? 0) + 1;
        const last = [...db.proxy].reverse().find((r) => r.secrets.includes(name));
        return {
          name,
          kind,
          placeholder_prefix: placeholder.length > 14 ? `${placeholder.slice(0, 14)}…` : placeholder,
          revoked: status === 'revoking' ? 'pending' : status === 'revoked',
          status,
          status_detail: status === 'revoking' || status === 'restoring' ? 'waiting for iron-proxy to reload' : null,
          uses_15m: uses.length,
          hosts,
          last_used: last ? new Date(last.ts).toISOString() : null,
        };
      }),
    );
  }),

  // Revoke/restore (reload method), the policy endpoints and the change log live in mock/policy.ts.
  ...policyHandlers,
];
