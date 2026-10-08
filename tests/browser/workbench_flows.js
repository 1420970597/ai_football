async (page) => {
  // Explicit test fixtures only; never exposed through the production API.
  const _PLACEHOLDER_MATCH = {
    match_id: 'test-real', league: '测试占位联赛', home: '测试主队', away: '测试客队', competition_type: 'real',
    score: [1, 0], clock: '60:00', stale: false, reasons: ['市场拟合模型尚未证明独立优势'],
    probabilities: {home: .7, draw: .2, away: .1}, compute_ms: 2, result_age_s: .1, quote_age_s: .1,
    markets: [{market: 'OU', line: '2.25', quotes: [{outcome: 'over', label: '全场大2.25', odds: 1.95, p_market: .5, trend_pct: .8},
      {outcome: 'under', label: '全场小2.25', odds: 1.96, p_market: .5, trend_pct: -.8}]}],
    candidates: [{label: '全场大2.25', ev: .02}], price_history: {'OU|2.25|over': [[1000, 1.9], [2000, 1.95]]}, events: []
  };
  let mode = 'default';
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/v1/**', async route => {
    if (mode === 'error') return route.abort();
    if (mode === 'loading') await new Promise(resolve => setTimeout(resolve, 1500));
    const url = new URL(route.request().url());
    if (url.pathname.includes('/ledger/history')) return route.fulfill({json: {entries_total: 1, legacy_identity_rows: 0,
      overall: {graded: 1, pending: 0, hit_rate: 1, roi: .5, matches: 1}, entries: [{at: '2026-01-01T12:00:00Z',
        home: '测试主队', away: '测试客队', label: '全场小2.25', odds: 2, ft_score: [1, 1], status: 'half_won', pnl: .5, competition_type: 'real'}]}});
    if (url.pathname.startsWith('/api/v1/workbench/')) return route.fulfill({json: _PLACEHOLDER_MATCH});
    const type = url.searchParams.get('type');
    const match = {..._PLACEHOLDER_MATCH};
    if (mode === 'edge') match.home = '测试长队名'.repeat(14) + '<script>window.__bad=true</script>';
    if (type === 'virtual') {match.match_id = 'test-virtual'; match.league = 'EAFC 测试占位'; match.competition_type = 'virtual';}
    return route.fulfill({json: {count: mode === 'empty' ? 0 : 1, matches: mode === 'empty' ? [] : [match],
      realtime: {connected: 1}, performance: {event_to_result_ms: {p95: 150}}}});
  });
  function assert(condition, message) {if (!condition) throw new Error(message);}
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto('http://127.0.0.1:18025/');
  await page.locator('.match-card').waitFor();
  assert(await page.locator('header nav button').count() === 2, 'only two primary pages');
  await page.screenshot({path: '/root/ai_football/output/playwright/task25-default.png', fullPage: true});
  await page.getByRole('button', {name: '分析测试主队对测试客队', exact: true}).click();
  await page.locator('#detail .probability-row').first().waitFor();
  await page.waitForTimeout(1200);
  assert(await page.locator('#detail svg').count() === 1, 'detail persists during polling');
  await page.getByRole('searchbox').fill('不存在的球队');
  assert(await page.getByText('没有符合筛选的赛事').isVisible(), 'search empty state');
  await page.getByRole('button', {name: '清除筛选', exact: true}).click();
  await page.getByRole('button', {name: '战绩', exact: true}).click();
  await page.getByText('赢半', {exact: true}).waitFor();
  assert(await page.getByText('+0.50', {exact: true}).isVisible(), 'half stake history');
  await page.getByRole('button', {name: '滚球工作台', exact: true}).click();
  await page.getByRole('button', {name: '虚拟 / EAFC', exact: true}).click();
  await page.getByText('EAFC 测试占位', {exact: true}).first().waitFor();
  await page.getByRole('button', {name: '真实足球', exact: true}).click();
  await page.getByText('测试占位联赛', {exact: true}).first().waitFor();
  mode = 'loading';
  await page.reload({waitUntil: 'domcontentloaded'});
  assert(await page.locator('.skeleton').count() === 3, 'loading skeleton');
  await page.screenshot({path: '/root/ai_football/output/playwright/task25-loading.png'});
  await page.locator('.match-card').waitFor();
  mode = 'empty';
  await page.reload();
  await page.getByText('等待这一组赛事的实时行情').waitFor();
  await page.screenshot({path: '/root/ai_football/output/playwright/task25-empty.png'});
  mode = 'error';
  await page.reload();
  await page.getByText('暂时无法读取赛事').waitFor();
  assert(await page.locator('#notice.error').isVisible(), 'error banner');
  await page.screenshot({path: '/root/ai_football/output/playwright/task25-error.png'});
  mode = 'edge';
  await page.setViewportSize({width: 390, height: 844});
  await page.reload();
  await page.locator('.match-card').waitFor();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile page has no horizontal overflow');
  assert(await page.evaluate(() => !window.__bad), 'names rendered as text');
  await page.screenshot({path: '/root/ai_football/output/playwright/task25-mobile-edge.png', fullPage: true});
  await page.locator('.match-title').click();
  await page.locator('#detail .probability-row').first().waitFor();
  await page.keyboard.press('Escape');
  assert(!await page.locator('#detail').isVisible(), 'mobile back to matches');
  assert(errors.length === 0, 'no uncaught browser errors: ' + errors.join(', '));
  console.log('PASS: Default, Loading, Empty, Error, Edge-Case; search, detail, history, type separation, mobile, safe DOM. Fixtures only.');
}
