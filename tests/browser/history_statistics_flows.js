async page => {
  const assert = (condition, message) => {if (!condition) throw new Error(message);};
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let mode = 'cold',historyRequests=0;
  const fixture = {
    entries_total:1, query_ms:1, next_offset:null, overall:{graded:1,matches:1,accuracy:1,accuracy_samples:1,won:1,lost:0,roi:1},
    entries:[{at:'2026-10-12T08:00:00Z',home:'测试主队',away:'测试客队',label:'全场主胜',odds:2,status:'won',pnl:1,competition_type:'real'}],
    statistics_refreshing:false, statistics_stale:false, statistics_at:'2026-10-12T08:00:00+00:00'
  };
  await page.route('**/api/v1/**', async route => {
    if (!new URL(route.request().url()).pathname.endsWith('/ledger/history')) return route.fulfill({json:{matches:[],available:false}});
    historyRequests++;
    if (mode === 'error') return route.fulfill({status:500,json:{error:'fixture failure'}});
    const data = JSON.parse(JSON.stringify(fixture));
    if (mode === 'cold') Object.assign(data,{overall:{},statistics_refreshing:true,statistics_at:''});
    if (mode === 'loading') Object.assign(data,{entries:[],entries_status:'loading',entries_total:null,overall:null,settled:null,portfolio_summary:null,statistics_status:'loading',statistics_refreshing:true,statistics_at:''});
    if (mode === 'page-error') Object.assign(data,{entries:[],entries_status:'error',entries_error:'后台查询暂时失败',overall:null,statistics_status:'loading'});
    if (mode === 'page-ready') Object.assign(data,{entries_status:'ready',entries_at:'2026-10-12T08:05:00Z'});
    if (mode === 'unknown-total') Object.assign(data,{entries_total:null,next_offset:100,entries_status:'ready',entries_at:'2026-10-12T08:05:00Z',overall:null,statistics_status:'loading',statistics_refreshing:true,statistics_at:''});
    if (mode === 'stale') Object.assign(data,{statistics_refreshing:true,statistics_stale:true,legacy_identity_rows:2});
    if (mode === 'invalid-time') data.statistics_at = 'invalid';
    if (mode === 'empty') Object.assign(data,{entries:[],entries_total:0,overall:{pending:2},statistics_at:''});
    return route.fulfill({json:data});
  });
  const origin = new URL(page.url()).origin;
  await page.goto(origin + '/?test=history-statistics#history');
  await page.locator('#history-notice').filter({hasText:'正在后台汇总'}).waitFor();
  assert((await page.locator('#history-rows').textContent()).includes('测试主队'), 'cold statistics keep available history rows');
  assert(!(await page.locator('#history-notice').textContent()).includes('0 场未取得证据'), 'incomplete aggregate does not imply zero evidence');
  mode='loading';await page.locator('#history-refresh').click();
  await page.locator('#history-empty').filter({hasText:'记录页正在加载'}).waitFor();
  assert((await page.locator('#history-stats').textContent()).includes('— / —'),'cold null aggregates have no fake zero sample counts');
  assert(!(await page.locator('#history-count').textContent()).includes('0 条'),'cold entry total unknown');
  const loadingRequests=historyRequests;
  mode='page-ready';await page.locator('#history-rows').filter({hasText:'测试主队'}).waitFor();
  assert(historyRequests>loadingRequests,'loading state automatically polls bounded page cache');
  assert((await page.locator('#history-notice').textContent()).includes('记录页时点：'),'row snapshot time shown');
  mode='unknown-total';await page.locator('#history-refresh').click();
  await page.locator('#history-count').filter({hasText:'— 条'}).waitFor();
  assert((await page.locator('#history-rows').textContent()).includes('测试主队'),'unknown total does not hide loaded entries');
  assert(await page.locator('#history-next').isEnabled(),'sentinel enables next page before aggregate total exists');
  assert(!(await page.locator('#history-count').textContent()).includes('0 条'),'unknown total is not reported as zero');
  mode='page-error';await page.locator('#history-refresh').click();
  await page.locator('#history-notice.error').filter({hasText:'后台查询暂时失败'}).waitFor();
  assert(!(await page.locator('#history-rows').textContent()).includes('测试主队'),'different cold/error page does not preserve wrong previous rows');
  mode = 'stale';await page.locator('#history-refresh').click();
  await page.locator('#history-notice').filter({hasText:'上次统计缓存'}).waitFor();
  const stale = await page.locator('#history-notice').textContent();
  assert(stale.includes('正在后台汇总') && stale.includes('统计时点：') && stale.includes('旧记录 2 条'), 'stale notice combines refresh time and legacy warning');
  assert(!(await page.locator('#history-notice').getAttribute('class')).includes('error'), 'background statistics are not shown as a read failure');
  mode = 'ready';await page.locator('#history-refresh').click();
  await page.waitForFunction(()=>document.getElementById('history-notice').textContent.includes('统计时点：') && !document.getElementById('history-notice').textContent.includes('上次统计缓存'));
  const ready = await page.locator('#history-notice').textContent();
  assert(!ready.includes('正在后台汇总') && !ready.includes('上次统计缓存'), 'fresh statistics clear old state');
  mode = 'invalid-time';await page.locator('#history-refresh').click();
  await page.locator('#history-notice').filter({hasText:'统计时点暂不可用'}).waitFor();
  mode = 'empty';await page.locator('#history-refresh').click();
  await page.locator('#history-empty').waitFor();
  assert((await page.locator('#history-notice').textContent()).includes('2 场未取得证据'), 'empty history preserves settlement evidence notice');
  mode = 'error';await page.locator('#history-refresh').click();
  await page.locator('#history-notice.error').filter({hasText:'读取失败（500），可刷新重试。'}).waitFor();
  assert(await page.locator('#history-empty').isVisible(), 'read error preserves prior empty view');
  assert(await page.locator('#history-refresh').isEnabled(), 'read failure allows retry');
  assert(errors.length === 0, 'no uncaught browser errors: ' + errors.join('; '));
  return {checks:'cold/loading/automatic page polling/page error/stale/refreshing/timestamp/invalid timestamp/empty/error',errors};
}
