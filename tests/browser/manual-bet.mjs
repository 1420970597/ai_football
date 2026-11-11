// One-click real-money entry: gate reasons, confirmation contents, and persistent outcome.
// Every request is routed; nothing here reaches a real account or a real order.
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

const signal = {
  id: 'signal-1', expert_id: 'expert-1', display_nickname: '林老师 TG', phase: 'recommendation',
  created_at: '2026-10-26T11:00:00Z', market_name: '全场 · 亚洲让球', period: 'full', market_kind: 'handicap',
  line: '-0.25', selection: 'home', recommendation_price: '1.96', outcome: 'pending',
  quote_time: '2026-10-26T11:11:00Z',
};
const match = { ...fixtures[0], id: 'leyu:900', sport: 'football', home_team: '利物浦', away_team: '曼城', expert_signals: [signal] };
const policy = (overrides = {}) => ({
  stake_per_order: '2', daily_limit: '10', open_exposure_limit: '5', daily_timezone: 'Asia/Shanghai',
  enabled: true, emergency_stopped: false, account_ready: true, live_enabled: true, entitlement: true, ...overrides,
});

async function open(page, mode) {
  await page.route('**/api/v1/**', async route => {
    const request = route.request(), p = new URL(request.url()).pathname.replace('/api/v1', '');
    if (p === '/auth/me') return route.fulfill({ json: { user: { id: 'bet-user', display_name: '隔离测试用户' }, membership: null } });
    if (p === '/accounts/leyu') return route.fulfill({ json: { account: null, binding_revision: 0 } });
    if (p === '/accounts/leyu/manual-betting') return route.fulfill({ json: policy(mode) });
    if (p === '/accounts/leyu/manual-betting/orders') {
      if (mode.__fail) return route.fulfill({ status: 409, json: { detail: '当前盘口已变化，请重新确认' } });
      return route.fulfill({ status: 202, json: { order: { id: 'order-1', state: mode.__repeat ? 'accepted' : 'pending', stake: '2', price: '1.96' } } });
    }
    if (p.startsWith('/accounts/history')) return route.fulfill({ json: {
      status: 'completed', account_id: 'bet-account', binding_revision: 1, observed_at: instant, order_count: 0,
      match_count: 0, unsettled_match_count: 0, settled_match_count: 0, association_complete: true, unmapped_match_count: 0,
      unmapped_order_count: 0, matches: [] } });
    if (p === '/public/matches') return route.fulfill({ json: { items: [match], next_cursor: null, server_time: instant } });
    return fixtureRoute(route);
  });
}

try {
  for (const width of [1440, 390]) {
    // 1. Each blocked reason disables the button and states why.
    for (const [mode, expected] of [
      [{ enabled: false }, '尚未保存每单下注金额'],
      [{ live_enabled: false }, '实盘执行当前未开放'],
      [{ account_ready: false }, '本人乐鱼账户尚未通过校验'],
      [{ entitlement: false }, '当前会员没有实盘交易权益'],
      [{ emergency_stopped: true }, '紧急停止'],
    ]) {
      const context = await browser.newContext({ viewport: { width, height: 900 }, locale: 'zh-CN' });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(`${width} blocked: ${error.message}`));
      await open(page, mode);
      await page.goto('http://127.0.0.1:8080/#scores?scope=recommended');
      await page.waitForLoadState('networkidle');
      const button = page.locator('[data-bet-signal]').first();
      await button.waitFor();
      assert.equal(await button.isDisabled(), true, `${width} ${expected}: button must be disabled`);
      const reason = (await button.getAttribute('aria-describedby')) || '';
      assert.ok(reason, `${width} ${expected}: reason must be linked`);
      const text = await page.locator('#' + reason).innerText();
      assert.ok(text.includes(expected), `${width}: expected "${expected}", saw "${text}"`);
      results.push({ width, case: 'blocked', mode: expected, ok: true });
      await context.close();
    }

    // 2. The buy target is large enough to tap, and the dialog names the bet.
    {
      const context = await browser.newContext({ viewport: { width, height: 900 }, locale: 'zh-CN' });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(`${width} enabled: ${error.message}`));
      await open(page, {});
      await page.goto('http://127.0.0.1:8080/#scores?scope=recommended');
      await page.waitForLoadState('networkidle');
      const button = page.locator('[data-bet-signal]').first();
      await button.waitFor();
      assert.equal(await button.isDisabled(), false, 'enabled policy must allow buying');
      const box = await button.boundingBox();
      assert.ok(box.height >= 44, `${width}: buy target height ${box.height} is below 44px`);
      await button.click();
      const message = page.locator('#confirm-message');
      await message.waitFor();
      const lines = await message.locator('span').allInnerTexts();
      const joined = lines.join('\n');
      for (const expected of ['利物浦 对 曼城', '林老师 TG', '亚洲让球', '1.96', '2.00 元']) {
        assert.ok(joined.includes(expected), `${width}: dialog must state "${expected}"; saw "${joined}"`);
      }
      // The period must appear exactly once: a market name that already carries it is not repeated.
      assert.equal(joined.split('全场').length - 1, 1, `${width}: period named once; saw "${joined}"`);
      assert.equal(await page.locator('#confirm-title').innerText(), '确认实盘买入');
      assert.ok((await page.locator('#confirm-yes').innerText()).includes('买入'), 'confirm button states the amount');
      results.push({ width, case: 'confirm', lines, ok: true });

      // 3. The outcome stays on the page instead of vanishing after a toast.
      await page.locator('#confirm-yes').click();
      const result = page.locator('[data-recommendation="signal-1"] .manual-bet-result');
      await result.waitFor();
      const text = await result.innerText();
      assert.ok(text.includes('已预留'), `outcome panel must persist; saw "${text}"`);
      await page.waitForTimeout(5200); // longer than the toast lifetime
      assert.equal(await result.isVisible(), true, 'outcome must still be visible after the toast expires');
      results.push({ width, case: 'result-persists', text, ok: true });
      await context.close();
    }

    // 4. A rejection is shown in the same place, in the server's words.
    {
      const context = await browser.newContext({ viewport: { width, height: 900 }, locale: 'zh-CN' });
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(`${width} rejected: ${error.message}`));
      await open(page, { __fail: true });
      await page.goto('http://127.0.0.1:8080/#scores?scope=recommended');
      await page.waitForLoadState('networkidle');
      await page.locator('[data-bet-signal]').first().click();
      await page.locator('#confirm-yes').click();
      const result = page.locator('[data-recommendation="signal-1"] .manual-bet-result');
      await result.waitFor();
      const text = await result.innerText();
      assert.ok(text.includes('当前盘口已变化'), `rejection reason must be shown; saw "${text}"`);
      assert.equal(await page.locator('[data-bet-signal]').first().isDisabled(), false, 'a rejected order must leave the button usable');
      results.push({ width, case: 'rejected', text, ok: true });
      await context.close();
    }
  }
  assert.deepEqual(errors, [], 'no page errors');
  await fs.writeFile(path.join(output, 'manual-bet-results.json'), JSON.stringify({ results, errors }, null, 2));
  console.log(JSON.stringify({ ok: results.length, errors }));
} finally {
  await browser.close();
  server.close();
}
