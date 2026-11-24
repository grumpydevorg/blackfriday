import { createHmac, randomBytes } from 'node:crypto';
import { RequestError, equal } from './game.mjs';

export const COOKIE = 'bf_host';
export const SESSION_SECONDS = 43200;
export const REFRESH_MS = 30_000;
export const MAX_FAILURES = 10;
export const WINDOW_MS = 600_000;

const mac = (secret, text) => createHmac('sha256', secret).update(text).digest('hex');

/**
 * Presenter sign-in. `load()` returns { key, passphrase } (passphrase may be undefined);
 * `throttle` has reserve(now) -> boolean and release(now). Nothing here logs or returns a secret.
 */
export function presenterAuth({ load, throttle, origin, now = Date.now }) {
  let cached, loadedAt = -Infinity, loading;
  async function secrets() {
    if (now() - loadedAt < REFRESH_MS) return cached;
    loading ??= load().then(value => { cached = value; loadedAt = now(); return value; }).finally(() => { loading = undefined; });
    return loading;
  }
  // The cookie's MAC key depends on both secrets, so changing either signs every device out.
  const macKey = ({ key, passphrase }) => mac(key, passphrase);
  const publicOrigin = headers => origin ?? `https://${headers.host}`;

  async function login(supplied) {
    const stored = await secrets();
    if (!stored.passphrase) throw new RequestError(503, 'No presenter passphrase set');
    if (!await throttle.reserve(now())) throw new RequestError(429, 'Too many failed attempts. Try again in a few minutes.');
    if (typeof supplied !== 'string' || !equal(supplied, stored.passphrase)) throw new RequestError(401, 'Wrong passphrase');
    await throttle.release(now());
    const expiry = Math.floor(now() / 1000) + SESSION_SECONDS;
    const nonce = randomBytes(16).toString('hex');
    return `${COOKIE}=${expiry}.${nonce}.${mac(macKey(stored), `${expiry}.${nonce}`)}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=${SESSION_SECONDS}`;
  }
  const logout = () => `${COOKIE}=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0`;

  async function cookieValid(headers) {
    const match = /(?:^|;\s*)bf_host=(\d{1,12})\.([0-9a-f]{32})\.([0-9a-f]{64})(?:;|$)/.exec(headers.cookie ?? '');
    if (!match) return false;
    const stored = await secrets();
    if (!stored.passphrase) return false;
    return Number(match[1]) > Math.floor(now() / 1000)
      && equal(match[3], mac(macKey(stored), `${match[1]}.${match[2]}`));
  }

  /** Throws 403 unless this request may use presenter routes: a valid cookie, or the presenter key header. */
  async function authorize(headers, method) {
    if (method === 'POST' && headers['x-rehearsal-action'] !== '1') throw new RequestError(403, 'Use the presenter page');
    if (headers['x-presenter-key']) {
      const stored = await secrets();
      if (stored.key && equal(headers['x-presenter-key'], stored.key)) return;
    } else if (await cookieValid(headers)) {
      if (headers.origin !== undefined && headers.origin !== publicOrigin(headers)) throw new RequestError(403, 'Wrong origin');
      return;
    }
    throw new RequestError(403, 'Sign in as presenter');
  }
  return { login, logout, authorize, publicOrigin, checkOrigin(headers) {
    if (headers.origin !== undefined && headers.origin !== publicOrigin(headers)) throw new RequestError(403, 'Wrong origin');
  } };
}

/** The same windowed counter the Web Function keeps in DynamoDB, for tests. */
export function memoryThrottle(now = Date.now) {
  let start = -Infinity, failures = 0;
  return {
    async reserve(at = now()) {
      if (at - start >= WINDOW_MS) { start = at; failures = 0; }
      if (failures >= MAX_FAILURES) return false;
      failures += 1;
      return true;
    },
    async release(at = now()) { if (at - start < WINDOW_MS && failures > 0) failures -= 1; },
    get failures() { return failures; },
  };
}

/** Which routes need the presenter: everything that starts compute or shows private state. */
export function routeKind(method, url) {
  if (method === 'POST' && url === '/api/host/login') return 'login';
  if (method === 'POST' && url === '/api/host/logout') return 'logout';
  if (url.startsWith('/api/host/') || (method === 'POST' && /^\/api\/(launch|migrate|compare|reset)$/.test(url))) return 'presenter';
  return 'public';
}
