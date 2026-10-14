async function (page) {
  const failures = [];
  const writes = [];
  const reads = [];
  let total = 250;
  let mode = 'normal';
  let holdOffset = null;
  let pending = null;
  const assert = (condition, message) => {if (!condition) throw new Error(message);};
  const waitForPending = async () => {
    for (let attempt = 0; pending === null && attempt < 100; attempt++) await page.waitForTimeout(20);
    assert(pending !== null, 'fixture pending gate missing');
  };
  const responseFor = requested => {
    const offset = total ? Math.min(requested, Math.floor((total - 1) / 100) * 100) : 0;
    const closed = Array.from({length:Math.min(100, total - offset)}, (_, index) => ({
      at:'2026-10-14T10:00:00Z', match_id:'closed-' + (offset + index), home:'回放' + String(offset + index).padStart(3, '0'),
      away:'测试客队', label:'全场大2.5', odds:1.9, status:'won', pnl:.9
    }));
    return {open:Array.from({length:3}, (_, index) => ({match_id:'open-' + index, home:'开启主队' + index, away:'测试客队',
      market_open:true, pick:{label:'全场大2.5', odds:1.9, p_model:.6, confidence:.7, decision_count:3,
        settled_hit_count:2, hit_count:8, miss_count:1, pending_decisions:4, composite_confidence:.75}})),
      closed, summary:{won:400,lost:100,pending:200}, source_age_s:1,
      closed_page:{offset,limit:100,returned:closed.length,total_available:total,prev_offset:offset ? offset - 100 : null,
        next_offset:offset + 100 < total ? offset + 100 : null,summary_total:700,window_truncated:true,scope:'recent_recommendations'}};
  };
  page.on('pageerror', error => failures.push(error.message));
  await page.route('**/api/v1/**', async route => {
    if (route.request().method() !== 'GET') {
      writes.push(route.request().method());
      return route.abort();
    }
    const url = new URL(route.request().url());
    if (url.pathname.endsWith('/recommendations')) {
      assert(url.searchParams.get('compact') === '1', 'recommendations must request compact projection');
      assert(url.searchParams.get('closed_limit') === '100', 'recommendations must request 100 rows');
      const offset = Number(url.searchParams.get('closed_offset'));
      assert(Number.isSafeInteger(offset) && offset >= 0, 'invalid requested offset');
      reads.push(offset);
      if (offset === holdOffset) {
        holdOffset = null;
        await new Promise(resolve => {pending = resolve;});
      }
      if (mode === '503') return route.fulfill({status:503,contentType:'text/plain',body:'fixture backend unavailable'});
      if (mode === 'parse') return route.fulfill({contentType:'application/json',body:'{broken json'});
      if (mode === 'shape') return route.fulfill({json:{open:[],closed:[],summary:{}}});
      return route.fulfill({json:responseFor(offset)});
    }
    if (url.pathname.endsWith('/account')) return route.fulfill({json:{available:false,error:'fixture account unavailable'}});
    return route.fulfill({json:{matches:[],coverage:{},realtime:{connected:1}}});
  });
  await page.reload();
  await page.getByRole('button',{name:'推荐买入',exact:true}).click();
  await page.waitForFunction(() => !S.recommendationsBusy && document.querySelectorAll('#closed-recommendations tr').length === 100);
  await page.evaluate(() => {clearTimeout(S.timer); S.timer = null;});
  const snapshot = () => page.evaluate(() => ({
    open:document.getElementById('open-recommendations').innerHTML,
    closed:document.getElementById('closed-recommendations').innerHTML,
    stats:document.getElementById('recommendations-stats').innerHTML,
    page:document.getElementById('closed-recommendations-page').textContent,
    empty:document.getElementById('closed-recommendations-empty').hidden
  }));
  const assertRetained = async (before, label) => assert(JSON.stringify(await snapshot()) === JSON.stringify(before), label + ' erased previous results');
  const state = () => page.evaluate(() => ({
    offset:S.recommendationsOffset,desired:S.recommendationsDesiredOffset,retry:(S.recommendationsNextAt || 0) - Date.now(),
    message:document.getElementById('recommendations-notice').textContent
  }));
  const recover = async () => {
    mode = 'normal';
    await page.locator('#recommendations-refresh').click();
    await page.waitForFunction(() => !S.recommendationsBusy && !document.getElementById('recommendations-notice').classList.contains('error'));
  };
  assert(await page.locator('#open-recommendations .buy-card').count() === 3, 'open recommendations were truncated');
  assert((await page.locator('#open-recommendations').textContent()).includes('决策次数 3.0 · 历史命中 2.0 · 历史未中 1.0 · 待结算 4.0'), 'evidence counts changed');
  const scope = await page.locator('#closed-recommendations-scope').textContent();
  assert(scope.includes('1000') && scope.includes('700') && scope.includes('非全部历史'), 'window scope missing');
  assert(await page.locator('.recommendations-history-link').getAttribute('href') === '#history', 'full history entry missing');
  assert(await page.locator('#closed-recommendations-prev').isDisabled(), 'first-page previous must be disabled');
  await page.locator('#closed-recommendations-next').click();
  await page.waitForFunction(() => !S.recommendationsBusy && S.recommendationsOffset === 100);
  assert((await page.locator('#closed-recommendations tr').first().textContent()).includes('回放100'), 'second-page boundary wrong');
  await page.evaluate(() => loadRecommendations());
  assert((await state()).offset === 100 && reads.at(-1) === 100, 'poll reset selected page');
  await page.locator('#closed-recommendations-next').click();
  await page.waitForFunction(() => !S.recommendationsBusy && S.recommendationsOffset === 200);
  assert(await page.locator('#closed-recommendations tr').count() === 50, 'last-page count wrong');
  assert(await page.locator('#closed-recommendations-next').isDisabled(), 'last-page next must be disabled');
  total = 25;
  await recover();
  assert((await state()).offset === 0 && await page.locator('#closed-recommendations tr').count() === 25, 'shrinking cache did not clamp offset');
  total = 0;
  await recover();
  assert(await page.locator('#closed-recommendations-empty').isVisible(), 'empty response not shown');
  assert(await page.locator('#closed-recommendations-prev').isDisabled() && await page.locator('#closed-recommendations-next').isDisabled(), 'empty page permits paging');
  total = 250;
  await recover();

  // A requested page must not overwrite a newer pending navigation intent.
  await page.evaluate(() => {
    window.fixtureClosedMutations = [];
    window.fixtureClosedObserver = new MutationObserver(() => window.fixtureClosedMutations.push(document.getElementById('closed-recommendations').textContent));
    window.fixtureClosedObserver.observe(document.getElementById('closed-recommendations'), {childList:true});
  });
  holdOffset = 100;
  pending = null;
  await page.locator('#closed-recommendations-next').click();
  await page.waitForFunction(() => S.recommendationsBusy && S.recommendationsActiveOffset === 100);
  await waitForPending();
  await page.locator('#closed-recommendations-prev').click();
  assert((await state()).desired === 0, 'busy navigation intent discarded');
  pending();
  await page.waitForFunction(() => !S.recommendationsBusy && S.recommendationsOffset === 0);
  assert(reads.at(-1) === 0, 'pending navigation did not trigger follow-up read');
  assert(!await page.evaluate(() => window.fixtureClosedMutations.some(text => text.includes('回放100'))), 'stale page briefly replaced current page');
  await page.evaluate(() => window.fixtureClosedObserver.disconnect());

  // Responses from a hidden view remain unable to replace the retained page.
  holdOffset = 100;
  pending = null;
  const visibleBeforeHide = await snapshot();
  await page.locator('#closed-recommendations-next').click();
  await page.waitForFunction(() => S.recommendationsBusy && S.recommendationsActiveOffset === 100);
  await waitForPending();
  await page.getByRole('button',{name:'滚球工作台',exact:true}).click();
  pending();
  await page.waitForFunction(() => !S.recommendationsBusy);
  await assertRetained(visibleBeforeHide, 'hidden-view response');
  await page.getByRole('button',{name:'推荐买入',exact:true}).click();
  await page.waitForFunction(() => !S.recommendationsBusy && S.recommendationsOffset === 100);

  const previous = await snapshot();
  mode = '503';
  await page.evaluate(() => loadRecommendations(true));
  let observed = await state();
  assert(observed.message.startsWith('后端服务暂不可用（503）') && observed.retry > 20000, 'HTTP 503 classification/backoff wrong');
  await assertRetained(previous, '503');
  const readsBeforeBackoff = reads.length;
  await page.evaluate(() => loadRecommendations());
  assert(reads.length === readsBeforeBackoff, 'automatic retry ignored 30-second backoff');
  await recover();

  for (const kind of ['fetch-timeout','body-timeout','fetch-network','body-network']) {
    const errorMeta = await page.evaluate(async failure => {
      const original = window.fetch;
      const fail = () => {throw failure.endsWith('timeout') ? new DOMException('fixture timeout','TimeoutError') : new TypeError('fixture disconnected');};
      window.fetch = failure.startsWith('body') ? async () => ({ok:true,json:async () => fail()}) : async () => fail();
      try {
        let metadata;
        try {await get('/fixture');} catch (error) {metadata = {stage:error.readStage,kind:error.readFailureKind,name:error.name};}
        await loadRecommendations(true);
        return metadata;
      } finally {window.fetch = original;}
    }, kind);
    observed = await state();
    assert(errorMeta.stage === (kind.startsWith('body') ? 'body' : 'fetch'), kind + ' read-stage metadata wrong');
    assert(errorMeta.kind === (kind.endsWith('timeout') ? 'timeout' : 'network'), kind + ' read-kind metadata wrong');
    assert(observed.message.startsWith(kind.endsWith('timeout') ? '请求读取超时' : '连接失败') && !observed.message.includes('后端服务暂不可用') && observed.retry > 20000, kind + ' message/backoff wrong');
    await assertRetained(previous, kind);
    await recover();
  }
  mode = 'parse';
  await page.evaluate(() => loadRecommendations(true));
  observed = await state();
  assert(observed.message.startsWith('响应数据格式异常') && observed.retry <= 0, 'JSON parse error treated as backend outage');
  await assertRetained(previous, 'JSON parse failure');
  await recover();
  mode = 'shape';
  await page.evaluate(() => loadRecommendations(true));
  observed = await state();
  assert(observed.message.startsWith('推荐响应结构异常') && observed.retry <= 0, 'response shape error treated as network');
  await assertRetained(previous, 'invalid response shape');
  await recover();
  await page.evaluate(async () => {
    const original = renderRecommendationCard;
    let cards = 0;
    renderRecommendationCard = item => {if (++cards === 2) throw new TypeError('fixture renderer TypeError'); return original(item);};
    try {await loadRecommendations(true);} finally {renderRecommendationCard = original;}
  });
  observed = await state();
  assert(observed.message.startsWith('页面展示异常') && observed.retry <= 0, 'renderer TypeError treated as network');
  await assertRetained(previous, 'render failure after preparing first card');
  await recover();
  assert(writes.length === 0 && failures.length === 0, 'non-GET writes or browser page errors: ' + [...writes,...failures].join('; '));
  console.log('PASS: compact 100-row paging, all open/count values, honest scope, end/empty/shrink, polling page preservation, busy/hidden-view races, distinct HTTP/fetch/body/JSON/render failures, old results and manual recovery; no non-GET writes.');
}
