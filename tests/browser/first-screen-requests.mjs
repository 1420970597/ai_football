// First-screen request budget over synthetic endpoints: a cold start reads each
// endpoint at most once, and a sign-in re-reads the identities exactly once.
import { chromium } from '/opt/verification/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fixtureRoute, instant } from './visual-fixture.mjs';

const output = process.env.BROWSER_EVIDENCE_DIR || '/evidence';
const appRoot = '/work/web/app';
await fs.mkdir(output, { recursive: true });
const server = http.createServer(async (req, res) => {
  try {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    const target = path.resolve(appRoot, pathname.startsWith('/assets/') ? pathname.slice(8) : 'index.html');
    if (!target.startsWith(appRoot + '/')) throw Error('invalid path');
    const body = await fs.readFile(target);
    res.writeHead(200, { 'Content-Type': target.endsWith('.js') ? 'text/javascript' : target.endsWith('.css') ? 'text/css' : 'text/html' });
    res.end(body);
  } catch { res.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(8080, '127.0.0.1', resolve));
const browser = await chromium.launch({ headless: true, args: ['--disable-dev-shm-usage'] });

const user = { user: { id: 'budget-user', display_name: '隔离测试用户', email: 'budget@test.invalid' }, membership: null };
const partialMessage = '余额校验已成功；部分历史订单暂未核对，系统将继续查单，相关未结本金仍计入敞口。';
const account = {
  account: { id: 'budget-account', binding_revision: 1, account_label: '隔离账户', health_state: 'healthy', renewal_state: 'ready',
    last_verified_at: instant, available_balance: '100', balance_observed_at: instant },
  betting: { auto_enabled: false, emergency_stopped: false, expert_selection_mode: 'manual', allowed_experts: [],
    stake_per_order: '2', max_per_order: '3', daily_limit: '10', open_exposure_limit: '5', daily_timezone: 'Asia/Shanghai' },
  // A refresh that verified the balance but left some orders unreconciled is a success with a note.
  tasks: [{ id: 'budget-task', kind: 'refresh', state: 'succeeded', last_error: 'order_reconciliation_partial', message: partialMessage,
    provider_code: '', recovery_action: null, current_binding: true, updated_at: instant }],
  execution_capability: { live_enabled: true, supported_sports: ['football', 'basketball'], supported_phases: ['scheduled', 'live'],
    prematch: 'supported', basketball: 'full_time_winner_total_handicap' },
};
const manualBet = { enabled: true, live_enabled: true, account_ready: true, entitlement: true, emergency_stopped: false,
  stake_per_order: '2', daily_limit: '10', open_exposure_limit: '5', daily_timezone: 'Asia/Shanghai' };
const history = { status: 'completed', account_id: 'budget-account', binding_revision: 1, observed_at: instant, order_count: 0,
  match_count: 0, unsettled_match_count: 0, settled_match_count: 0, association_complete: true, unmapped_order_count: 0, matches: [] };

const once = ['/auth/me', '/admin/me'];
// Admin and QR code scripts stay off every first screen that does not need them.
const lazy = { '/assets/qrcode.js': 0, '/assets/admin.js': 0 };
// Exact counts per first screen; every other request is still capped at one.
const scenarios = [
  { name: 'guest-scores', hash: '#scores', expect: { '/public/experts': 1, '/public/matches': 1, '/public/signals': 0,
    '/accounts/leyu/manual-betting': 0, 'POST /accounts/history/refresh': 0 } },
  { name: 'guest-experts', hash: '#experts', expect: { '/public/experts': 1, '/public/signals': 1 } },
  { name: 'guest-account', hash: '#account', expect: { '/public/experts': 1, '/accounts/leyu': 0 } },
  { name: 'guest-membership', hash: '#membership', expect: { '/public/experts': 1, '/public/membership/plans': 1 } },
  { name: 'guest-admin', hash: '#admin', expect: { '/public/experts': 1, '/assets/admin.js': 1 } },
  { name: 'user-scores', hash: '#scores', signedIn: true, expect: { '/public/experts': 1, '/accounts/leyu/manual-betting': 1,
    'POST /accounts/history/refresh': 1, '/accounts/history': 0 } },
  { name: 'user-account', hash: '#account', signedIn: true, expect: { '/public/experts': 1, '/accounts/leyu': 1 } },
  { name: 'sign-in', hash: '#scores', signIn: true, expect: { 'POST /auth/login': 1, '/accounts/leyu': 1, '/public/experts': 1 } },
];
const results = [], errors = [], failures = [];
try {
  for (const scenario of scenarios) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 960 }, locale: 'zh-CN' });
    const page = await context.newPage();
    let signedIn = !!scenario.signedIn, counts = {};
    page.on('pageerror', error => errors.push(`${scenario.name}: ${error.message}`));
    page.on('request', request => {
      const url = new URL(request.url());
      const key = (request.method() === 'GET' ? '' : request.method() + ' ') + url.pathname.replace('/api/v1', '');
      counts[key] = (counts[key] || 0) + 1;
    });
    await page.route('**/api/v1/**', async route => {
      const request = route.request(), p = new URL(request.url()).pathname.replace('/api/v1', '');
      if (p === '/auth/login') { signedIn = true; return route.fulfill({ json: { user: { id: user.user.id }, csrf_token: 'test-csrf' } }); }
      if (signedIn && p === '/auth/me') return route.fulfill({ json: user });
      if (signedIn && p === '/accounts/leyu') return route.fulfill({ json: account });
      if (signedIn && p === '/accounts/leyu/manual-betting') return route.fulfill({ json: manualBet });
      if (signedIn && p.startsWith('/accounts/history')) return route.fulfill({ json: history });
      return fixtureRoute(route);
    });
    const settle = async () => { await page.waitForLoadState('networkidle'); await page.waitForTimeout(300); };
    await page.goto('http://127.0.0.1:8080/' + scenario.hash);
    await settle();
    if (scenario.signIn) {
      counts = {};
      await page.locator('#account-entry').click();
      await page.locator('#auth-form [name=email]').fill('budget@test.invalid');
      await page.locator('#auth-form [name=password]').fill('fixture-password');
      await page.locator('#auth-form button').first().click();
      await page.locator('#account-content:not([hidden])').waitFor();
      await settle();
    }
    const check = (condition, message) => { if (!condition) failures.push(`${scenario.name}: ${message}`); };
    for (const key of once) check(counts[key] === 1, `${key} requested ${counts[key] || 0} times, expected 1`);
    for (const [key, expected] of Object.entries({ ...lazy, ...scenario.expect }))
      check((counts[key] || 0) === expected, `${key} requested ${counts[key] || 0} times, expected ${expected}`);
    for (const [key, count] of Object.entries(counts)) check(count <= 1, `${key} requested ${count} times`);
    if (scenario.name === 'user-account') {
      const health = await page.locator('#account-health').textContent();
      const verification = await page.locator('#account-verification').textContent();
      check(health === '已通过校验', `partial reconciliation health shows "${health}"`);
      check(verification.includes('账户刷新 · 校验成功') && verification.includes(partialMessage), 'partial reconciliation note missing');
      check(!verification.includes('校验失败') && !verification.includes('order_reconciliation_partial'), 'partial reconciliation shown as a failure');
    }
    results.push({ name: scenario.name, counts: Object.fromEntries(Object.entries(counts).sort()) });
    await context.close();
  }
  await fs.writeFile(`${output}/first-screen-requests.json`, JSON.stringify({ results, failures, errors }, null, 2));
  console.log(JSON.stringify({ passed: results.length - new Set(failures.map(text => text.split(':')[0])).size, failures, errors }));
  assert.deepEqual(failures, []);
  assert.deepEqual(errors, []);
} finally { await browser.close(); server.close(); }
