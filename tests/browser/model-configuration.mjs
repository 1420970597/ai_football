// Isolated UI contract fixtures; these do not claim real labelled quality evidence.
import { chromium } from '/opt/verification/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';

const root='/work/web/app';
const server=http.createServer(async(req,res)=>{
  try {
    const url=new URL(req.url,'http://localhost');
    const file=path.resolve(root,url.pathname.startsWith('/assets/')?url.pathname.slice(8):'index.html');
    if(!file.startsWith(root+'/')) throw new Error('invalid path');
    let body=await fs.readFile(file);
    if(file.endsWith('index.html')) body=Buffer.from(body.toString().replace(/<script\b[^>]*>[\s\S]*?<\/script>/g,''));
    res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'}).end(body);
  } catch {res.writeHead(404).end();}
});
await new Promise(resolve=>server.listen(8080,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,args:['--disable-dev-shm-usage']});
const results=[];
try {
  for(const width of [1440,390]) {
    const context=await browser.newContext({viewport:{width,height:960}});
    const page=await context.newPage(); const errors=[], calls=[];
    page.on('pageerror',error=>errors.push(error.message));
    const active={model:'current/model',version:'a'.repeat(64),provenance:{model_id:'current/model',model_revision:'legacy_unattested',
      prompt_sha256:'b'.repeat(64),skill_version:'legacy_unattested',hermes_version:'legacy_unattested',tool_contract_version:'legacy_unattested'}};
    const state={revision:1,active,candidates:[],consumption:null,application_state:'awaiting_consumer',reason:'消费不代表推理成功'};
    await page.route('**/api/v1/**',async route=>{
      const request=route.request(), url=new URL(request.url()), name=url.pathname.replace('/api/v1/admin','');
      const body=request.postDataJSON(); if(request.method()!=='GET') calls.push({name,body});
      let value={items:[]};
      if(name==='/settings') value={items:{hermes_analysis_model:{value:state.active.model},quote_max_age_seconds:{value:'15'},
        auto_betting_enabled:{value:'false'}},leyu_version:1,runtime:{state:'unavailable'},model_configuration:state};
      else if(name==='/model-configuration') value=state;
      else if(name==='/model-configuration/candidates') {
        assert.equal(body.expected_revision,state.revision);
        state.candidates.push({id:'fixture-candidate',model:body.model,provenance:body.provenance,fingerprint:'c'.repeat(64),
          status:'pending_evidence',created_at:'2026-10-27T18:00:00Z',report:null,review:null}); state.revision++;
        value={candidate:state.candidates[0],configuration:state};
      } else if(name.endsWith('/evidence')) {
        state.candidates[0].report={sha256:body.report_sha256,dataset_id:'ui-contract',changed_samples:1,synthetic_only:false,
          changed_sample_ids:['sample-1'],differences:[{sample_id:'sample-1',reasons:['<img src=x onerror="window.fixtureXss=1">']}]};
        state.candidates[0].status='awaiting_human_review';state.revision++;value=state;
      } else if(name.endsWith('/review')) {
        assert.deepEqual(body.decisions,[{sample_id:'sample-1',decision:'accept',note:'核验此界面契约差异'}]);
        state.candidates[0].status='reviewed';state.revision++;value=state;
      } else if(name.endsWith('/activate')) {
        state.active={...active,model:state.candidates[0].model,version:'d'.repeat(64)};
        state.candidates[0].status='activated';state.revision++;value=state;
      } else if(name==='/sources') value={items:[{id:'isolated-source',chat_id:'isolated-chat',display_name:'隔离来源',enabled:true,allow_media:false}]};
      else if(name==='/sources/isolated-source/experts') value={items:[
        {id:'active-author',nickname:'当前信任作者',sender_id:'active-1',enabled:true},
        {id:'legacy-author',nickname:'兼容历史字段作者',sender_id:'legacy-1'},
        {id:'disabled-author',nickname:'已移除作者',sender_id:'disabled-1',enabled:false}]};
      else if(name==='/status') value={database:'healthy',hermes:'unconfigured',telegram_sources:0,review_queue:0};
      else if(name==='/diagnostics') value={database:'healthy',schema:'verified',tasks:[],storage:'unconfigured'};
      else if(name==='/mfa/status') value={configuration_state:'missing',pending:null,recovery_codes_remaining:0};
      await route.fulfill({json:value});
    });
    await page.goto('http://127.0.0.1:8080');
    await page.evaluate(async()=>{
      const admin=await import('/assets/admin.js');
      admin.initAdmin({state:{admin:{user:{display_name:'隔离管理员',email:'admin@example.invalid'}}},loadIdentities:async()=>{},route:()=>{}});
      await admin.loadAdmin();
      document.querySelector('#view-admin').classList.add('active');
      document.querySelector('#view-admin').hidden=false;
      document.querySelector('#admin-system').hidden=false;
      document.querySelector('#model-configuration-section').open=true;
    });
    const trusted=page.locator('#trusted-experts');
    assert.match(await trusted.textContent(),/当前信任作者/);
    assert.match(await trusted.textContent(),/兼容历史字段作者/);
    assert.doesNotMatch(await trusted.textContent(),/已移除作者/);
    assert.equal(await trusted.locator('[data-untrust]').count(),2);
    const form=page.locator('#model-candidate-form');
    await form.locator('[name=model]').fill('candidate/model-v2');
    await form.locator('[name=model_revision]').fill('candidate-frozen-v2');
    await form.locator('button').click();
    await page.locator('#model-candidates summary').filter({hasText:'等待比较证据'}).waitFor();
    assert.match(await page.locator('#model-configuration-state').textContent(),/当前模型 current\/model/);
    await page.locator('#model-candidates details').first().evaluate(e=>e.open=true);
    await page.locator('.model-evidence-form [name=report]').fill('{"fixture":"UI only"}');
    await page.locator('.model-evidence-form [name=report_sha256]').fill('e'.repeat(64));
    await page.locator('.model-evidence-form button').click();
    await page.locator('.model-review-form').waitFor({state:'attached'});
    await page.locator('#model-candidates details').evaluateAll(items=>items.forEach(e=>e.open=true));
    await page.locator('.model-review-form select').selectOption('accept');
    await page.locator('.model-review-form input').fill('核验此界面契约差异');
    await page.locator('.model-review-form textarea').fill('全部界面契约差异已核验');
    await page.locator('.model-review-form button').click();
    await page.locator('[data-activate-model]').waitFor({state:'attached'});
    await page.locator('#model-candidates details').evaluateAll(items=>items.forEach(e=>e.open=true));
    await page.locator('[data-activate-model]').click();
    await page.locator('#confirm-dialog[open]').waitFor();
    assert.match(await page.locator('#confirm-message').textContent(),/确认来源与全部差异已核验/);
    assert.ok(!calls.some(item=>item.name.endsWith('/activate')));
    await page.locator('#confirm-no').click();
    assert.ok(!calls.some(item=>item.name.endsWith('/activate')));
    await page.locator('[data-activate-model]').click();
    await page.locator('#confirm-yes').click();
    await page.waitForFunction(()=>document.querySelector('#model-configuration-state').textContent.includes('当前模型 candidate/model-v2'));
    assert.equal(calls.filter(item=>item.name.endsWith('/activate')).length,1);
    assert.equal(await page.evaluate(()=>window.fixtureXss),undefined);
    assert.deepEqual(errors,[]);
    console.log('PASS isolated model governance UI width='+width+' candidate/evidence/review/cancel/activate + disabled trust isolation');
    results.push({width,status:'passed',checks:['candidate','evidence','all_differences_review','cancel_without_activation',
      'explicit_single_activation','text_escaping','disabled_trust_isolation','no_page_errors']});
    await context.close();
  }
} finally {
  await browser.close();await new Promise(resolve=>server.close(resolve));
  if(process.env.MODEL_UI_RESULTS) await fs.writeFile(process.env.MODEL_UI_RESULTS,JSON.stringify({
    scope:'model_configuration_ui',real_browser:true,synthetic_data:true,production_evidence:false,
    browser_version:browser.version(),status:results.length===2?'passed':'failed',results},null,2)+'\n');
}
