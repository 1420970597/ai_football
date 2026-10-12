async (page) => {
  const assert = (value, message) => {if (!value) throw new Error(message);};
  const errors = [], writes = [];
  page.on('pageerror', error => errors.push(error.message));
  let mode = 'candidate';
  const candidate = {version:'candidate-v5',updated_at:'2026-10-12T08:00:00Z',parameter_count:39,architecture:'L2 Logistic',promotion:{approved:false,reasons:['尚未证明优于市场']},validation:{matches:120,decisions:4000,accuracy:.61,had_top1_accuracy:.48,brier:.23},baseline:{matches:120,decisions:4000,accuracy:.62,had_top1_accuracy:.49,brier:.22}};
  await page.route('**/api/v1/**', async route => {
    if (route.request().method() !== 'GET') writes.push(route.request().url());
    if (!route.request().url().includes('/data-model')) return route.fulfill({json:{matches:[],available:false}});
    const data = {model:{},training:{state:'candidate',candidate,eligible_decisions:null,data_quality:{sampled:true,counts_scope:'sampled_qualified_inputs',sample_valid_training_decisions:123,input_records:null,input_matches:null,invalid_training_records:null}},
      decision_mode:'algorithm',settings:{decision_mode:'algorithm',shadow_enabled:true,cycle_matches:100},running:true,
      shadow:{available:true,artifact_kind:'candidate',approved:false,version:'candidate-v5',last_prediction_at:'2026-10-12T08:15:00Z'},
      prospective:{version:'candidate-v5',status:'waiting_for_settlement',reason:'已生成预测，等待终场。',updated_at:'2026-10-12T08:16:00Z',counts:{predictions:300,settled_predictions:0,pending_predictions:300,settled_matches:0}},
      storage:{summary_status:'missing',summary_reason:'后台尚未发布统计',archive:{running:true,phase:'uploading'},disk:{free:1073741824}}};
    if (mode === 'ready') {
      Object.assign(data.prospective,{status:'ready',reason:'当前版本前瞻样本',counts:{predictions:600,settled_predictions:400,pending_predictions:200,settled_matches:20,ungraded_predictions:3,excluded_predictions:10},model:{matches:20,decisions:400,accuracy:.63,had_top1_accuracy:.55,had_top1_matches:12,asian_payment_accuracy:.575,asian_selected_contracts:80,brier:.22},baseline:{matches:20,decisions:400,accuracy:.60,had_top1_accuracy:.50,asian_payment_accuracy:.525,brier:.23},window:{from:'2026-10-12T08:00:00Z',to:'2026-10-12T09:00:00Z',max_predictions:10000},latency:{p95_ms:572005}});
      data.shadow.live={outbox_batches:3,outbox_predictions:300,outbox_oldest_wait_ms:1000,persist_state:'pending'};
      data.shadow.performance={scope:'live_lane_recent_process_samples',sample_limit:512,samples:100,updated_at:'2026-10-12T09:00:00Z',compute_ms:{p95:4},queue_ms:{p95:121},decision_to_prediction_ms:{p95:125},prediction_persist_ms:{p95:90},decision_to_durable_ms:{p95:130},decision_to_pg_confirmed_ms:{p95:220}};
    }
    if (mode === 'no-sample') Object.assign(data.prospective,{status:'ready',model:{accuracy:0,had_top1_accuracy:null,asian_payment_accuracy:null},baseline:{accuracy:0,had_top1_accuracy:null,asian_payment_accuracy:null}});
    if (mode === 'unapproved') Object.assign(data,{decision_mode:'model',model:{version:'wrongly-loaded',promotion:{}},decision_model_available:false});
    if (mode === 'stale') Object.assign(data.storage,{at:'invalid',summary_at:'2026-01-01T00:00:00Z',summary_status:'stale',summary_reason:'后台统计已过期',records:{decisions:99,matches:3,settled_decisions:70},categories:[{name:'走势',files:null,bytes:null,allocated_bytes:null}],archive:{backend:'s3',files:2461111,bytes:1073741824,object_bytes:536870912}});
    return route.fulfill({json:data});
  });
  const origin = new URL(page.url()).origin;
  await page.goto(origin + '/#data-model');
  await page.getByText('候选已训练 · 未晋级',{exact:true}).waitFor();
  let cards = await page.locator('#model-summary').textContent();
  assert(cards.includes('训练参数量39') && cards.includes('生产模型版本尚无已晋级模型'),'candidate metadata separated from production');
  assert(cards.includes('全量有效训练决策尚未审查') && cards.includes('全量无效 / 隔离数据尚未审查'),'null full audit remains explicit');
  let shadow = await page.locator('.shadow-performance').textContent();
  assert(shadow.includes('候选影子模型（未晋级）') && shadow.includes('前瞻待结样本300'),'candidate shadow and pending count visible');
  assert(!shadow.includes('0.0%'),'pending has no fake zero accuracy');
  assert(shadow.includes('影子计算 P95尚未统计'),'missing live latency is not fake zero');
  assert((await page.locator('#model-performance').textContent()).includes('61.0%'),'candidate offline performance actually rendered');
  assert((await page.locator('#storage-summary').textContent()).includes('S3 归档文件尚未统计'),'incomplete archive status does not falsely disable S3');
  assert((await page.locator('#storage-categories').textContent()).includes('分类统计尚未生成'),'missing category table has explicit empty state');
  mode='ready';await page.locator('#data-model-refresh').click();
  await page.getByText('55.0%',{exact:true}).first().waitFor();
  shadow=await page.locator('.shadow-performance').textContent();
  for (const text of ['胜平负 Top-1 正确率55.0%','胜平负市场 Top-150.0%','亚洲盘选中方向获利率57.5%','亚洲盘市场方向获利率52.5%','前瞻已结样本400','前瞻待结样本200','125 ms','当前版本最近 10000 条预测']) assert(shadow.includes(text),'shadow metric '+text);
  for (const text of ['影子计算 P954 ms','实时排队 P95121 ms','决策至影子输出 P95125 ms','影子持久化 P9590 ms','决策至本地可靠保存 P95130 ms','决策至 PG 确认 P95220 ms','待同步 PG 批次3','待同步 PG 预测300','已存历史预测端到端 P95572005 ms','实时延迟样本100','最近 512 个有效样本','重启恢复记录不参与计算、排队和输出延迟统计']) assert(shadow.includes(text),'latency scope '+text);
  await page.route('**/api/v1/data-model', async route => route.fulfill({json:{model:{},training:{state:'candidate',candidate},settings:{shadow_enabled:true},shadow:{available:true,artifact_kind:'candidate',version:'candidate-v5'},running:true,
    prospective:{version:'candidate-v5',status:'ready',model:{matches:20,decisions:400,had_top1_accuracy:.55},baseline:{matches:20,decisions:400,had_top1_accuracy:.5},counts:{predictions:600,settled_predictions:400,pending_predictions:200,settled_matches:20},
      windows:{pending:{max_predictions:20000},settled:{max_matches:64}},sampling:{max_matches:64,max_contracts_per_match:128,full_population:false,note:'原始预测在PG永久保留；已结窗口仅最近64场，不代表全量正确率'}}}}));
  await page.locator('#data-model-refresh').click();
  await page.getByText('已结窗口上限',{exact:true}).waitFor();
  shadow=await page.locator('.shadow-performance').textContent();
  for (const text of ['已结窗口上限64 场','每场完整合约上限128 个','待结窗口最多 20000 条预测','不代表全量正确率']) assert(shadow.includes(text),'settled scope '+text);
  await page.unroute('**/api/v1/data-model');
  for (const width of [1440,390,320]) {
    await page.setViewportSize({width,height:1000});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'no page overflow '+width);
    await page.screenshot({path:'/root/ai_football/output/playwright/shadow-statistics-'+width+'.png',fullPage:true});
  }
  mode='no-sample';await page.locator('#data-model-refresh').click();
  await page.getByText('暂无适用样本',{exact:true}).first().waitFor();
  mode='unapproved';await page.locator('#data-model-refresh').click();
  await page.getByText('不可用',{exact:true}).waitFor();
  assert(!(await page.locator('#model-summary').textContent()).includes('已加载'),'unapproved artifact cannot claim active');
  mode='stale';await page.locator('#data-model-refresh').click();
  await page.getByText('上次统计 ·',{exact:false}).waitFor();
  assert((await page.locator('#storage-summary').textContent()).includes('S3 归档文件2461111'),'actual archive totals render');
  assert(!(await page.locator('#storage-categories').textContent()).includes('undefined'),'missing numbers not blank undefined');
  assert((await page.locator('#storage-description').textContent()).includes('后台统计已过期'),'stale reason explicit');
  assert(errors.length===0,'no JS errors '+errors.join(';'));
  assert(writes.length===0,'read-only browser makes no API mutations');
  return {checks:'candidate/shadow pending/ready/top1/asian/null/stale/mobile/read-only',errors,writes};
}
