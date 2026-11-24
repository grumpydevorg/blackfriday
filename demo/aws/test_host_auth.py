import subprocess
from pathlib import Path

AWS_DIR = Path(__file__).parent

PRELUDE = """
import assert from 'node:assert/strict';
import { presenterAuth, memoryThrottle, routeKind } from './host_auth.mjs';
const ORIGIN = 'https://arena.example';
let clock = 1_800_000_000_000;
const now = () => clock;
let secrets = { key: 'k'.repeat(64), passphrase: 'correct horse battery' };
let loads = 0;
const throttle = memoryThrottle(now);
const auth = presenterAuth({ load: async () => { loads++; return structuredClone(secrets); }, throttle, origin: ORIGIN, now });
const post = (cookie, extra = {}) => ({ cookie, 'x-rehearsal-action': '1', ...extra });
const status = async promise => { try { await promise; return 200; } catch (error) { return error.status; } };
const cookieOf = setCookie => setCookie.split(';')[0];
"""


def javascript(source):
    return subprocess.check_output(['node', '--input-type=module', '-e', PRELUDE + source], cwd=AWS_DIR, text=True)


def test_sign_in_sets_a_strict_cookie_and_wrong_passphrase_counts():
    javascript("""
      assert.equal(await status(auth.login('wrong passphrase!')), 401);
      assert.equal(throttle.failures, 1);
      const setCookie = await auth.login('correct horse battery');
      assert.match(setCookie, /^__Host-bf_host=\\d+\\.[0-9a-f]{32}\\.[0-9a-f]{64}; HttpOnly; Secure; SameSite=Strict; Path=\\/; Max-Age=43200$/);
      assert.ok(!/Domain/i.test(setCookie), 'a __Host- cookie must not set Domain');
      assert.ok(!setCookie.includes('correct') && !setCookie.includes('kkkk'));
      assert.equal(throttle.failures, 1, 'a success does not count');
      await auth.authorize(post(cookieOf(setCookie)), 'POST');
      await auth.authorize({ cookie: cookieOf(setCookie) }, 'GET');
      assert.match(auth.logout(), /Max-Age=0/);
    """)


def test_eleventh_failure_in_the_window_is_429_even_with_the_right_passphrase():
    javascript("""
      for (let n = 0; n < 10; n++) assert.equal(await status(auth.login(`wrong ${n}`)), 401);
      assert.equal(await status(auth.login('wrong again')), 429);
      assert.equal(await status(auth.login('correct horse battery')), 429);
      clock += 600_001;
      await auth.login('correct horse battery');
    """)


def test_cookie_from_before_a_passphrase_change_expires_within_thirty_seconds():
    javascript("""
      const cookie = cookieOf(await auth.login('correct horse battery'));
      secrets = { ...secrets, passphrase: 'a different passphrase' };
      clock += 29_000;
      await auth.authorize(post(cookie), 'POST');
      clock += 2_000;
      assert.equal(await status(auth.authorize(post(cookie), 'POST')), 403);
      const next = cookieOf(await auth.login('a different passphrase'));
      await auth.authorize(post(next), 'POST');
      secrets = { ...secrets, key: 'z'.repeat(64) };
      clock += 31_000;
      assert.equal(await status(auth.authorize(post(next), 'POST')), 403);
      assert.ok(loads >= 3);
    """)


def test_expired_and_tampered_cookies_are_refused():
    javascript("""
      const cookie = cookieOf(await auth.login('correct horse battery'));
      const [name, value] = cookie.split('=');
      const [expiry, nonce, mac] = value.split('.');
      assert.equal(await status(auth.authorize(post(`${name}=${Number(expiry) + 1}.${nonce}.${mac}`), 'POST')), 403);
      assert.equal(await status(auth.authorize(post(`${name}=${expiry}.${'0'.repeat(32)}.${mac}`), 'POST')), 403);
      assert.equal(await status(auth.authorize(post(`${name}=${expiry}.${nonce}.${'0'.repeat(64)}`), 'POST')), 403);
      assert.equal(await status(auth.authorize(post(`${name}=${expiry}.${nonce}`), 'POST')), 403);
      assert.equal(await status(auth.authorize(post(undefined), 'POST')), 403);
      await auth.authorize(post(cookie), 'POST');
      clock += 43_201_000;
      assert.equal(await status(auth.authorize(post(cookie), 'POST')), 403);
    """)


def test_presenter_posts_need_the_action_header_and_the_endpoints_own_origin():
    javascript("""
      const cookie = cookieOf(await auth.login('correct horse battery'));
      assert.equal(await status(auth.authorize({ cookie }, 'POST')), 403);
      assert.equal(await status(auth.authorize(post(cookie, { origin: 'https://evil.example' }), 'POST')), 403);
      assert.equal(await status(auth.authorize(post(cookie, { origin: 'null' }), 'POST')), 403);
      await auth.authorize(post(cookie, { origin: ORIGIN }), 'POST');
      assert.equal(await status(auth.login('x')), 401);
      assert.equal(await status((async () => auth.checkOrigin({ origin: 'https://evil.example' }))()), 403);
      auth.checkOrigin({});
    """)


def test_no_passphrase_set_gives_503_and_presenter_routes_stay_403():
    javascript("""
      secrets = { key: 'k'.repeat(64) };
      assert.equal(await status(auth.login('anything at all')), 503);
      assert.equal(throttle.failures, 0);
      assert.equal(await status(auth.authorize(post('__Host-bf_host=9999999999.' + 'a'.repeat(32) + '.' + 'b'.repeat(64)), 'POST')), 403);
      assert.equal(await status(auth.authorize({}, 'GET')), 403);
    """)


def test_the_presenter_key_header_is_a_second_way_in_and_reports_its_mode():
    javascript("""
      assert.equal(await auth.authorize(post(undefined, { 'x-presenter-key': 'k'.repeat(64) }), 'POST'), 'key');
      assert.equal(await status(auth.authorize(post(undefined, { 'x-presenter-key': 'wrong' }), 'POST')), 403);
      assert.equal(await status(auth.authorize({ 'x-presenter-key': 'k'.repeat(64) }, 'POST')), 403);
      const cookie = cookieOf(await auth.login('correct horse battery'));
      assert.equal(await auth.authorize(post(cookie), 'POST'), 'cookie');
    """)


def test_a_stored_passphrase_under_eight_characters_counts_as_unset():
    javascript("""
      secrets = { key: 'k'.repeat(64), passphrase: 'short' };
      assert.equal(await status(auth.login('short')), 503);
      assert.equal(throttle.failures, 0);
    """)


def test_the_cookie_is_found_by_exact_name_and_a_sibling_cannot_shadow_it():
    javascript("""
      const [, value] = cookieOf(await auth.login('correct horse battery')).split('=');
      const good = `__Host-bf_host=${value}`;
      await auth.authorize(post(`theme=dark; ${good}; other=1`), 'POST');
      assert.equal(await status(auth.authorize(post(`xx__Host-bf_host=${value}`), 'POST')), 403);
      assert.equal(await status(auth.authorize(post(`bf_host=${value}`), 'POST')), 403);
      // Two cookies of the same name are ambiguous: refuse rather than pick one.
      assert.equal(await status(auth.authorize(post(`__Host-bf_host=${'0'.repeat(10)}.${'a'.repeat(32)}.${'b'.repeat(64)}; ${good}`), 'POST')), 403);
    """)


def test_without_a_configured_origin_the_cookie_path_and_the_public_url_fail_closed():
    javascript("""
      const open = presenterAuth({ load: async () => structuredClone(secrets), throttle, origin: undefined, now });
      const cookie = cookieOf(await open.login('correct horse battery'));
      assert.equal(await status(open.authorize(post(cookie), 'POST')), 403, 'a cookie is worthless without an origin to check');
      assert.equal(await status((async () => open.publicOrigin())()), 503);
      assert.equal(await status((async () => open.checkOrigin({}))()), 403);
      // The presenter key path does not depend on the origin.
      assert.equal(await open.authorize(post(undefined, { 'x-presenter-key': 'k'.repeat(64) }), 'POST'), 'key');
    """)


def test_player_routes_need_no_credentials_and_presenter_routes_do():
    javascript("""
      for (const [method, url] of [['GET', '/'], ['GET', '/host'], ['GET', '/round.js'], ['GET', '/api/state'],
          ['POST', '/api/join'], ['POST', '/api/rename'], ['POST', '/api/pick']]) assert.equal(routeKind(method, url), 'public', url);
      for (const [method, url] of [['GET', '/api/host/state'], ['GET', '/api/host/qr.svg'], ['POST', '/api/host/start'],
          ['POST', '/api/host/close'], ['POST', '/api/host/reset'], ['POST', '/api/host/remove']]) assert.equal(routeKind(method, url), 'presenter', url);
      // The one-round routes are gone: they are no longer presenter routes, and the Web Function 404s them.
      for (const url of ['/api/launch', '/api/migrate', '/api/compare', '/api/reset']) assert.equal(routeKind('POST', url), 'public', url);
      assert.equal(routeKind('POST', '/api/host/login'), 'login');
      assert.equal(routeKind('POST', '/api/host/logout'), 'logout');
    """)


def test_remove_is_a_presenter_route():
    javascript("""
      assert.equal(routeKind('POST', '/api/host/remove'), 'presenter');
    """)
