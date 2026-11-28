// Synthetic public contracts for clicks and stale-response isolation; no provider or writes.
import {chromium} from '/opt/verification/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import {fixtureRoute,instant} from './visual-fixture.mjs';

const output=process.env.BROWSER_EVIDENCE_DIR||'/evidence';
const server=http.createServer(async(req,res)=>{
  try{
    const target=path.resolve('/work/web/app',new URL(req.url,'http://localhost').pathname.startsWith('/assets/')?req.url.slice(8):'index.html');
    if(!target.startsWith('/work/web/app/'))throw Error('invalid path');
    const raw=await fs.readFile(target);
    res.writeHead(200,{'Content-Type':target.endsWith('.js')?'text/javascript':target.endsWith('.css')?'text/css':'text/html'}).end(raw);
  }catch{res.writeHead(404).end();}
});
await fs.mkdir(output,{recursive:true});
await new Promise(resolve=>server.listen(8080,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,args:['--disable-dev-shm-usage']});
const results=[],errors=[];
const experts=sport=>[{id:'sport-expert',display_nickname:'分运动隔离专家',settled_signals:7,effective_settled_signals:4,total_signals:7,push:2,void:1,
  win:sport==='football'?3:1,loss:sport==='football'?1:3,accuracy:sport==='football'?.75:.25,
  unit_net:sport==='football'?2:-2,unit_roi:sport==='football'?.5:-.5,average_unit_net:sport==='football'?.5:-.5,
  excluded_signals:0},{id:'empty-expert',display_nickname:'无样本专家',effective_settled_signals:0,settled_signals:0,total_signals:0,accuracy:null,unit_net:0},
  {id:'neutral-expert',display_nickname:'走盘作废样本',effective_settled_signals:0,settled_signals:3,total_signals:3,push:2,void:1,accuracy:null,unit_net:0},
  {id:'half-expert',display_nickname:'半赢半输样本',effective_settled_signals:2,settled_signals:2,total_signals:2,half_win:1,half_loss:1,effective_stake:1,accuracy:.5,unit_net:0},
  {id:'legacy-expert',display_nickname:'旧分类样本',settled_signals:5,total_signals:5,win:1,half_win:1,loss:0,half_loss:1,push:2,accuracy:.75,unit_net:0},
  {id:'unknown-expert',display_nickname:'字段未知样本',effective_settled_signals:null,settled_signals:9,total_signals:9,win:9,half_win:0,loss:0,half_loss:0,accuracy:null,unit_net:0},
  {id:'zero-expert',display_nickname:'明确零样本',effective_settled_signals:0,settled_signals:9,total_signals:9,win:9,half_win:0,loss:0,half_loss:0,accuracy:null,unit_net:0}];
try{
  for(const width of [1440,390]){
    const context=await browser.newContext({viewport:{width,height:960},locale:'zh-CN'}),page=await context.newPage();
    page.on('pageerror',e=>errors.push(e.message));
    const reads=[],writes=[];let holdFootball=false,releaseHeld,startedHeld;
    const held=new Promise(resolve=>{startedHeld=resolve;});
    await page.route('**/api/v1/**',async route=>{
      const request=route.request(),url=new URL(request.url()),p=url.pathname.replace('/api/v1','');
      if(request.method()!=='GET')writes.push(p);
      if(p.startsWith('/public/experts')||p==='/public/signals'){
        const sport=url.searchParams.get('sport');assert.ok(['football','basketball'].includes(sport));
        reads.push({path:p,sport});
        if(p==='/public/signals'){
          if(holdFootball&&sport==='football'){
            holdFootball=false;startedHeld();await new Promise(resolve=>{releaseHeld=resolve;});
          }
          return route.fulfill({json:{items:[{id:sport+'-signal',expert_id:'sport-expert',display_nickname:'分运动隔离专家',
            fixture_id:sport+'-match',sport,home_team:sport==='football'?'足球主队':'篮球主队',away_team:'隔离客队',
            competition_name:'隔离联赛',phase:'scheduled',fixture_phase:'scheduled',market_name:'大小',market_kind:'total',
            period:'full_time',line:sport==='football'?'2.5':'220.5',selection:'over',recommendation_price:'1.95',
            outcome:'pending',created_at:instant,quote_time:instant}],next_cursor:null,limited:true,sport}});
        }
        const summary=experts(sport)[0],scope={from_utc:instant,to_utc:instant,filters:{sport}};
        if(p.endsWith('/trend'))return route.fulfill({json:{items:[{day:instant,cumulative_unit_net:summary.unit_net}],summary,...scope}});
        if(p==='/public/experts')return route.fulfill({json:{items:experts(sport),source_options:[],...scope}});
        return route.fulfill({json:{expert:summary,...scope}});
      }
      return fixtureRoute(route);
    });
    async function selectSport(sport){
      const response=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/public/experts'&&new URL(r.url()).searchParams.get('sport')===sport);
      await page.locator(`[data-expert-sport="${sport}"]`).click();await response;
      await page.waitForFunction(sport=>document.querySelector('#experts-title').textContent===(sport==='basketball'?'篮球':'足球')+'专家推荐',sport);
      assert.equal(await page.locator(`[data-expert-sport="${sport}"]`).getAttribute('aria-pressed'),'true');
      assert.equal(new URL(page.url()).hash,'#experts?sport='+sport);
    }
    await page.goto('http://127.0.0.1:8080/#experts');
    await page.waitForSelector('#experts-list [data-expert="sport-expert"]');
    assert.equal(await page.locator('#expert-stats-filter [name=sport]').inputValue(),'football');
    assert.match(await page.locator('#experts-list').innerText(),/75\.0%/);
    assert.equal(await page.locator('[data-expert-row="sport-expert"] > span').first().innerText(),'4','push and void are settled but not effective samples');
    assert.equal(await page.locator('[data-expert-row="neutral-expert"] > span').first().innerText(),'0');
    assert.match(await page.locator('[data-expert-row="neutral-expert"]').innerText(),/样本不足/);
    assert.equal(await page.locator('[data-expert-row="half-expert"] > span').first().innerText(),'2','two half outcomes remain two effective records');
    assert.match(await page.locator('[data-expert-row="half-expert"]').innerText(),/50\.0%/,'accuracy weights the two half stakes');
    assert.equal(await page.locator('[data-expert-row="legacy-expert"] > span').first().innerText(),'3');
    assert.equal(await page.locator('[data-expert-row="unknown-expert"] > span').first().innerText(),'样本不足','explicit null must not use old count fallback');
    assert.equal(await page.locator('[data-expert-row="zero-expert"] > span').first().innerText(),'0','explicit zero must not use old count fallback');
    assert.match(await page.locator('[data-expert-row="empty-expert"]').innerText(),/样本不足/);
    await selectSport('basketball');
    await page.waitForFunction(()=>document.querySelector('#signals-list').textContent.includes('篮球主队'));
    assert.match(await page.locator('#experts-list').innerText(),/25\.0%/);
    const before=reads.length;
    await page.locator('#experts-list [data-expert="sport-expert"]').click();
    await page.locator('.yield-chart').waitFor();
    assert.match(await page.locator('#dialog-content').innerText(),/篮球/);
    assert.match(await page.locator('#dialog-content').innerText(),/25\.0%/);
    assert.match(new URL(page.url()).hash,/sport=basketball/);
    assert.ok(reads.slice(before).filter(x=>x.path.includes('sport-expert')).every(x=>x.sport==='basketball'));
    await page.locator('#content-dialog [data-close-dialog]').click();
    await page.locator('#content-dialog').waitFor({state:'hidden'});
    holdFootball=true;await selectSport('football');await held;
    await selectSport('basketball');
    await page.waitForFunction(()=>document.querySelector('#signals-list').textContent.includes('篮球主队'));
    const late=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/public/signals'&&new URL(r.url()).searchParams.get('sport')==='football');
    releaseHeld();await (await late).finished();
    await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
    assert.doesNotMatch(await page.locator('#signals-list').innerText(),/足球主队/);
    assert.match(await page.locator('#signals-list').innerText(),/篮球主队/);
    await page.locator('[data-expert-tab="signals"]').click();
    assert.equal(await page.locator('#expert-signals').isVisible(),true);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:output+'/sports-experts-'+width+'.png'});
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#signals-list').textContent.includes('篮球主队'));
    assert.equal(await page.locator('[data-expert-sport="basketball"]').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('#expert-stats-filter [name=sport]').inputValue(),'basketball');
    assert.match(await page.locator('#experts-list').innerText(),/25\.0%/);
    assert.deepEqual(writes,[]);
    results.push({width,passed:true,scope:'football_and_basketball_separate',late_response_ignored:true,sample_insufficiency_visible:true,production_writes:0});
    await context.close();
  }
  assert.deepEqual(errors,[]);
}catch(e){results.push({passed:false,category:e.name,message:e.message});process.exitCode=1;}
finally{
  await browser.close();await new Promise(resolve=>server.close(resolve));
  await fs.writeFile(output+'/sports-expert-statistics-results.json',JSON.stringify({results,errors,synthetic_data:true,provider_settlement_proof:false,production_writes:0},null,2)+'\n');
}
console.log(JSON.stringify(results));
