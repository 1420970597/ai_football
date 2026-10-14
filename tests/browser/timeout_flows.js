async function (page) {
  const failures = [];
  const writes = [];
  page.on('pageerror', error => failures.push(error.message));
  let recommendationReads = 0;
  await page.route('**/api/v1/**', async route => {
    if (route.request().method() !== 'GET') {writes.push(route.request().method()); return route.abort();}
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/account')) return route.fulfill({json:{available:true, sports_balance:123.45, today_pnl:{available:true,amount:2,date:'2026-10-11',settled_count:1},unsettled:{count:0},settled:{count:1}}});
    if (path.endsWith('/recommendations')) {
      recommendationReads++;
      return route.fulfill({json:{open:[{match_id:'fixture',home:'超时测试主队',away:'客队',market_open:true,pick:{label:'全场大2.5',odds:1.9,confidence:.6}}],closed:[],summary:{open:1,closed:0},closed_page:{offset:0,limit:100,returned:0,total_available:0,next_offset:null,prev_offset:null,summary_total:0,window_truncated:false,scope:'recent_recommendations'}}});
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
    const timeoutOriginal = AbortSignal.timeout;
    let bodyAbort;
    let ordinaryAbort;
    // Native AbortController signals carry the deadline reason even when body
    // consumption rejects with AbortError. HTTP streaming is tested separately.
    const controller = new AbortController();
    AbortSignal.timeout = () => controller.signal;
    window.fetch = async (_url, options) => ({ok:true,json:() => new Promise((_resolve,reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('The user aborted a request.','AbortError')), {once:true});
      queueMicrotask(() => controller.abort(new DOMException('The operation timed out.','TimeoutError')));
    })});
    try {await get('/fixture');} catch (error) {
      bodyAbort = {name:error.name,message:error.message,stage:error.readStage,kind:error.readFailureKind,reason:error.readAbortReason};
    } finally {AbortSignal.timeout = timeoutOriginal; window.fetch = fetchOriginal;}
    window.fetch = async () => ({ok:true,json:async () => {throw new DOMException('unrelated abort','AbortError');}});
    try {await get('/fixture');} catch (error) {ordinaryAbort = {name:error.name,kind:error.readFailureKind};}
    finally {window.fetch = fetchOriginal;}
    return {timeoutName,timeoutMessage,bodyTimeout,bodyAbort,ordinaryAbort,retained,retryDelay:retryAt-Date.now(),account:document.getElementById('account-summary').textContent};
  });
  if (evidence.timeoutName !== 'TimeoutError' || !evidence.timeoutMessage.includes('读取超时') || evidence.bodyTimeout !== evidence.timeoutMessage) throw new Error('fetch/body timeout not normalized');
  if (evidence.bodyAbort.name !== 'TimeoutError' || evidence.bodyAbort.message !== evidence.timeoutMessage || evidence.bodyAbort.stage !== 'body' || evidence.bodyAbort.kind !== 'timeout' || evidence.bodyAbort.reason !== 'TimeoutError') throw new Error('AbortError body lost native timeout signal metadata');
  if (evidence.ordinaryAbort.kind === 'timeout') throw new Error('unrelated AbortError mislabeled deadline timeout');
  if (evidence.retained !== 1 || evidence.retryDelay < 20000 || recommendationReads !== before) throw new Error('old result/backoff lost');
  if (!evidence.account.includes('123.45')) throw new Error('account data lost after timeout');
  await page.getByText('请求读取超时，保留上次结果，30秒后自动重试，也可点击刷新。',{exact:true}).waitFor();
  await page.locator('#recommendations-refresh').click();
  await page.locator('.buy-card').waitFor();
  if (recommendationReads !== before+1) throw new Error('manual refresh cannot recover');
  if (failures.length || writes.length) throw new Error([...failures,...writes].join('; '));
  console.log('PASS: fetch/body timeout normalization, 30s backoff, retained account and recommendation, manual recovery. Fixtures only.');
}
