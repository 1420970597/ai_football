async (page) => {
  // Explicit test fixtures only; never exposed through the production API.
  const _PLACEHOLDER_MATCH = {
    match_id: 'test-real', league: '测试占位联赛', home: '测试主队', away: '测试客队', competition_type: 'real',
    score: [1, 0], clock: '60:00', stale: false, reasons: ['市场拟合模型尚未证明独立优势'],
    probabilities: {home: .7, draw: .2, away: .1}, compute_ms: 2, result_age_s: .1, quote_age_s: .1,
    markets: [{market: 'OU', line: '2.25', quotes: [{outcome: 'over', label: '全场大2.25', odds: 1.95, p_market: .5, trend_pct: .8},
      {outcome: 'under', label: '全场小2.25', odds: 1.96, p_market: .5, trend_pct: -.8}]}],
    remaining_goals: [.8, .4],
    candidates: [{label: '全场大2.25', ev: .02, odds: 1.95, p_model: .52}],
    price_history: {'OU|2.25|over': [[1000, 1.9], [2000, 1.95]], 'OU|2.25|under': [[1000, 2], [2000, 1.96]]}, events: []
  };
  const _PLACEHOLDER_SETTINGS = {version:1, persistent:true, algorithms:{poisson_market:'当前盘口 Poisson', poisson_time_decay:'时间衰减 Poisson'},
    settings:{algorithms:['poisson_market','poisson_time_decay'],primary_algorithm:'poisson_time_decay',devig_method:'proportional',
      min_probability:.52,min_ev:.02,quote_max_age_s:15,state_max_age_s:90,anchor_max_age_s:120,
      llm_enabled:false,llm_base_url:'http://localhost:9999/v1',llm_model:'test-model',has_llm_key:true,
      llm_timeout_s:20,llm_temperature:.2,llm_max_tokens:2048,llm_interval_s:60}};
  let mode = 'default';
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.route('**/api/v1/**', async route => {
    if (mode === 'error') return route.abort();
    if (mode === 'loading') await new Promise(resolve => setTimeout(resolve, 1500));
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/settings')) {
      if (route.request().method() === 'POST') {
        const body = route.request().postDataJSON();
        if (!body.settings.algorithms.length) return route.fulfill({status:400,json:{error:'至少启用一个有效算法'}});
        _PLACEHOLDER_SETTINGS.settings = {..._PLACEHOLDER_SETTINGS.settings, ...body.settings};
        delete _PLACEHOLDER_SETTINGS.settings.llm_api_key;
        _PLACEHOLDER_SETTINGS.version++;
      }
      return route.fulfill({json:_PLACEHOLDER_SETTINGS});
    }
    if (url.pathname.includes('/ledger/history')) return route.fulfill({json: {entries_total: 1, legacy_identity_rows: 0,
      overall: {graded: 1, pending: 0, hit_rate: 1, roi: .5, matches: 1}, entries: [{at: '2026-01-01T12:00:00Z',
        home: '测试主队', away: '测试客队', label: '全场小2.25', odds: 2, ft_score: [1, 1], status: 'half_won', pnl: .5, competition_type: 'real'}]}});
    if (url.pathname.startsWith('/api/v1/workbench/')) return route.fulfill({json: _PLACEHOLDER_MATCH});
    const type = url.searchParams.get('type');
    const match = {..._PLACEHOLDER_MATCH};
    if (mode === 'edge') match.home = '测试长队名'.repeat(14) + '<script>window.__bad=true</script>';
    if (type === 'virtual') {match.match_id = 'test-virtual'; match.league = 'EAFC 测试占位'; match.competition_type = 'virtual';}
    return route.fulfill({json: {count: mode === 'empty' ? 0 : 1, matches: mode === 'empty' ? [] : [match],
      coverage: {source_current: mode === 'empty' ? 0 : 3, subscribed: mode === 'empty' ? 0 : 2},
      realtime: {connected: 1}, performance: {event_to_result_ms: {p95: 150}}}});
  });
  function assert(condition, message) {if (!condition) throw new Error(message);}
  await page.setViewportSize({width: 1440, height: 1000});
  const baseURL = new URL(page.url()).origin;
  await page.goto(baseURL + '/');
  await page.locator('.match-card').waitFor();
  assert(await page.locator('header nav button').count() === 3, 'live/history/settings primary pages');
  assert(await page.locator('#source-match-count').textContent() === '3', 'source coverage is distinct from results');
  assert(await page.locator('#subscribed-count').textContent() === '2', 'typed subscription coverage');
  assert(await page.locator('.match-probabilities .probability-row').count() === 3, 'list includes probability bars');
  await page.screenshot({path: '/root/ai_football/output/playwright/task30-default.png', fullPage: true});
  await page.getByRole('button', {name: '分析测试主队对测试客队', exact: true}).click();
  await page.locator('#detail .probability-row').first().waitFor();
  await page.waitForTimeout(1200);
  assert(await page.locator('#detail svg').count() === 1, 'detail persists during polling');
  assert(await page.locator('#detail svg text').count() >= 5, 'trend has odds and time axes');
  assert(await page.locator('.intensity-grid strong').first().textContent() === '0.80', 'remaining goal intensity');
  assert(await page.locator('.ev-table tbody tr').count() === 1, 'numeric pricing table');
  await page.getByLabel('走势图盘口').selectOption('OU|2.25|under');
  await page.waitForTimeout(1200);
  assert(await page.getByLabel('走势图盘口').inputValue() === 'OU|2.25|under', 'selected trend preserved on polling');
  await page.getByRole('searchbox').fill('不存在的球队');
  assert(await page.getByText('没有符合筛选的赛事').isVisible(), 'search empty state');
  await page.getByRole('button', {name: '清除筛选', exact: true}).click();
  await page.getByRole('button', {name: '战绩', exact: true}).click();
  await page.getByText('赢半', {exact: true}).waitFor();
  assert(await page.getByText('+0.50', {exact: true}).isVisible(), 'half stake history');
  await page.getByRole('button', {name: '设置', exact: true}).click();
  await page.locator('#setting-min_ev').waitFor();
  assert(await page.locator('#setting-llm_api_key').inputValue() === '', 'credentials never prefilled');
  await page.locator('#setting-min_ev').fill('0.04');
  await page.getByRole('button', {name:'保存并立即生效',exact:true}).click();
  await page.getByText('v2 已生效', {exact:true}).waitFor();
  assert(await page.locator('#setting-min_ev').inputValue() === '0.04', 'hot saved value');
  for (const input of await page.locator('input[name="algorithm"]').all()) await input.uncheck();
  await page.getByRole('button', {name:'保存并立即生效',exact:true}).click();
  await page.getByText('至少启用一个有效算法', {exact:true}).waitFor();
  assert(await page.locator('#setting-min_ev').inputValue() === '0.04', 'invalid save preserves edits');
  await page.getByRole('button', {name:'重新读取',exact:true}).click();
  await page.locator('input[name="algorithm"]:checked').first().waitFor();
  await page.getByRole('button', {name: '滚球工作台', exact: true}).click();
  await page.getByRole('button', {name: '虚拟 / EAFC', exact: true}).click();
  await page.getByText('EAFC 测试占位', {exact: true}).first().waitFor();
  await page.getByRole('button', {name: '真实足球', exact: true}).click();
  await page.getByText('测试占位联赛', {exact: true}).first().waitFor();
  mode = 'loading';
  await page.reload({waitUntil: 'domcontentloaded'});
  assert(await page.locator('.skeleton').count() === 3, 'loading skeleton');
  await page.screenshot({path: '/root/ai_football/output/playwright/task30-loading.png'});
  await page.locator('.match-card').waitFor();
  mode = 'empty';
  await page.reload();
  await page.getByText('等待这一组赛事的实时行情').waitFor();
  await page.screenshot({path: '/root/ai_football/output/playwright/task30-empty.png'});
  mode = 'error';
  await page.reload();
  await page.getByText('暂时无法读取赛事').waitFor();
  assert(await page.locator('#notice.error').isVisible(), 'error banner');
  await page.screenshot({path: '/root/ai_football/output/playwright/task30-error.png'});
  mode = 'edge';
  await page.setViewportSize({width: 390, height: 844});
  await page.reload();
  await page.locator('.match-card').waitFor();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'mobile page has no horizontal overflow');
  assert(await page.evaluate(() => !window.__bad), 'names rendered as text');
  await page.screenshot({path: '/root/ai_football/output/playwright/task30-mobile-edge.png', fullPage: true});
  await page.locator('.match-title').click();
  await page.locator('#detail .probability-row').first().waitFor();
  await page.keyboard.press('Escape');
  assert(!await page.locator('#detail').isVisible(), 'mobile back to matches');
  assert(errors.length === 0, 'no uncaught browser errors: ' + errors.join(', '));
  console.log('PASS: Default, Loading, Empty, Error, Edge-Case; search, detail, history, hot settings, rejected save, type separation, mobile, safe DOM. Fixtures only.');
}
