import { delay, http, HttpResponse } from 'msw';
import { credentialMeta, currentAllowlist, IS_ADMIN, MOCK_EMAIL, policyHandlers } from './policy';
import { paneOf } from './servers';
import {
  GB_PER_SANDBOX,
  MAX_DURATION_S,
  QUOTA_GB,
  SIZES,
  createSandbox,
  db,
  findSandbox,
  stats,
  transition,
  usageFor,
  usedGb,
  type MSandbox,
  type MSize,
} from './db';

const RESERVED = ['ANTHROPIC_API_KEY', 'GH_TOKEN', 'DEMO_TOKEN', 'TEST_KEY', 'SANDBOX_NAME', 'AWS_SDK_UA_APP_ID', 'NODE_EXTRA_CA_CERTS',
  'SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'PATH', 'HOME', 'USER', 'SHELL', 'TERM', 'PWD', 'LANG', 'HOSTNAME', 'TZ', 'TMUX', 'IS_SANDBOX'];

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
    size: sb.size,
    size_label: SIZES[sb.size].label,
    memory_gb: SIZES[sb.size].gb,
    vcpu: SIZES[sb.size].vcpu,
    usd_per_hour: SIZES[sb.size].usd,
    egress: 'proxy',
    auto_resume: sb.auto_resume,
    suspended_duration_s: sb.suspended_duration_s ?? sb.max_duration_s,
    env_names: sb.env_names,
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

/** What the mock's "AWS" says when RunMicrovm goes past the quota: AWS's wording, seen live on 2026-11-05. */
const MOCK_AWS_QUOTA_MESSAGE = 'The base maximum allocated memory limit has been reached for this account';

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
      sizes: (Object.keys(SIZES) as MSize[]).map((id) => ({
        id,
        label: SIZES[id].label,
        memory_gb: SIZES[id].gb,
        vcpu: SIZES[id].vcpu,
        usd_per_hour: SIZES[id].usd,
        available: true,
        unavailable_reason: null,
        note:
          id === 'xlarge'
            ? 'Needs all 8 GB of this account\'s MicroVM memory quota: AWS will refuse it if anything else is running or suspended.'
            : null,
        default: id === 'medium',
      })),
      default_size: 'medium',
      burst_factor: 4,
      lifetime: { min_minutes: 1, max_minutes: 480, default_minutes: 480 },
      idle: { min_minutes: 1, max_minutes: 479, default_minutes: 15 },
      suspended: { min_minutes: 0, max_minutes: 480, default: 'lifetime' },
      env: {
        name_pattern: '^[A-Za-z_][A-Za-z0-9_]*$',
        max_vars: 32,
        value_max: 2048,
        forbidden_chars: '"$\\`',
        reserved: [...RESERVED, 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_REGION'].sort(),
        reserved_prefixes: ['AWS_', 'LD_', 'BASH_FUNC_'],
      },
      run_payload: { limit: 4096, used_bytes: 1342 },
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
    const body = (await request.json()) as {
      name?: string;
      size?: MSize;
      idle_minutes?: number;
      max_minutes?: number;
      max_hours?: number;
      suspended_minutes?: number | null;
      auto_resume?: boolean;
      env?: Record<string, string>;
    };
    const name = (body.name ?? '').trim();
    if (!name) return HttpResponse.json({ detail: 'Name is required.' }, { status: 422 });
    const size: MSize = body.size && body.size in SIZES ? body.size : 'medium';
    const maxMinutes = body.max_minutes ?? Math.round((body.max_hours ?? 8) * 60);
    const idle = body.idle_minutes ?? 15;
    if (idle >= maxMinutes) return HttpResponse.json({ detail: 'the idle time before suspending must be shorter than the lifetime' }, { status: 422 });
    const bad = Object.keys(body.env ?? {}).filter((k) => !/^[A-Za-z_][A-Za-z0-9_]*$/.test(k) || RESERVED.includes(k) || k.toUpperCase().startsWith('AWS_'));
    if (bad.length) return HttpResponse.json({ detail: `environment variables: ${bad.join(', ')} is reserved or invalid` }, { status: 422 });
    // No pre-check in the backend: it always calls RunMicrovm. The mock plays AWS refusing past the quota.
    if (usedGb() + SIZES[size].gb > QUOTA_GB) {
      const used = usedGb();
      return HttpResponse.json(
        {
          detail: `AWS refused ${SIZES[size].label} (${SIZES[size].gb} GB) with ServiceQuotaExceededException: “${MOCK_AWS_QUOTA_MESSAGE}”. Our estimate: ${used} of ${QUOTA_GB} GB of MicroVM memory in use in the account, ${Math.max(0, QUOTA_GB - used)} GB free; suspended sandboxes count. Pick a smaller size or terminate a sandbox.`,
        },
        { status: 409 },
      );
    }
    const sb = createSandbox({
      name,
      size,
      idleMinutes: idle,
      maxMinutes,
      suspendedMinutes: body.suspended_minutes ?? null,
      autoResume: body.auto_resume !== false,
      envNames: Object.keys(body.env ?? {}),
    });
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
      idle_policy: { auto_resume: sb.auto_resume, max_idle_s: sb.idle_minutes * 60, suspended_duration_s: sb.suspended_duration_s ?? sb.max_duration_s },
      stats: live && running ? stats(sb) : null,
      pane: live && running ? paneOf(sb.id) : null,
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
    const live = db.sandboxes.filter((s) => s.state !== 'TERMINATED');
    return HttpResponse.json({
      used_gb: usedGb(),
      quota_gb: QUOTA_GB,
      free_gb: Math.max(0, QUOTA_GB - usedGb()),
      vm_memory_gb: GB_PER_SANDBOX,
      user_microvms: live.length,
      user_gb: usedGb(),
      other_gb: 0,
      other_unknown_size: 0,
      account_microvms: live.length,
      scope: 'account',
      segments: live.map((s) => ({ id: s.id, name: s.name, state: s.state, mine: true, gb: SIZES[s.size].gb })),
      quota_source: { kind: 'service_quotas', service_code: 'lambda', quota_code: 'L-CD1C0CC4', name: 'Max allocated ARM_64 MicroVM memory', adjustable: true, unit: 'Gigabytes', fetched_at: new Date().toISOString(), error: null },
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
