import { UserManager, WebStorageStateStore, type User } from 'oidc-client-ts';
import { MOCK, cognitoOrigin, type RuntimeConfig } from './config';

// Authorization code + PKCE against the Cognito managed login. The ID token is what the
// backend verifies (aud = client id, token_use = id), so that is the token we send.

let mgr: UserManager | null = null;
let cfgRef: RuntimeConfig | null = null;
let renewing: Promise<User | null> | null = null;

export interface SessionUser {
  email: string;
  sub: string;
}

export function initAuth(cfg: RuntimeConfig): void {
  cfgRef = cfg;
  if (MOCK) return;
  const authority = `https://cognito-idp.${cfg.region}.amazonaws.com/${cfg.userPoolId}`;
  const domain = cognitoOrigin(cfg.cognitoDomain);
  mgr = new UserManager({
    authority,
    client_id: cfg.clientId,
    redirect_uri: `${window.location.origin}/`,
    post_logout_redirect_uri: `${window.location.origin}/`,
    response_type: 'code',
    scope: 'openid email',
    loadUserInfo: false,
    automaticSilentRenew: true,
    userStore: new WebStorageStateStore({ store: window.localStorage }),
    // Explicit metadata: Cognito's managed login lives on its own domain and its logout
    // endpoint is not the standard end_session_endpoint.
    metadata: {
      issuer: authority,
      authorization_endpoint: `${domain}/oauth2/authorize`,
      token_endpoint: `${domain}/oauth2/token`,
      userinfo_endpoint: `${domain}/oauth2/userInfo`,
      revocation_endpoint: `${domain}/oauth2/revoke`,
      jwks_uri: `${authority}/.well-known/jwks.json`,
    },
  });
}

function manager(): UserManager {
  if (!mgr) throw new Error('auth not initialised');
  return mgr;
}

/** Completes the redirect if the URL carries ?code=&state=. Returns an error message if sign-in failed. */
export async function handleRedirectCallback(): Promise<string | null> {
  if (MOCK) return null;
  const params = new URLSearchParams(window.location.search);
  if (params.has('error')) {
    const msg = params.get('error_description') || params.get('error') || 'Sign-in failed';
    window.history.replaceState({}, '', '/');
    return msg;
  }
  if (!params.has('code') || !params.has('state')) return null;
  try {
    await manager().signinRedirectCallback();
    return null;
  } catch (e) {
    return e instanceof Error ? e.message : 'Sign-in failed';
  } finally {
    window.history.replaceState({}, '', '/');
  }
}

function toSessionUser(u: User): SessionUser {
  const p = u.profile as Record<string, unknown>;
  return { email: String(p.email ?? ''), sub: String(p.sub ?? '') };
}

export async function currentUser(): Promise<SessionUser | null> {
  if (MOCK) return { email: 'dev@example.com', sub: 'mock-sub' };
  const u = await manager().getUser();
  if (!u) return null;
  if (u.expired) {
    const renewed = await renew();
    return renewed ? toSessionUser(renewed) : null;
  }
  return toSessionUser(u);
}

async function renew(): Promise<User | null> {
  if (!renewing) {
    renewing = manager()
      .signinSilent()
      .catch(() => null)
      .finally(() => {
        renewing = null;
      });
  }
  return renewing;
}

/** Returns a valid ID token, refreshing it if needed. `force` refreshes even if not expired (after a 401). */
export async function getIdToken(force = false): Promise<string | null> {
  if (MOCK) return 'mock-id-token';
  let u = await manager().getUser();
  if (!u || force || u.expired || (u.expires_in ?? 0) < 30) {
    u = (await renew()) ?? (force ? null : u);
  }
  return u?.id_token ?? null;
}

export async function signIn(): Promise<void> {
  if (MOCK) return;
  await manager().signinRedirect();
}

export async function signOut(): Promise<void> {
  if (MOCK) {
    window.location.reload();
    return;
  }
  await manager().removeUser();
  const cfg = cfgRef!;
  const logout = new URL(`${cognitoOrigin(cfg.cognitoDomain)}/logout`);
  logout.searchParams.set('client_id', cfg.clientId);
  logout.searchParams.set('logout_uri', `${window.location.origin}/`);
  window.location.assign(logout.toString());
}

export function onSessionLost(cb: () => void): () => void {
  if (MOCK || !mgr) return () => {};
  const m = mgr;
  const handler = () => cb();
  m.events.addSilentRenewError(handler);
  m.events.addUserSignedOut(handler);
  return () => {
    m.events.removeSilentRenewError(handler);
    m.events.removeUserSignedOut(handler);
  };
}
