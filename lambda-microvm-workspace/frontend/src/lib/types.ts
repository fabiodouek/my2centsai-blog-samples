// Types for the backend contract in SPEC.md section 2. The normalizers in api.ts accept a
// few spelling variants (snake/camel case, seconds/ms/ISO timestamps) and map them to these.

export type SandboxState =
  | 'PENDING'
  | 'RUNNING'
  | 'SUSPENDING'
  | 'SUSPENDED'
  | 'RESUMING'
  | 'TERMINATING'
  | 'TERMINATED'
  | 'UNKNOWN';

export interface Sandbox {
  id: string;
  name: string;
  state: SandboxState;
  createdAt: number | null; // epoch ms
  startedAt: number | null; // epoch ms
  maxDurationS: number;
  idleMinutes: number;
  imageVersion: string | null;
  expiresAt: number | null;
  stateReason: string | null;
  /** A terminal is open on this sandbox in the backend right now (any browser). */
  attachedAnywhere: boolean;
  /** Last terminal attach/detach/keystroke or live agent call the backend saw; null = unknown. */
  lastActivityAt: number | null;
  /** When the backend saw it end (TERMINATING/TERMINATED only). */
  terminatedAt: number | null;
}

export interface AppConfig {
  proxyMode: string;
  quotaGb: number;
  maxDurationS: number;
  pricing: Record<string, unknown>;
  /** The proxy allowlist (proxy_allowlist). Entries may start with "*.". */
  allowlist: string[] | null;
  vmMemoryGb: number;
  defaultIdleMinutes: number;
  pricingSource: string | null;
  pricingAsOf: string | null;
  /** Part 2's sandbox subnets: a DNS query from one of these came from a sandbox. Empty = unknown. */
  sandboxCidrs: string[];
}

export interface Me {
  sub: string;
  email: string;
  groups: string[];
  /** In the Cognito `admins` group: may change the proxy policy and revoke/restore credentials. */
  isAdmin: boolean;
}

export interface Quota {
  /** Every live MicroVM in the account (the quota is per account). */
  usedGb: number;
  quotaGb: number;
  /** This user's live sandboxes, when the backend reports it. */
  userGb: number | null;
  vmMemoryGb: number;
}

export type CredentialStatus = 'active' | 'revoking' | 'revoked' | 'restoring' | 'unknown';

export interface Credential {
  name: string;
  /** builtin: part 2's anthropic/github/demo. custom: added from the Proxy policy page. */
  kind: 'builtin' | 'custom';
  placeholderPrefix: string;
  /** Deny policy in place (also true while revoking); null when the backend couldn't read the policy. */
  revoked: boolean | null;
  /** revoking: policy set, iron-proxy restarting (~3 min). restoring: policy removed, waiting for a swap (~1 min). */
  status: CredentialStatus;
  statusDetail: string | null;
  statusSince: number | null;
  uses15m: number;
  hosts: Record<string, number>;
  lastUsed: number | null;
}

export interface IdlePolicy {
  autoResumeEnabled: boolean;
  maxIdleDurationS: number;
  suspendedDurationS: number | null;
}

export interface Stats {
  cpuPct: number;
  cpuCount: number;
  memTotalMb: number;
  memUsedMb: number;
  load1: number;
  diskUsedMb: number;
  diskTotalMb: number;
  uptimeS: number;
  procs: number;
}

export interface Vitals {
  state: SandboxState;
  startedAt: number | null;
  maxDurationS: number;
  expiresAt: number | null;
  idlePolicy: IdlePolicy | null;
  remainingS: number | null;
  stats: Stats | null;
  /** Set when live=1 was requested but the agent could not be reached. */
  statsError: string | null;
  /** What runs in the terminal's tmux pane (live=1 only); null = unknown. */
  pane: Pane | null;
}

export interface Pane {
  command: string;
  claudeRunning: boolean;
  /** A shell prompt is in the foreground. */
  atShell: boolean;
}

export interface ClaudeSession {
  sessionId: string;
  project: string;
  model: string;
  turns: number;
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheCreationTokens: number;
  firstTs: number | null;
  lastTs: number | null;
  estCostUsd: number | null;
  pricedAs: string | null;
}

export interface ClaudeUsage {
  sessions: ClaudeSession[];
  totals: {
    turns: number;
    inputTokens: number;
    outputTokens: number;
    cacheReadTokens: number;
    cacheCreationTokens: number;
    estCostUsd: number | null;
  };
  unpricedModels: string[];
  pricingSource: string | null;
  pricingAsOf: string | null;
  /** The backend's user-facing sentence, e.g. "Estimated from Anthropic list prices as of 2026-11-03". */
  pricingNote: string | null;
}

export interface ProxyEvent {
  key: number;
  ts: number;
  action: 'allow' | 'reject' | string;
  status: number | null;
  method: string;
  host: string;
  path: string;
  rejectedBy: string | null;
  secrets: string[];
  mode: string | null;
  durationMs: number | null;
  /** What iron-proxy's aws_auth transform did, or null when it didn't act. */
  aws: AwsAnnotation | null;
}

export interface AwsAnnotation {
  service: string;
  region: string | null;
  /** aws_auth re-signed the request with the scoped role. */
  signed: boolean;
  /** e.g. service_not_allowed, region_not_allowed; null when signed. */
  rejected: string | null;
}

export interface DnsEvent {
  key: number;
  ts: number;
  query: string;
  qtype: string;
  rcode: string;
  srcaddr: string;
}

export interface LifecycleEvent {
  key: number;
  ts: number;
  id: string;
  state: SandboxState;
  prev: SandboxState | null;
  name: string | null;
}

/** Terminal connection states shown in the overlay. The first five come from the server. */
export type TermState =
  | 'idle'
  | 'connecting'
  | 'attached'
  | 'reconnecting'
  | 'suspended'
  | 'exited'
  | 'detached'
  | 'terminated'
  | 'error';

// ---------- proxy policy (control plane), backend/API.md "Proxy policy" ----------

/** Returned by every policy write: what the sidecar has to apply before the change is live. */
export interface ApplyRef {
  configVersion: number | null;
  scopeVersion: number | null;
  requestedAt: number | null;
}

export interface PolicyOverview {
  enabled: boolean;
  /** Why the control plane is off (enabled = false). */
  reason: string | null;
  editable: boolean;
  note: string | null;
  configVersion: number | null;
  scopeVersion: number | null;
  region: string | null;
  limits: { policyChars: number; runPayloadBytes: number; runPayloadBytesUsed: number | null };
}

export interface AllowHost {
  host: string;
  /** "aws": follows the AWS access services, can't be edited here. */
  managed: string | null;
  service: string | null;
}

export interface Allowlist {
  version: number | null;
  hosts: AllowHost[];
}

export interface PolicyCredential {
  name: string;
  kind: 'builtin' | 'custom';
  env: string | null;
  placeholder: string | null;
  hosts: string[];
  /** [] = every header. */
  headers: string[];
  query: boolean;
  body: boolean;
  secretName: string | null;
  status: CredentialStatus;
  statusDetail: string | null;
  onAllowlist: boolean | null;
  createdAt: number | null;
  createdBy: string | null;
  rotatedAt: number | null;
  rotatedBy: string | null;
  rotatable: boolean;
  deletable: boolean;
}

export interface NewCredential {
  name: string;
  env: string;
  hosts: string[];
  headers: string[];
  query: boolean;
  body: boolean;
  value: string;
}

export type AccessLevel = 'List' | 'Read' | 'Write' | 'Permissions' | 'Tagging';
export const ACCESS_LEVELS: AccessLevel[] = ['List', 'Read', 'Write', 'Permissions', 'Tagging'];

/** One action from AWS's Service Authorization Reference, checked against the workspace's ceiling. */
export interface AwsAction {
  /** prefix:Name, e.g. s3:GetObject */
  action: string;
  /** IAM service prefix */
  service: string;
  name: string;
  level: AccessLevel;
  /** The selected role allows it, it isn't blocked, and its service can be mapped to iron-proxy. */
  allowed: boolean;
  /** Allowed only on specific resources or under conditions (every matching Allow is scoped). */
  limited: boolean;
  reason: string | null;
}

export interface AwsWildcard {
  action: string;
  label: string;
  covers: number;
  allowed: boolean;
  reason: string | null;
}

/** A service in the catalog list (no actions: those load on demand). */
export interface AwsServiceSummary {
  id: string;
  name: string;
  actions: number;
  allowed: number;
  levels: Partial<Record<AccessLevel, number>>;
  /** false: no endpoint mapping, nothing in it can be applied */
  enabled: boolean;
  reason: string | null;
  signingNames: string[];
}

export interface AwsServiceDetail {
  id: string;
  name: string;
  enabled: boolean;
  reason: string | null;
  signingNames: string[];
  hosts: string[];
  actions: AwsAction[];
  wildcards: AwsWildcard[];
}

export interface CatalogSource {
  url: string | null;
  fetchedAt: number | null;
  ageS: number | null;
  stale: boolean;
  error: string | null;
  services: number | null;
  actions: number | null;
  fetchS: number | null;
}

/** One of the four roles iron-proxy can sign sandbox AWS calls as (part 2, control_plane=true). */
export interface AwsRole {
  id: string;
  description: string;
  default: boolean;
  /** e.g. builder: "Can create billable resources in this account." */
  warning: string | null;
}

export interface CatalogCeiling {
  /** The role the catalog's allowed/reason were evaluated for */
  role: string | null;
  /** Its policies: managed names and "inline: <name>" */
  policies: string[];
  /** Older backends: the single ceiling policy (ReadOnlyAccess) */
  policy: string | null;
  version: string | null;
  fetchedAt: number | null;
  ageS: number | null;
  stale: boolean;
  error: string | null;
}

export interface AwsCatalog {
  region: string | null;
  /** The role this catalog was evaluated for */
  role: string | null;
  roles: AwsRole[];
  source: CatalogSource;
  ceiling: CatalogCeiling;
  levels: AccessLevel[];
  services: AwsServiceSummary[];
  baseline: Record<string, unknown> | null;
  policyChars: number;
}

export interface AwsSearchResult {
  q: string;
  level: AccessLevel | null;
  total: number;
  truncated: boolean;
  services: { id: string; name: string; enabled: boolean; matches: number }[];
  actions: AwsAction[];
}

export interface AwsPreview {
  role: string | null;
  /** false: no session policy, the role's full permissions ("allow *"); policy is null */
  sessionPolicy: boolean;
  actions: string[];
  services: string[];
  allowedServices: string[];
  hosts: string[];
  policy: Record<string, unknown> | null;
  policyJson: string;
  chars: number;
  limit: number;
  fits: boolean;
}

export interface AwsScope {
  version: number | null;
  role: string | null;
  sessionPolicy: boolean;
  actions: string[];
  services: string[];
  updatedAt: number | null;
  updatedBy: string | null;
  preview: AwsPreview | null;
}

export interface PolicyStatus {
  enabled: boolean;
  applied: boolean;
  pending: boolean;
  target: ApplyRef;
  latencyS: number | null;
  config: { version: number | null; appliedAt: number | null };
  scope: {
    version: number | null;
    /** What the sidecar assumed: the role and whether a session policy narrowed it */
    role: string | null;
    sessionPolicy: boolean | null;
    sessionName: string | null;
    sourceIdentity: string | null;
    expiration: number | null;
    assumedAt: number | null;
    /** STS AssumeRole: share of the maximum session token size the token uses, and its size in bytes */
    sessionTokenUtilization: number | null;
    sessionTokenSize: number | null;
    /** Deprecated by STS in favour of the two above; shown only when they're missing */
    packedPolicySize: number | null;
    error: string | null;
  };
  reload: { at: number | null; ok: boolean | null; httpStatus: number | null; error: string | null };
  sidecar: { task: string | null; updatedAt: number | null; heartbeatAgeS: number | null; stale: boolean };
  error: string | null;
}

export type ChangeKind =
  | 'allowlist.add'
  | 'allowlist.remove'
  | 'credential.create'
  | 'credential.rotate'
  | 'credential.delete'
  | 'credential.revoke'
  | 'credential.restore'
  | 'aws.scope'
  | string;

export interface PolicyChange {
  id: string;
  at: number | null;
  actor: string | null;
  kind: ChangeKind;
  summary: string;
  detail: Record<string, unknown> | null;
  configVersion: number | null;
  scopeVersion: number | null;
}
