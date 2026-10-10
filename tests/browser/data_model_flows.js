async (page) => {
  const assert = (condition,message) => {if (!condition) throw new Error(message);};
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  let mode='empty',saved,modelRequests=0,recommendationRequests=0,unavailableRecommendations=true;
  const settings={version:1,persistent:true,algorithms:{poisson_market:'当前盘口 Poisson',poisson_time_decay:'时间衰减 Poisson'},capabilities:{betting:{execution_supported:true}},
    settings:{algorithms:['poisson_market','poisson_time_decay'],primary_algorithm:'poisson_time_decay',devig_method:'proportional',min_probability:.52,min_ev:.02,
      quote_max_age_s:15,state_max_age_s:90,anchor_max_age_s:120,devig_spread_warn_pp:1,fractional_kelly:.25,max_total_exposure:.25,risk_correlation:.4,execution_cost:.001,
      weight_prior_matches:20,max_algorithm_weight:.6,algorithm_alert_enabled:true,algorithm_alert_min_samples:30,algorithm_alert_threshold:.5,
      betting_enabled:false,betting_stake_mode:'fixed',betting_fixed_stake:10,betting_min_hit_count:3,
      llm_enabled:false,llm_experiment_enabled:true,llm_base_url:'',llm_model:'',llm_fallback_models:'',llm_timeout_s:20,llm_temperature:.2,llm_max_tokens:2048,llm_interval_s:60,llm_review_max_age_s:120,
      model_training_enabled:true,model_training_matches:100,model_training_cpu:1,model_training_memory_mb:512}};
  const fixture={model:{},training:{state:'waiting',eligible_matches:42,new_matches:42},settings:{cycle_matches:100,cpu_cores:1,memory_mb:512},running:true,
    note:'独立观察预测，走势冻结在决策输入截点。',prospective:{},storage:{at:'2026-10-10T08:00:00Z',bytes:1073741824,files:99,
    disk:{total:100*1073741824,used:80*1073741824,free:20*1073741824},records:{decisions:1234,matches:42,settled_decisions:900},categories:[{name:'走势',files:99,bytes:1073741824,allocated_bytes:1073741824}]}};
  await page.route('**/api/v1/**',async route=>{
    const path=new URL(route.request().url()).pathname;
    if(path.endsWith('/recommendations')) {recommendationRequests++;return route.fulfill(unavailableRecommendations ? {status:502,json:{error:'test upstream unavailable'}} : {json:{open:[],closed:[],summary:{}}});}
    if(path.endsWith('/data-model')){
      modelRequests++;
      if(mode==='missing') return route.fulfill({status:404,json:{error:'unknown endpoint'}});
      if(mode==='error') return route.fulfill({status:502,json:{error:'fixture offline'}});
      if(mode==='loading') await new Promise(resolve=>setTimeout(resolve,800));
      const data=JSON.parse(JSON.stringify(fixture));
      if(mode==='trained') {data.model={architecture:'L2 Logistic',version:'test-20261010',parameter_count:30,updated_at:'2026-10-10T07:00:00Z',validation:{matches:24,decisions:480,accuracy:.7,brier:.21,log_loss:.62},baseline:{matches:24,decisions:480,accuracy:.6,brier:.24,log_loss:.7}};data.training.state='ready';}
      if(mode==='failed') {data.training.state='failed';data.training.error='训练进程被终止（资源限制或超时）';}
      return route.fulfill({json:data});
    }
    if(path.endsWith('/settings')){
      if(route.request().method()==='POST') {saved=route.request().postDataJSON();Object.assign(settings.settings,saved.settings);settings.version++;}
      return route.fulfill({json:settings});
    }
    return route.fulfill({json:{matches:[],available:false}});
  });
  const origin=new URL(page.url()).origin;
  await page.setViewportSize({width:1440,height:1000});
  await page.goto(origin+'/#data-model');
  await page.getByText('等待新增已结算比赛',{exact:true}).waitFor();
  assert((await page.locator('#model-summary').textContent()).includes('未训练'),'no fictional model parameters');
  assert((await page.locator('#model-performance').textContent()).includes('暂无有效样本'),'no fake zero accuracy');
  assert((await page.locator('#storage-summary').textContent()).includes('20.00 GiB'),'real disk free shown');
  mode='missing';await page.locator('#data-model-refresh').click();
  await page.getByText('当前后端版本尚未提供数据与模型接口，请构建并更新 analytics-api 后手动刷新。',{exact:true}).waitFor();
  const afterMissing=modelRequests;await page.waitForTimeout(5500);
  assert(modelRequests===afterMissing,'404 automatic polling stopped');
  mode='trained'; await page.locator('#data-model-refresh').click();
  await page.getByText('test-20261010',{exact:true}).waitFor();
  assert((await page.locator('#model-performance').textContent()).includes('70.0%'),'validation metrics shown');
  assert((await page.locator('#model-performance').textContent()).includes('当前版本前瞻验证'),'prospective separated');
  await page.screenshot({path:'/root/ai_football/output/playwright/task32-model-desktop.png',fullPage:true});
  mode='failed';await page.locator('#data-model-refresh').click();
  await page.getByText('训练失败',{exact:true}).waitFor();
  assert(await page.locator('#data-model-notice.error').isVisible(),'training error visible');
  mode='error';await page.locator('#data-model-refresh').click();
  await page.getByText('读取失败（502），请刷新重试。',{exact:true}).waitFor();
  await page.locator('#nav-settings').click();
  await page.locator('#setting-min_ev').fill('0.07');
  await page.locator('#settings-categories').getByRole('button',{name:'数据与模型',exact:true}).click();
  assert(await page.locator('#setting-model_training_matches').isVisible(),'three level default group');
  await page.locator('#setting-model_training_matches').fill('12');
  await page.locator('#settings-groups').getByRole('button',{name:'资源限制',exact:true}).click();
  await page.locator('#setting-model_training_cpu').fill('2');
  await page.locator('#setting-model_training_memory_mb').fill('768');
  assert(!(await page.locator('#setting-model_training_matches').isVisible()),'only chosen group visible');
  await page.locator('#settings-categories').getByRole('button',{name:'决策',exact:true}).click();
  assert(await page.locator('#setting-min_ev').inputValue()==='0.07','draft retained across categories');
  await page.locator('#settings-save').click();
  await page.getByText('v2 已生效',{exact:true}).waitFor();
  assert(saved.settings.model_training_cpu===2 && saved.settings.model_training_memory_mb===768 && saved.settings.model_training_matches===12,'hidden groups submitted');
  await page.locator('#settings-categories').getByRole('button',{name:'数据与模型',exact:true}).click();
  await page.locator('#settings-groups').getByRole('button',{name:'资源限制',exact:true}).click();
  await page.locator('#setting-model_training_cpu').fill('3');
  await page.locator('#settings-categories').getByRole('button',{name:'决策',exact:true}).click();
  await page.locator('#settings-save').click();
  assert(await page.locator('#setting-model_training_cpu').isVisible(),'validation navigates to invalid hidden group');
  assert(settings.version===2,'invalid value not submitted');
  await page.locator('#setting-model_training_cpu').fill('2');
  for(const width of [390,320]){
    await page.setViewportSize({width,height:844});
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'settings mobile no page overflow '+width);
    await page.screenshot({path:'/root/ai_football/output/playwright/task32-settings-'+width+'.png',fullPage:true});
    mode='trained';await page.locator('#nav-data-model').click();
    await page.getByText('test-20261010',{exact:true}).waitFor();
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'dashboard mobile no overflow '+width);
    await page.screenshot({path:'/root/ai_football/output/playwright/task32-model-'+width+'.png',fullPage:true});
    await page.locator('#nav-settings').click();
  }
  await page.locator('#nav-recommendations').click();
  await page.getByText('后端服务暂不可用，保留上次结果，30秒后自动重试，也可点击刷新。',{exact:true}).waitFor();
  const failedRequests=recommendationRequests;await page.waitForTimeout(5500);
  assert(recommendationRequests===failedRequests,'502 polling backs off');
  unavailableRecommendations=false;await page.locator('#recommendations-refresh').click();
  await page.getByText('暂无开启盘口推荐',{exact:true}).waitFor();
  assert(recommendationRequests===failedRequests+1,'manual refresh bypasses backoff');
  assert(errors.length===0,'no JS errors: '+errors.join(';'));
  return {checks:'dashboard/three levels/drafts/hidden validation/save/failure/mobile320+390',errors};
}
