// Synthetic account settings only; all API traffic is intercepted, no executor.
import {chromium} from '/opt/verification/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import crypto from 'node:crypto';
import {fixtureRoute,instant,expert} from './visual-fixture.mjs';
import {comparePixels,decodePng} from './visual-pixels.mjs';

const output=process.env.PROFITABLE_BROWSER_RESULTS||'/evidence';
const baseline='/work/tests/browser/profitable-expert-baselines';
const update=process.argv.includes('--update');
if(update&&process.env.VISUAL_BASELINE_UPDATE!=='reviewed')throw Error('reviewed baseline update required');
const sha=value=>crypto.createHash('sha256').update(value).digest('hex');
const server=http.createServer(async(req,res)=>{
  try{
    const url=new URL(req.url,'http://localhost');
    const target=path.resolve('/work/web/app',url.pathname.startsWith('/assets/')?url.pathname.slice(8):'index.html');
    if(!target.startsWith('/work/web/app/'))throw Error('invalid path');
    const raw=await fs.readFile(target);
    res.writeHead(200,{'Content-Type':target.endsWith('.js')?'text/javascript':target.endsWith('.css')?'text/css':'text/html'}).end(raw);
  }catch{res.writeHead(404).end();}
});
await fs.mkdir(output,{recursive:true});
await new Promise(resolve=>server.listen(8080,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,args:['--disable-dev-shm-usage']});
const results=[],errors=[];
const policy={channel_threshold:16,max_changed_ratio:.002,browser_version:browser.version(),synthetic_data:true,production_evidence:false};
let completed=false;
try{
  for(const width of [1440,390]){
    const context=await browser.newContext({viewport:{width,height:width===390?844:960},locale:'zh-CN',timezoneId:'Asia/Shanghai',deviceScaleFactor:1,reducedMotion:'reduce'});
    const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
    let b={auto_enabled:true,emergency_stopped:false,expert_selection_mode:'manual',allowed_experts:[expert.id],stake_per_order:'2',max_per_order:'3',daily_limit:'10',open_exposure_limit:'5',daily_timezone:'Asia/Shanghai'};
    const writes=[];
    await page.route('**/api/v1/**',async route=>{
      const request=route.request(),p=new URL(request.url()).pathname.replace('/api/v1','');
      if(p==='/auth/me')return route.fulfill({json:{user:{id:'synthetic-owner',role:'user',display_name:'隔离验收用户',email:'synthetic@test.invalid'},membership:{name:'专家会员',entitlements:{automatic_betting:true}}}});
      if(p==='/accounts/leyu')return route.fulfill({json:{account:{account_label:'隔离账户',health_state:'healthy',renewal_state:'ready',last_verified_at:instant},betting:b,tasks:[],execution_capability:{live_enabled:true,supported_sports:['football','basketball'],supported_phases:['scheduled','live'],prematch:'supported',basketball:'full_time_winner_total_handicap'}}});
      if(p==='/accounts/orders')return route.fulfill({json:{items:[],next_cursor:null}});
      if(p==='/accounts/leyu/auto-betting'){
        assert.equal(request.method(),'PUT');const body=request.postDataJSON();writes.push(body);
        b={...b,...body,auto_enabled:body.enabled};
        return route.fulfill({json:{enabled:body.enabled,expert_selection_mode:body.expert_selection_mode}});
      }
      return fixtureRoute(route);
    });
    await page.goto('http://127.0.0.1:8080/#account');
    const form=page.locator('#betting-form'),mode=form.locator('[name=expert_selection_mode]');
    await mode.waitFor();await page.waitForFunction(()=>document.querySelector('#betting-form').getAttribute('aria-busy')==='false');
    assert.equal(await mode.inputValue(),'manual');assert.equal(writes.length,0,'loading must not authorize a new mode');
    assert.ok(!(await page.locator('#betting-status').innerText()).includes('仅支持足球滚球'));
    assert.ok(!(await page.locator('#betting-status').innerText()).includes('赛前及篮球专家推荐仍可浏览'));
    assert.ok((await page.locator('#betting-expert-mode-note').innerText()).includes('不限于下方已选名单'));
    assert.ok((await page.locator('#betting-expert-mode-note').innerText()).includes('足球赛前与滚球，以及篮球全场胜负、大小、让分的赛前与滚球'));
    assert.ok((await form.locator('[name=confirmed]').locator('..').innerText()).includes('足球赛前与滚球、篮球全场胜负及大小让分的赛前与滚球'));
    assert.equal(await form.locator('[name=expert]').first().isChecked(),true);
    await mode.selectOption('profitable');
    assert.equal(await form.locator('[name=expert]').first().isDisabled(),true);
    assert.equal(writes.length,0,'selecting alone must not save or enable');
    await page.locator('#betting-expert-mode-control').scrollIntoViewIfNeeded();await page.evaluate(()=>document.fonts.ready);
    if(process.env.VISUAL_PROBE_MUTATION==='mode')await mode.evaluate(el=>el.style.backgroundColor='#ff0000');
    const file='profitable-mode-'+width+'.png',raw=await page.locator('#betting-expert-mode-control').screenshot({animations:'disabled'});
    const visual={width,file,sha256:sha(raw)};
    if(update){await fs.mkdir(baseline,{recursive:true});await fs.writeFile(baseline+'/'+file,raw);visual.status='baseline_written';}
    else{
      const manifest=JSON.parse(await fs.readFile(baseline+'/manifest.json'));
      for(const [key,value]of Object.entries(policy))assert.deepEqual(manifest[key],value);
      const previous=await fs.readFile(baseline+'/'+file);assert.equal(sha(previous),manifest.screens.find(x=>x.file===file).sha256);
      visual.diff=comparePixels(decodePng(previous),decodePng(raw));visual.status=visual.diff.passed?'passed':'failed';
    }
    await fs.writeFile(output+'/'+file,raw);
    // Native validation must block save without owner confirmation.
    await form.locator('button.primary-button').click();assert.equal(writes.length,0);
    await form.locator('[name=confirmed]').check();
    const saved=page.waitForResponse(r=>r.url().endsWith('/accounts/leyu/auto-betting'));
    await form.locator('button.primary-button').click();await saved;
    await page.waitForFunction(()=>document.querySelector('#betting-form').getAttribute('aria-busy')==='false');
    assert.equal(writes.length,1);assert.equal(writes[0].expert_selection_mode,'profitable');
    assert.deepEqual(writes[0].allowed_experts,[expert.id],'saved manual list must survive disabled checkboxes');
    for(const[key,value]of Object.entries({stake_per_order:'2',max_per_order:'3',daily_limit:'10',open_exposure_limit:'5'}))assert.equal(writes[0][key],value);
    assert.equal(await mode.inputValue(),'profitable');
    assert.ok((await page.locator('#betting-status').innerText()).includes('近30天全部盈利专家'));
    assert.ok(!(await page.locator('#betting-status').innerText()).includes('赛前及篮球专家推荐仍可浏览'));
    await mode.selectOption('manual');assert.equal(await form.locator('[name=expert]').first().isEnabled(),true);
    await form.locator('[name=expert]').first().uncheck();
    await form.locator('button.primary-button').click();await page.waitForTimeout(100);assert.equal(writes.length,1,'manual mode without expert must be rejected');
    await form.locator('[name=expert]').first().check();
    await form.locator('[name=confirmed]').check();
    const restored=page.waitForResponse(r=>r.url().endsWith('/accounts/leyu/auto-betting'));
    await form.locator('button.primary-button').click();await restored;
    assert.equal(writes[1].expert_selection_mode,'manual');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,'no horizontal overflow');
    results.push({...visual,clicks:'passed',owner_confirmation:'required',manual_restored:true});
    await context.close();
  }
  assert.deepEqual(errors,[]);
  if(update)await fs.writeFile(baseline+'/manifest.json',JSON.stringify({...policy,screens:results},null,2)+'\n');
  completed=true;
}finally{
  await browser.close();await new Promise(resolve=>server.close(resolve));
  await fs.writeFile(output+'/profitable-browser-results.json',JSON.stringify({...policy,status:completed&&results.every(r=>r.status!=='failed')?'passed':'failed',errors,screens:results},null,2)+'\n');
}
assert.ok(results.every(r=>r.status!=='failed'),'new control visual differences exceed unchanged threshold');
console.log('PASS profitable expert controls, owner confirmation, preserved limits and manual rollback at desktop/mobile widths');
