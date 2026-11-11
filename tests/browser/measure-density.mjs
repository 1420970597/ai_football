// Measures first-screen density on the scores page: how many match rows are fully
// visible, row height, and the space taken above the list. Scratch measurement for
// the r2b density step; it asserts nothing, it reports.
import { chromium } from '/opt/verification/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import { fixtureRoute, fixtures, instant } from './visual-fixture.mjs';

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
const signal = (index) => ({
  id: `measure-signal-${index}`, expert_id: 'visual-expert', display_nickname: '林老师 TG',
  phase: 'recommendation', created_at: instant, market_name: '全场 · 亚洲让球', period: 'full',
  market_kind: 'handicap', line: '-0.25', selection: 'home', recommendation_price: '1.96',
  outcome: 'pending', quote_time: instant,
});
const withSignals = fixtures.map((item, index) => ({ ...item, expert_signals: [signal(index)] }));
const report = {};
try {
  for (const [label, width, height] of [['desktop', 1440, 960], ['mobile', 390, 844]]) {
    for (const [scope, items] of [['all', fixtures], ['recommended', withSignals]]) {
      const context = await browser.newContext({ viewport: { width, height }, locale: 'zh-CN', deviceScaleFactor: 1 });
      const page = await context.newPage();
      await page.route('**/api/v1/**', route => {
        const pathname = new URL(route.request().url()).pathname.replace('/api/v1', '');
        if (pathname === '/public/matches') return route.fulfill({ json: { items, next_cursor: null, server_time: instant } });
        return fixtureRoute(route);
      });
      await page.goto(`http://127.0.0.1:8080/#scores?scope=${scope}`);
      await page.waitForLoadState('networkidle');
      await page.waitForSelector('.match-row');
      report[`${label}-${scope}`] = await page.evaluate(() => {
        const rows = [...document.querySelectorAll('#matches-list .match-row')];
        const viewport = innerHeight;
        const fully = rows.filter(row => {
          const box = row.getBoundingClientRect();
          return box.top >= 0 && box.bottom <= viewport;
        });
        const first = rows[0]?.getBoundingClientRect();
        const list = document.querySelector('#matches-list')?.getBoundingClientRect();
        const head = document.querySelector('.score-table-head')?.getBoundingClientRect();
        return {
          viewport,
          rowCount: rows.length,
          fullyVisibleRows: fully.length,
          rowHeight: first ? Math.round(first.height) : null,
          listTop: list ? Math.round(list.top) : null,
          headHeight: head ? Math.round(head.height) : null,
          chromeAboveList: list ? Math.round(list.top) : null,
          blocks: ['.section-title', '.sports-bar', '.homepage-match-tabs', '.score-tabs', '.date-strip',
            '.filter-row', '#matches-notice', '#account-history-summary', '#manual-bet-panel', '.score-table-head']
            .map(selector => {
              const node = document.querySelector(selector);
              if (!node) return { selector, missing: true };
              const box = node.getBoundingClientRect(), style = getComputedStyle(node);
              return {
                selector, h: Math.round(box.height), top: Math.round(box.top),
                margin: `${style.marginTop}/${style.marginBottom}`,
                hidden: node.hidden || style.display === 'none',
              };
            }),
          // How much of the first screen one match row costs, recommendation included.
          recommendationHeight: (() => {
            const node = document.querySelector('#matches-list .match-recommendations');
            return node ? Math.round(node.getBoundingClientRect().height) : 0;
          })(),
        };
      });
      await context.close();
    }
  }
  await fs.writeFile(path.join(output, 'density.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  await browser.close();
  server.close();
}
