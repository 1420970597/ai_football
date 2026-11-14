// Personal-history UI contracts over isolated receipts, never a real account.
import { chromium } from '/opt/verification/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fixtureRoute, instant, fixtures } from './visual-fixture.mjs';

const output = process.env.BROWSER_EVIDENCE_DIR || '/evidence';
await fs.mkdir(output, { recursive: true });
const server = http.createServer(async (req, res) => {
  try {
    const pathname = new URL(req.url, 'http://localhost').pathname;
    const target = path.resolve('/work/web/app', pathname.startsWith('/assets/') ? pathname.slice(8) : 'index.html');
    if (!target.startsWith('/work/web/app/')) throw Error('invalid path');
    res.writeHead(200, { 'Content-Type': target.endsWith('.js') ? 'text/javascript' : target.endsWith('.css') ? 'text/css' : 'text/html' });
    res.end(await fs.readFile(target));
  } catch { res.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(8080, '127.0.0.1', resolve));
const browser = await chromium.launch({ headless: true, args: ['--disable-dev-shm-usage'] });
const results = [], errors = [];
const completed = {
  status: 'completed', account_id: 'personal-account', binding_revision: 1,
  window_start: '2026-11-04T12:00:00Z', window_end: '2026-11-11T12:00:00Z', observed_at: '2026-11-11T12:00:01Z',
  order_count: 4, match_count: 2, unsettled_match_count: 1, settled_match_count: 1,
  association_complete: true, unmapped_order_count: 0,
  matches: [
    { fixture_id: 'leyu:123', bought: true, order_count: 3, unsettled_count: 0, settled_count: 3,
      currencies: [{ currency: 'CNY', settled_net: '20', unknown_profit_count: 1, fees_unknown: true },
        { currency: 'USD', settled_net: '-3', unknown_profit_count: 0, fees_unknown: true }] },
    { fixture_id: 'leyu:456', bought: true, order_count: 1, unsettled_count: 1, settled_count: 0, currencies: [] },
  ],
};
try {
  for (const width of [1440, 390]) {
    for (const mode of ['completed', 'partial', 'failed', 'unbound', 'verification_required', 'guest']) {
      const context = await browser.newContext({ viewport: { width, height: 960 }, locale: 'zh-CN' });
      const page = await context.newPage(), reads = [], writes = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/api/v1/**', async route => {
        const req = route.request(), url = new URL(req.url()), p = url.pathname.replace('/api/v1', '');
        if (req.method() !== 'GET') writes.push(p);
        if (p === '/auth/me' && mode !== 'guest') return route.fulfill({ json: {
          user: { id: 'personal-user', display_name: '隔离测试用户', email: 'history@test.invalid' }, membership: null,
        } });
        if (p === '/public/matches') return route.fulfill({ json: {
          items: ['123', '456', '789'].map((id, i) => ({ ...fixtures[i], id: 'leyu:' + id, expert_signals: [], sport: 'football' })),
          next_cursor: null, server_time: instant,
        } });
        if (p.startsWith('/accounts/history')) {
          reads.push({ p, method: req.method(), context: req.headers()['x-session-context'] });
          if (mode === 'failed') return route.fulfill({ status: 503, json: { detail: '隔离历史服务不可用' } });
          const result = mode === 'completed' ? completed : mode === 'partial'
            ? { ...completed, status: 'partial', matches: completed.matches.slice(0, 1), association_complete: false }
            : { status: mode, matches: [], association_complete: false };
          return route.fulfill({ json: mode === 'completed' && req.method() === 'POST'
            ? { status: 'queued', matches: [], association_complete: false } : result });
        }
        return fixtureRoute(route);
      });
      await page.goto('http://127.0.0.1:8080/');
      await page.locator('[data-fixture="leyu:123"]').waitFor();
      const summary = page.locator('#account-history-summary');
      if (mode === 'completed' || mode === 'partial') {
        await page.waitForFunction(() => document.querySelector('[data-account-fixture="leyu:123"]').textContent.includes('+20.00'));
        const first = await page.locator('[data-account-fixture="leyu:123"]').innerText();
        assert.match(first, /本场已买入/);
        assert.match(first, /\+20.00 CNY/);
        assert.match(first, /-3.00 USD/);
        assert.match(first, /手续费未提供/);
        assert.match(first, /1 单盈亏未提供/);
        assert.match(await summary.innerText(), /按整场比赛关联/);
        if (mode === 'completed') {
          assert.match(await page.locator('[data-account-fixture="leyu:456"]').innerText(), /待结算/);
          assert.doesNotMatch(await page.locator('[data-account-fixture="leyu:456"]').innerText(), /0.00|9000/);
          assert.match(await page.locator('[data-account-fixture="leyu:789"]').innerText(), /近7日无记录/);
        } else {
          assert.match(await summary.innerText(), /仅部分核验/);
          assert.match(await page.locator('[data-account-fixture="leyu:789"]').innerText(), /未核验/);
        }
      } else {
        const expected = { failed: '暂不可用', unbound: '尚未绑定', verification_required: '尚未通过校验', guest: '登录后查看' }[mode];
        await page.waitForFunction(text => document.querySelector('#account-history-summary').textContent.includes(text), expected);
        if (mode !== 'guest') assert.match(await page.locator('[data-account-fixture="leyu:789"]').innerText(), /未核验/);
      }
      await page.locator('#refresh-matches').click();
      await page.locator('.score-tabs [data-phase="finished"]').click();
      await page.locator('[data-fixture="leyu:123"]').waitFor();
      assert.equal(writes.filter(p => p === '/accounts/history/refresh').length, mode === 'guest' ? 0 : 1);
      assert.ok(writes.every(p => p === '/accounts/history/refresh'));
      assert.ok(reads.every(r => r.context === 'user'));
      if (mode === 'guest') assert.equal(await page.locator('.match-account-history:visible').count(), 0);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      if (mode === 'completed') await page.screenshot({ path: `${output}/history-${width}.png`, fullPage: true });
      // A late response from the previous owner cannot repopulate personal data.
      if (mode === 'completed') {
        const isolated = await page.evaluate(async () => {
          const { createAccountHistory } = await import('/assets/account-history.js');
          const history = createAccountHistory();
          const original = window.fetch;
          let resolve;
          window.fetch = () => new Promise(r => { resolve = r; });
          history.identify('previous-owner');
          const pending = history.load();
          history.identify(null);
          resolve(new Response(JSON.stringify({ status: 'completed', association_complete: true,
            matches: [{ fixture_id: 'leyu:123', order_count: 1, currencies: [] }] }), { status: 200 }));
          await pending;
          window.fetch = original;
          return { text: document.querySelector('#account-history-summary').textContent,
            markup: history.matchMarkup('leyu:123') };
        });
        assert.match(isolated.text, /登录后查看/);
        assert.equal(isolated.markup, '');
      }
      results.push({ width, mode, historyRequests: reads.length, writes });
      await context.close();
    }
  }
  assert.deepEqual(errors, []);
  await fs.writeFile(`${output}/history-results.json`, JSON.stringify({ results, errors }, null, 2));
  console.log(JSON.stringify({ passed: results.length, errors }));
} finally { await browser.close(); server.close(); }
