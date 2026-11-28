// Explicit synthetic pagination fixtures; no upstream, production data or real funds.
import {chromium} from '/opt/verification/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';

const root='/work/web/app',output=process.env.MATCH_MARKETS_RESULTS||'/evidence';
const instant='2026-10-26T11:11:00Z';
const fixture=(id,home,away)=>({id,sport:'football',competition_name:'明确隔离分页测试联赛',home_team:home,away_team:away,
  phase:'live',match_state:'active',kickoff_at:instant,source_updated_at:instant,home_score:1,away_score:0,clock_minute:63,
  quote_fresh:true,markets:[]});
const first=fixture('fixture-a','分页主队A','分页客队A'),second=fixture('fixture-b','隔离主队B','隔离客队B');
const quotes=Array.from({length:1207},(_,i)=>({quote_id:'fixture-a-quote-'+String(i).padStart(4,'0'),
  market_id:'fixture-market-'+i,market_name:'亚洲让球',market_kind:'handicap',period:i%2?'first_half':'full_time',
  line:String(i/4),selection:'home',price:'1.96',status:'open',fresh:true,is_main:i%4!==1,source_updated_at:instant}));
const otherQuotes=Array.from({length:5},(_,i)=>({...quotes[i],quote_id:'fixture-b-quote-'+i}));
first.markets=[quotes[0]];second.markets=[otherQuotes[0]];
const server=http.createServer(async(req,res)=>{
  try{
    const url=new URL(req.url,'http://localhost'),file=path.resolve(root,url.pathname.startsWith('/assets/')?url.pathname.slice(8):'index.html');
    if(!file.startsWith(root+'/'))throw Error('invalid local path');
    const body=await fs.readFile(file);
    res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'}).end(body);
  }catch{res.writeHead(404).end();}
});
await fs.mkdir(output,{recursive:true});
await new Promise(resolve=>server.listen(8080,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,args:['--disable-dev-shm-usage']});
const results=[],errors=[];let complete=false;
const digest=value=>crypto.createHash('sha256').update(value).digest('hex');
try{
  for(const width of [1440,390]){
    const context=await browser.newContext({viewport:{width,height:width===390?844:960},deviceScaleFactor:1,
      isMobile:width===390,locale:'zh-CN',timezoneId:'Asia/Shanghai',reducedMotion:'reduce'});
    const page=await context.newPage();page.setDefaultTimeout(10000);
    // Freeze scheduled 20-second background refresh, not any tested user action.
    await page.clock.install({time:new Date(instant)});await page.clock.pauseAt(new Date(instant));
    page.on('pageerror',error=>errors.push({width,message:error.message}));
    let failNext=true,mode='normal',releaseDelayed,delayedStarted;
    const calls=[],unknown=[];
    await page.route('**/api/v1/**',async route=>{
      const request=route.request(),url=new URL(request.url()),name=url.pathname.replace('/api/v1','');
      const reply=(json,status=200)=>route.fulfill({status,json});
      if(request.method()!=='GET')throw Error('read-only fixture unexpectedly received a mutation');
      if(name==='/auth/me'||name==='/admin/me')return reply({detail:'尚未登录'},401);
      if(name==='/public/experts'||name==='/public/signals')return reply({items:[],next_cursor:null});
      if(name==='/public/leagues')return reply({items:[]});
      if(name==='/public/matches')return reply({items:[first,second],next_cursor:null,server_time:instant});
      if(name==='/public/matches/fixture-a')return reply({fixture:first,markets:quotes.slice(0,500),expert_signals:[],next_market_cursor:'fixture-next-500'});
      if(name==='/public/matches/fixture-b')return reply({fixture:second,markets:otherQuotes,expert_signals:[],next_market_cursor:null});
      if(name==='/public/matches/fixture-a/markets'){
        const cursor=url.searchParams.get('cursor');calls.push({cursor,mode});
        if(mode==='race'){
          assert.equal(cursor,'fixture-next-500');
          await new Promise(resolve=>{releaseDelayed=resolve;delayedStarted();});
          return reply({markets:quotes.slice(500,1000),next_market_cursor:'fixture-next-1000'});
        }
        if(cursor==='fixture-next-500'){
          if(failNext){failNext=false;return reply({detail:'隔离样本首次分页暂不可用'},503);}
          return reply({markets:quotes.slice(500,1000),next_market_cursor:'fixture-next-1000'});
        }
        assert.equal(cursor,'fixture-next-1000');
        // One overlap models an already-seen quote; the UI must not append it twice.
        return reply({markets:[quotes[999],...quotes.slice(1000)],next_market_cursor:null});
      }
      unknown.push(name);return reply({detail:'分页样本未配置此接口'},404);
    });
    const rows=()=>page.locator('#content-dialog tbody tr[data-quote]');
    const ids=()=>rows().evaluateAll(items=>items.map(item=>item.dataset.quote));
    const assertFilter=async count=>{
      assert.equal(await page.locator('#market-period').inputValue(),'first_half');
      assert.equal(await page.locator('#market-scope').inputValue(),'additional');
      const visible=await page.locator('#content-dialog tbody tr[data-quote]:visible').evaluateAll(items=>items.map(item=>item.dataset.quote));
      const expected=quotes.slice(0,count).filter(item=>item.period==='first_half'&&!item.is_main).map(item=>item.quote_id);
      assert.deepEqual(visible.sort(),expected.sort());
    };
    await page.goto('http://127.0.0.1:8080/#scores');
    await page.locator('.match-row[data-fixture="fixture-a"] [data-match]').first().click();
    await page.waitForFunction(()=>document.querySelectorAll('#content-dialog tbody tr[data-quote]').length===500);
    assert.equal(await page.locator('#match-markets-more').count(),1);
    await page.locator('#market-period').selectOption('first_half');
    await page.locator('#market-scope').selectOption('additional');await assertFilter(500);
    const initial=await ids();
    await page.locator('#match-markets-more').click();
    await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('保留已加载盘口，可重试'));
    await page.waitForFunction(()=>!document.querySelector('#match-markets-more').disabled);
    assert.deepEqual(await ids(),initial);await assertFilter(500);
    await page.locator('#match-markets-more').click();
    await page.waitForFunction(()=>document.querySelectorAll('#content-dialog tbody tr[data-quote]').length===1000);
    await assertFilter(1000);
    await page.locator('#match-markets-more').click();
    await page.waitForFunction(()=>document.querySelectorAll('#content-dialog tbody tr[data-quote]').length===1207);
    assert.equal(await page.locator('#match-markets-more').count(),0);await assertFilter(1207);
    const final=await ids();assert.equal(new Set(final).size,1207);
    assert.deepEqual(final.slice().sort(),quotes.map(item=>item.quote_id).sort());
    assert.deepEqual(calls.map(item=>item.cursor),['fixture-next-500','fixture-next-500','fixture-next-1000']);
    await page.locator('#market-period').selectOption('all');await page.locator('#market-scope').selectOption('all');
    assert.equal(await page.locator('#content-dialog tbody tr[data-quote]:visible').count(),1207);
    await page.locator('#content-dialog').evaluate(element=>element.scrollTop=0);
    const paginationPng=await page.screenshot({animations:'disabled'});
    const paginationFile='match-markets-'+width+'-complete.png';await fs.writeFile(output+'/'+paginationFile,paginationPng);

    // Start another A page request, navigate by actual controls, then release its old response.
    await page.locator('#content-dialog [data-close-dialog]').click();
    await page.waitForFunction(()=>!document.querySelector('#content-dialog').open&&!location.hash.includes('match='));
    await page.locator('.match-row[data-fixture="fixture-a"] [data-match]').first().click();
    await page.waitForFunction(()=>document.querySelectorAll('#content-dialog tbody tr[data-quote]').length===500);
    mode='race';const started=new Promise(resolve=>{delayedStarted=resolve;});
    await page.locator('#match-markets-more').click();await started;
    await page.locator('#content-dialog [data-close-dialog]').click();
    await page.waitForFunction(()=>!document.querySelector('#content-dialog').open&&!location.hash.includes('match='));
    await page.locator('.match-row[data-fixture="fixture-b"] [data-match]').first().click();
    await page.waitForFunction(()=>document.querySelector('#dialog-title').textContent==='隔离主队B vs 隔离客队B');
    assert.deepEqual((await ids()).sort(),otherQuotes.map(item=>item.quote_id).sort());
    const late=page.waitForResponse(response=>response.url().includes('/fixture-a/markets?'));
    releaseDelayed();await late;await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('#dialog-title').textContent(),'隔离主队B vs 隔离客队B');
    assert.deepEqual((await ids()).sort(),otherQuotes.map(item=>item.quote_id).sort());
    assert.equal(await page.locator('#match-markets-more').count(),0);
    assert.deepEqual(unknown,[]);
    const racePng=await page.screenshot({animations:'disabled'}),raceFile='match-markets-'+width+'-race-isolated.png';
    await fs.writeFile(output+'/'+raceFile,racePng);
    results.push({width,status:'passed',initial_unique_quotes:500,final_unique_quotes:1207,replayed_overlap:1,
      normal_requests:calls.filter(item=>item.mode==='normal').map(item=>item.cursor),late_response_requests:1,
      checks:['first_500_next_button','503_keeps_rows_and_retry','500_500_207_unique_complete','overlap_deduplicated',
        'period_scope_preserved','all_filters_show_complete_set','terminal_cursor_hides_button','late_response_isolated_from_other_fixture'],
      screenshots:[{file:paginationFile,sha256:digest(paginationPng)},{file:raceFile,sha256:digest(racePng)}]});
    console.log('PASS synthetic match market pagination '+width+' first500/503/retry/1207/filter/late-response');
    await context.close();
  }
  assert.deepEqual(errors,[]);complete=true;
}finally{
  await browser.close();await new Promise(resolve=>server.close(resolve));
  await fs.writeFile(output+'/results.json',JSON.stringify({scope:'match_market_pagination',synthetic_data:true,
    production_evidence:false,real_browser:true,browser_version:browser.version(),scheduled_refresh_frozen:true,
    status:complete?'passed':'failed',results,errors},null,2)+'\n');
}
