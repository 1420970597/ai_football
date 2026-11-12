// Isolated read-only UI contracts; no provider requests or funded actions.
import {chromium} from '/opt/verification/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import {fixtureRoute,instant} from './visual-fixture.mjs';

const output=process.env.BROWSER_EVIDENCE_DIR||'/evidence';
await fs.mkdir(output,{recursive:true});
const server=http.createServer(async(req,res)=>{
  try{
    const pathname=new URL(req.url,'http://localhost').pathname;
    const target=path.resolve('/work/web/app',pathname.startsWith('/assets/')?pathname.slice(8):'index.html');
    if(!target.startsWith('/work/web/app/'))throw Error('invalid path');
    const body=await fs.readFile(target);
    res.writeHead(200,{'Content-Type':target.endsWith('.js')?'text/javascript':target.endsWith('.css')?'text/css':'text/html'}).end(body);
  }catch{res.writeHead(404).end();}
});
await new Promise(resolve=>server.listen(8080,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,args:['--disable-dev-shm-usage']});
const results=[],errors=[];
try{
  for(const width of [1440,1030,390]){
    const context=await browser.newContext({viewport:{width,height:960},locale:'zh-CN'});
    const page=await context.newPage(),reads=[],writes=[];
    let fail=false,hold=false,releaseHeld,startHeld;
    const held=new Promise(resolve=>{startHeld=resolve;});
    page.on('pageerror',error=>errors.push(error.message));
    const match=(sport,phase='live')=>({id:sport+'-match',sport,competition_name:sport==='football'?'足球联赛':'篮球联赛',
      home_team:sport==='football'?'足球主队':'篮球主队',away_team:'客队',phase,clock_minute:33,
      // 篮球取三对三（加时赛真实比分），这是比分列要放下的最长内容；两对两放得下不代表它放得下。
      home_score:sport==='football'?1:108,away_score:sport==='football'?0:102,kickoff_at:instant,source_updated_at:instant,
      markets:[],expert_signals_has_more:false,expert_signals:[0,1].map(i=>({id:sport+'-signal-'+i,expert_id:sport+'-expert',
        display_nickname:i?'另一位专家':'本场专家',phase:'scheduled',market_id:sport==='football'?'2':'38',
        market_name:sport==='football'?'全场大小':'全场总分',market_kind:'total',period:i?'first_half':'full_time',
        line:sport==='football'?'2.5':'220.5',selection_line:sport==='football'?'2.5':'220.5',selection:i?'under':'over',
        recommendation_price:'1.95',quote_time:instant,created_at:instant,outcome:phase==='finished'?'win':'pending'}))});
    await page.route('**/api/v1/**',async route=>{
      const req=route.request(),url=new URL(req.url()),p=url.pathname.replace('/api/v1','');
      if(req.method()!=='GET')writes.push(p);
      if(p==='/public/matches'){
        const sport=url.searchParams.get('sport'),scope=url.searchParams.get('recommended_only');
        const phase=url.searchParams.get('phase')==='all'?'live':url.searchParams.get('phase');
        reads.push({p,sport,scope,phase});
        if(hold&&sport==='football'){hold=false;startHeld();await new Promise(resolve=>{releaseHeld=resolve;});}
        if(fail)return route.fulfill({status:503,json:{detail:'隔离服务暂不可用'}});
        let items=phase==='cancelled'?[]:[match(sport,phase)];
        if(scope==='false')items=items.concat({...match(sport,phase),id:'unrelated',home_team:'没有专家的比赛',expert_signals:[]});
        return route.fulfill({json:{items,next_cursor:null,markets_included:false,recommended_only:scope==='true',server_time:instant}});
      }
      if(p==='/public/match-summaries')return route.fulfill({json:{items:url.searchParams.getAll('fixture_ids').map(id=>({fixture_id:id,phase:'live',markets:[],quote_fresh:false}))}});
      if(p==='/public/signals/basketball-signal-0')return route.fulfill({status:403,json:{detail:'需要完整原文权益'}});
      if(p==='/public/matches/basketball-match')return route.fulfill({json:{fixture:match('basketball'),markets:[],expert_signals:[],server_time:instant}});
      return fixtureRoute(route);
    });
    await page.goto('http://127.0.0.1:8080/');
    await page.locator('[data-fixture="football-match"] [data-recommendation]').first().waitFor();
    assert.equal(reads[0].scope,'true');
    assert.equal(await page.locator('[data-match-scope="recommended"]').getAttribute('aria-pressed'),'true');
    assert.equal(await page.locator('.match-row').count(),1);
    assert.equal(await page.locator('.match-recommendation').count(),2);
    assert.match(await page.locator('#matches-list').innerText(),/推荐时系统赔率 1.95/);
    assert.match(await page.locator('#matches-list').innerText(),/本场专家/);
    assert.match(await page.locator('#matches-list').innerText(),/另一位专家/);
    assert.doesNotMatch(await page.locator('#matches-list').innerText(),/没有专家的比赛/);
    await page.locator('[data-match-scope="all"]').click();
    await page.locator('[data-fixture="unrelated"]').waitFor();
    assert.equal(await page.locator('.match-recommendation').count(),0);
    await page.reload();
    await page.locator('[data-fixture="unrelated"]').waitFor();
    assert.equal(await page.locator('[data-match-scope="all"]').getAttribute('aria-pressed'),'true');
    await page.locator('[data-match-scope="recommended"]').click();
    await page.locator('.match-recommendation').first().waitFor();
    await page.locator('.sports-bar [data-sport="basketball"]').click();
    await page.locator('[data-fixture="basketball-match"] [data-recommendation]').first().waitFor();
    assert.match(await page.locator('#score-title').innerText(),/篮球专家关联比赛/);
    assert.match(await page.locator('#matches-list').innerText(),/220.5/);
    assert.doesNotMatch(await page.locator('#matches-list').innerText(),/足球主队/);
    await page.locator('[data-recommendation="basketball-signal-0"] [data-signal]').click();
    await page.waitForFunction(()=>document.querySelector('#dialog-content').textContent.includes('需要完整原文权益'));
    assert.match(await page.locator('#dialog-content').innerText(),/需要完整原文权益/);
    await page.locator('#content-dialog [data-close-dialog]').click();
    await page.locator('#content-dialog').waitFor({state:'hidden'});
    await page.locator('.score-tabs [data-phase="finished"]').click();
    await page.waitForFunction(()=>document.querySelector('.match-phase')?.textContent==='完场');
    assert.match(await page.locator('.match-recommendation').first().innerText(),/全赢/);
    await page.locator('.score-tabs [data-phase="cancelled"]').click();
    await page.waitForFunction(()=>document.querySelector('#matches-state').textContent.includes('暂无符合条件的专家关联比赛'));
    assert.equal(await page.locator('.match-row').count(),0);
    await page.locator('.score-tabs [data-phase="all"]').click();
    await page.locator('.match-recommendation').first().waitFor();
    hold=true;await page.locator('.sports-bar [data-sport="football"]').click();await held;
    await page.locator('.sports-bar [data-sport="basketball"]').click();
    await page.locator('[data-fixture="basketball-match"]').waitFor();
    releaseHeld();
    await page.evaluate(()=>new Promise(resolve=>setTimeout(resolve,150)));
    assert.doesNotMatch(await page.locator('#matches-list').innerText(),/足球主队/);
    fail=true;await page.locator('#refresh-matches').click();
    await page.waitForFunction(()=>!document.querySelector('#matches-notice').hidden);
    assert.match(await page.locator('#matches-notice').innerText(),/保留上次结果/);
    fail=false;await page.locator('#refresh-matches').click();
    await page.waitForFunction(()=>document.querySelector('#matches-notice').hidden);
    await page.reload();
    await page.locator('[data-fixture="basketball-match"] [data-recommendation]').first().waitFor();
    // 先量文字有没有溢出自己的格子。boundingBox 返回的是网格轨道的边框盒，
    // 文字超出时盒子照旧待在列内、压着客队的字画出去，所以「盒子先后顺序」永远为真，
    // 看不见这类溢出。scrollWidth 比 clientWidth 才是真正能失败的那个数。
    const fit=await page.locator('.match-row[data-sport="basketball"] .score-button').evaluate(el=>{const b=el.getBoundingClientRect();return {scrollWidth:el.scrollWidth,clientWidth:el.clientWidth,track:Math.round(b.width*10)/10};});
    assert.ok(fit.scrollWidth<=fit.clientWidth+1,JSON.stringify({width,fit}));
    const home=await page.locator('.match-row .team-home').boundingBox();
    const score=await page.locator('.match-row .score-button').boundingBox();
    const away=await page.locator('.match-row .team-away').boundingBox();
    assert.ok(home.x+home.width<=score.x+1&&score.x+score.width<=away.x+1,'basketball score must not overlap either team');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.screenshot({path:output+'/homepage-expert-matches-'+width+'.png',fullPage:true});
    assert.deepEqual(writes,[]);
    results.push({width,passed:true,read_only:true,grouped_recommendations:true,sport_and_phase_isolation:true,scope_reload:true,stale_response_ignored:true});
    await context.close();
  }
  assert.deepEqual(errors,[]);
}catch(error){results.push({passed:false,name:error.name,message:error.message});process.exitCode=1;}
finally{
  await browser.close();await new Promise(resolve=>server.close(resolve));
  await fs.writeFile(output+'/homepage-expert-matches-results.json',JSON.stringify({results,errors,synthetic_data:true,production_writes:0},null,2)+'\n');
}
console.log(JSON.stringify(results));
