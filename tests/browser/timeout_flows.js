async function (page) {
  const failures = [];
  page.on('pageerror', error => failures.push(error.message));
  let recommendationReads = 0;
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/account')) return route.fulfill({json:{available:true, sports_balance:123.45, today_pnl:{available:true,amount:2,date:'2026-10-11',settled_count:1},unsettled:{count:0},settled:{count:1}}});
    if (path.endsWith('/recommendations')) {
      recommendationReads++;
      return route.fulfill({json:{open:[{match_id:'fixture',home:'超时测试主队',away:'客队',market_open:true,pick:{label:'全场大2.5',odds:1.9,confidence:.6}}],closed:[],summary:{open:1,closed:0}}});
    }
    return route.fulfill({json:{matches:[],coverage:{},realtime:{connected:1}}});
  });
  await page.reload();
  await page.locator('#account-summary .account-balance').waitFor();
  await page.getByRole('button',{name:'推荐买入',exact:true}).click();
  await page.locator('.buy-card').waitFor();
  const before = recommendationReads;
  const evidence = await page.evaluate(async () => {
    const fetchOriginal = window.fetch;
    window.fetch = async () => {throw new DOMException('signal timed out','TimeoutError');};
    let timeoutName, timeoutMessage;
    try {await get('/fixture');} catch (error) {timeoutName=error.name; timeoutMessage=error.message;}
    S.accountAt = 0;
    await loadAccount();
    await loadRecommendations(true);
    const retryAt = S.recommendationsNextAt;
    window.fetch = fetchOriginal;
    await loadRecommendations();
    const retained = document.querySelectorAll('.buy-card').length;
    let bodyTimeout;
    window.fetch = async () => ({ok:true,json:async()=>{throw new DOMException('signal timed out','TimeoutError');}});
    try {await get('/fixture');} catch(error) {bodyTimeout=error.message;}
    window.fetch = fetchOriginal;
    return {timeoutName,timeoutMessage,bodyTimeout,retained,retryDelay:retryAt-Date.now(),account:document.getElementById('account-summary').textContent};
  });
  if (evidence.timeoutName !== 'TimeoutError' || !evidence.timeoutMessage.includes('读取超时') || evidence.bodyTimeout !== evidence.timeoutMessage) throw new Error('fetch/body timeout not normalized');
  if (evidence.retained !== 1 || evidence.retryDelay < 20000 || recommendationReads !== before) throw new Error('old result/backoff lost');
  if (!evidence.account.includes('123.45')) throw new Error('account data lost after timeout');
  await page.getByText('后端服务暂不可用，保留上次结果，30秒后自动重试，也可点击刷新。',{exact:true}).waitFor();
  await page.locator('#recommendations-refresh').click();
  await page.locator('.buy-card').waitFor();
  if (recommendationReads !== before+1) throw new Error('manual refresh cannot recover');
  if (failures.length) throw new Error(failures.join('; '));
  console.log('PASS: fetch/body timeout normalization, 30s backoff, retained account and recommendation, manual recovery. Fixtures only.');
}
