async (page) => {
  const assert = (condition, message) => {if (!condition) throw new Error(message);};
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let mode = 'loading', saved;
  const fixture = {version:1, persistent:true, algorithms:{
    poisson_market:'当前盘口 Poisson', poisson_time_decay:'时间衰减 Poisson', devig_consensus:'去水共识',
    economics_risk_adjusted:'经济学风控', microstructure_adjusted:'盘口微观结构'},
    capabilities:{betting:{execution_supported:true,reason:'测试配置',configured_enabled:false}},
    settings:{decision_mode:'algorithm',model_blend_weight:.5,algorithms:['poisson_market','poisson_time_decay'],primary_algorithm:'poisson_time_decay',devig_method:'proportional',
      min_probability:.52,min_ev:.02,quote_max_age_s:15,state_max_age_s:90,anchor_max_age_s:120,devig_spread_warn_pp:1,fractional_kelly:.25,
      weight_prior_matches:20,max_algorithm_weight:.6,algorithm_alert_enabled:true,algorithm_alert_min_samples:30,algorithm_alert_threshold:.5,
      betting_enabled:false,betting_stake_mode:'fixed',betting_fixed_stake:10,betting_min_hit_count:3,betting_min_evidence_matches:30,betting_min_edge:.05,
      betting_retry_max_attempts:5,betting_retry_base_delay_s:2,max_total_exposure:.25,risk_correlation:.4,execution_cost:.001,
      llm_enabled:false,llm_experiment_enabled:true,llm_base_url:'',llm_model:'',llm_fallback_models:'',has_llm_key:false,
      llm_timeout_s:20,llm_temperature:.2,llm_max_tokens:2048,llm_interval_s:60,llm_review_max_age_s:120,
      model_shadow_enabled:true,model_training_enabled:true,model_training_matches:100,model_training_cpu:1,model_training_memory_mb:2048}};
  await page.route('**/api/v1/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/settings')) {
      if (mode === 'error') return route.fulfill({status:500,json:{error:'fixture read failure'}});
      if (mode === 'loading') await new Promise(resolve => setTimeout(resolve,500));
      const data = JSON.parse(JSON.stringify(fixture));
      if (mode === 'old') {delete data.settings.decision_mode; delete data.settings.model_blend_weight;}
      if (route.request().method() === 'POST') {
        saved = route.request().postDataJSON();
        Object.assign(fixture.settings, saved.settings);
        data.settings = fixture.settings;
        data.version = ++fixture.version;
      }
      return route.fulfill({json:data});
    }
    if (path.endsWith('/bet/status')) return route.fulfill({json:fixture.capabilities.betting});
    return route.fulfill({json:{matches:[],available:false}});
  });
  const origin = new URL(page.url()).origin;
  await page.setViewportSize({width:1440,height:1000});
  await page.goto(origin + '/?test=settings-help#settings', {waitUntil:'domcontentloaded'});
  assert(await page.locator('#settings-save').isDisabled(), 'save disabled while settings load');
  await page.locator('#setting-decision_mode').waitFor();
  mode = 'normal';
  const controls = await page.locator('#settings-fields input,#settings-fields select').count();
  assert(await page.locator('#settings-fields .setting-help').count() === controls, 'every setting has one help icon');
  assert(await page.evaluate(() => Array.from(document.querySelectorAll('#settings-fields .setting-help')).every(button =>
    button.textContent === '?' && button.type === 'button' && button.title.length > 15 && button.getAttribute('aria-label') &&
    document.getElementById(button.getAttribute('aria-controls'))?.textContent !== '此设置的说明暂不可用。')), 'all settings have explicit accessible explanations');
  assert(await page.evaluate(() => Array.from(document.querySelectorAll('#settings-fields input,#settings-fields select')).every(input =>
    document.querySelector('label[for="' + input.id + '"]') && input.getAttribute('aria-describedby'))), 'all settings keep explicit input labels and descriptions');
  const algorithmHelp = page.locator('#setting-help-algorithm-poisson_time_decay');
  await page.getByRole('button', {name:'时间衰减 Poisson的设置说明',exact:true}).click();
  assert(await algorithmHelp.isVisible(), 'algorithm help can be opened');
  assert((await algorithmHelp.textContent()).includes('max(0,5400−t)/max(1,5400−t_anchor)'), 'time decay uses the implementation formula');
  assert(await page.locator('#setting-algorithm-poisson_time_decay').isChecked(), 'help does not toggle algorithm input');
  await page.keyboard.press('Escape');
  assert(!await algorithmHelp.isVisible(), 'Escape closes focused help');
  const modeHelp = page.getByRole('button', {name:'决策模式的设置说明',exact:true});
  await modeHelp.focus(); await page.keyboard.press('Enter');
  assert(await page.locator('#setting-help-decision_mode').isVisible(), 'keyboard opens help');
  assert((await page.locator('#setting-help-decision_mode').textContent()).includes('无有效模型时不产生买入'), 'pure model unavailable behavior explained');
  await modeHelp.click();
  assert(!await page.locator('#setting-help-decision_mode').isVisible(), 'second click closes help');
  await page.locator('#setting-decision_mode').selectOption('hybrid');
  await page.locator('#setting-model_blend_weight').fill('0.35');
  await page.getByRole('button', {name:'混合模型权重的设置说明',exact:true}).click();
  assert((await page.locator('#setting-help-model_blend_weight').textContent()).includes('(1−w)·p_algorithm+w·p_model'), 'hybrid formula visible');
  await page.screenshot({path:'/root/ai_football/output/playwright/settings-help-desktop.png',fullPage:true});
  await page.locator('#settings-save').click();
  await page.getByText('v2 已生效', {exact:true}).waitFor();
  assert(saved.settings.decision_mode === 'hybrid' && saved.settings.model_blend_weight === .35, 'decision mode and blend weight submitted');
  await page.locator('#settings-submenus').getByRole('button', {name:'风险与预警',exact:true}).click();
  await page.getByRole('button', {name:'分数 Kelly的设置说明',exact:true}).click();
  assert((await page.locator('#setting-help-fractional_kelly').textContent()).includes('(o·p−1)/(o−1)'), 'Kelly formula shown');
  for (const width of [390,320]) {
    await page.setViewportSize({width,height:844});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'long formulas do not overflow mobile width ' + width);
    assert(await page.locator('#setting-help-fractional_kelly').isVisible(), 'mobile formula remains visible');
    await page.screenshot({path:'/root/ai_football/output/playwright/settings-help-mobile-' + width + '.png',fullPage:true});
  }
  await page.locator('#settings-categories').getByRole('button', {name:'LLM',exact:true}).click();
  await page.getByRole('button', {name:'清除已保存 API Key的设置说明',exact:true}).click();
  assert(!await page.locator('#clear-llm-key').isChecked(), 'help does not clear a saved key');
  assert(await page.locator('#setting-help-clear_llm_key').isVisible(), 'clear-key setting has help');
  mode = 'error';
  await page.locator('#settings-reload').click();
  await page.getByText('读取失败（500）', {exact:true}).waitFor();
  assert(await page.locator('#setting-help-clear_llm_key').isVisible(), 'read error preserves existing explanations');
  mode = 'old';
  await page.locator('#settings-reload').click();
  await page.locator('#settings-categories').getByRole('button', {name:'决策',exact:true}).click();
  assert(await page.locator('#setting-decision_mode').isDisabled(), 'older API cannot save unsupported decision mode');
  assert(await page.locator('#setting-model_blend_weight').isDisabled(), 'older API cannot save unsupported blend weight');
  await page.getByRole('button', {name:'混合模型权重的设置说明',exact:true}).click();
  assert(await page.locator('#setting-help-model_blend_weight').isVisible(), 'unavailable setting retains its explanation');
  assert(errors.length === 0, 'no uncaught browser errors: ' + errors.join('; '));
  return {checks:'all labels/help/formulas/keyboard/no toggle/modes/save/loading/error/old API/mobile 320+390', controls, errors};
}
