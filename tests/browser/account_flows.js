async (page) => {
  // Read-only fixtures: no account credentials or real order submissions.
  const _PLACEHOLDER_ACCOUNT = {
    available: true, sports_balance: 111.86, center_balance: 0,
    unsettled: {count: 1, amount: 2, items: []},
    settled: {count: 3, amount: 6, items: []},
    today_pnl: {available: true, amount: 1.8, settled_count: 3, date: '2026-10-10',
      timezone: 'Asia/Shanghai', basis: 'settlement_time', reason: null}
  };
  let account = _PLACEHOLDER_ACCOUNT;
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  function assert(condition, message) {if (!condition) throw new Error(message);}
  await page.route('**/api/v1/**', async route => {
    assert(route.request().method() === 'GET', 'account display must not make write requests');
    if (new URL(route.request().url()).pathname.endsWith('/account')) return route.fulfill({json:account});
    return route.fulfill({json: {count:0, matches:[], coverage:{}, realtime:{}, performance:{}}});
  });
  const baseURL = new URL(page.url()).origin;
  await page.setViewportSize({width:1440, height:1000});
  await page.goto(baseURL + '/');
  const header = page.locator('#account-summary');
  const headerPnl = header.locator('.account-pnl');
  await headerPnl.getByText('今日盈亏 +1.80', {exact:true}).waitFor();
  assert(await headerPnl.evaluate(node => node.classList.contains('positive')), 'positive profit style');
  await header.click();
  await page.locator('#account-popover .account-pnl').waitFor();
  assert(await page.locator('#account-details .account-pnl').textContent() === '今日盈亏 +1.80', 'popover uses the same metric');
  const note = await page.locator('.account-pnl-note').textContent();
  assert(note.includes('北京时间') && note.includes('按结算时间统计') && note.includes('不含未结算订单'), 'settlement basis visible');
  assert(note.includes('2026-10-10') && note.includes('3 笔'), 'date and settled count visible');
  await page.screenshot({path:'/root/ai_football/output/playwright/account-pnl-desktop.png'});
  await page.locator('#account-close').click();

  for (const [amount, text, style] of [[-2.5, '-2.50', 'negative'], [0, '0.00', '']]) {
    account = {..._PLACEHOLDER_ACCOUNT, today_pnl:{..._PLACEHOLDER_ACCOUNT.today_pnl, amount}};
    await page.reload();
    await headerPnl.getByText('今日盈亏 ' + text, {exact:true}).waitFor();
    assert(await headerPnl.evaluate((node, style) => style ? node.classList.contains(style) :
      !node.classList.contains('positive') && !node.classList.contains('negative'), style), 'loss/zero style');
  }
  account = {..._PLACEHOLDER_ACCOUNT, today_pnl:{..._PLACEHOLDER_ACCOUNT.today_pnl,
    available:false, amount:null, reason:'结算记录未完整读取，暂不能统计今日盈亏'}};
  await page.reload();
  await headerPnl.getByText('今日盈亏 —', {exact:true}).waitFor();
  await header.click();
  assert(await page.locator('.account-pnl-note').textContent().then(t => t.includes('结算记录未完整读取')), 'incomplete reason visible');
  // An older backend without this field also shows unavailable, never zero.
  account = {..._PLACEHOLDER_ACCOUNT};
  delete account.today_pnl;
  await page.reload();
  await headerPnl.getByText('今日盈亏 —', {exact:true}).waitFor();
  account = {available:false, error:'测试账户接口不可用'};
  await page.reload();
  await header.getByText('账户数据不可用', {exact:true}).waitFor();
  await header.click();
  assert(await page.locator('.account-error').textContent() === '测试账户接口不可用', 'account failure reason visible');

  account = _PLACEHOLDER_ACCOUNT;
  for (const width of [1440, 1280, 1100, 950, 768, 651, 650, 390, 320]) {
    await page.setViewportSize({width, height:844});
    await page.reload();
    await headerPnl.getByText('今日盈亏 +1.80', {exact:true}).waitFor();
    assert(await headerPnl.isVisible(), 'today profit visible at ' + width);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'no page overflow at ' + width);
    assert(await headerPnl.evaluate(node => {
      const box = node.getBoundingClientRect();
      const parent = node.parentElement.getBoundingClientRect();
      return box.x >= 0 && box.right <= innerWidth && box.y >= parent.y && box.bottom <= parent.bottom;
    }), 'today profit is not clipped at ' + width);
    if (width === 390) {
      await header.click();
      await page.screenshot({path:'/root/ai_football/output/playwright/account-pnl-mobile.png'});
    }
  }
  // Large balances must not push the mobile header outside the viewport.
  account = {..._PLACEHOLDER_ACCOUNT, sports_balance:123456789.12,
    today_pnl:{..._PLACEHOLDER_ACCOUNT.today_pnl, amount:-123456789.12}};
  await page.reload();
  await headerPnl.getByText('今日盈亏 -123456789.12', {exact:true}).waitFor();
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'large amounts have no mobile overflow');
  assert(errors.length === 0, 'no uncaught JavaScript errors: ' + errors.join(', '));
  console.log('PASS: account daily P/L positive, negative, zero, unavailable, legacy, failure, settlement basis; 9 widths and large amounts. Fixtures only.');
}
