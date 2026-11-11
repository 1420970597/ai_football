// Real read-only API and native browser; do not inject responses or mutate application state.
import {chromium} from '/opt/verification/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
const browser=await chromium.launch({headless:true,args:['--disable-dev-shm-usage']});
const results=[];
const target=(process.env.SCORE_ANCHOR_BASE_URL||'http://web').replace(/\/$/,'');
const output=process.env.SCORE_ANCHOR_RESULTS||'/evidence';
await fs.mkdir(output,{recursive:true});
try{
 for(const width of [1440,390]){
  const context=await browser.newContext({viewport:{width,height:960}});
  const page=await context.newPage();const summaryResponses=[],writes=[],errors=[];
  page.on('request',r=>{if(['POST','PUT','PATCH','DELETE'].includes(r.method()))writes.push(new URL(r.url()).pathname);});
  page.on('pageerror',e=>errors.push(e.name));
  page.on('response',r=>{if(new URL(r.url()).pathname==='/api/v1/public/match-summaries')summaryResponses.push({status:r.status()});});
  const facts=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/v1/public/matches');
  // scope=all is the path this test covers: rows start with a placeholder and are
  // filled by /match-summaries. The default scope=recommended draws the odds cell
  // from expert signals and never requests summaries, so it cannot show this race.
  const start=Date.now();await page.goto(target+'/#scores?scope=all',{waitUntil:'domcontentloaded'});
  assert.equal((await facts).status(),200);
  await page.locator('#matches-list .match-row').first().waitFor();
  const ids=await page.locator('#matches-list .match-row').evaluateAll(rs=>rs.map(r=>r.dataset.fixture));
  const first=page.locator('#matches-list .team-home').first();await first.focus();
  await page.evaluate(()=>scrollTo(0,350));const scrollBefore=await page.evaluate(()=>scrollY);
  const anchor=await page.locator('#matches-list .match-row').evaluateAll(rows=>{const row=rows.find(r=>r.getBoundingClientRect().top>=0&&r.getBoundingClientRect().bottom<=innerHeight);if(!row)throw Error('no_visible_anchor');return {id:row.dataset.fixture,top:row.getBoundingClientRect().top};});
  const focusedFixture=await first.getAttribute('data-match');const factsMs=Date.now()-start;
  await page.waitForFunction(()=>[...document.querySelectorAll('#matches-list .match-odds')].every(e=>!e.textContent.includes('盘口加载中')),{},{timeout:30000});
  assert.deepEqual(await page.locator('#matches-list .match-row').evaluateAll(rs=>rs.map(r=>r.dataset.fixture)),ids);
  assert.equal(await page.evaluate(()=>document.activeElement?.getAttribute('data-match')),focusedFixture);
  const scrollAfter=await page.evaluate(()=>scrollY);
  const anchorAfter=await page.locator('#matches-list .match-row').evaluateAll((rows,id)=>rows.find(r=>r.dataset.fixture===id)?.getBoundingClientRect().top,anchor.id);
  assert.ok(Number.isFinite(anchorAfter),'visible_fixture_removed');
  assert.ok(Math.abs(anchorAfter-anchor.top)<=2,'visible_fixture_shifted_'+JSON.stringify({before:anchor.top,after:anchorAfter,scrollBefore,scrollAfter}));
  assert.ok(summaryResponses.length>0,'no_summary_http_response');
  assert.ok(summaryResponses.every(r=>r.status===200),'summary_http_failure');
  assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
  assert.equal(await page.locator('body').evaluate(e=>e.scrollWidth>innerWidth),false);
  await page.screenshot({path:output+'/settled-summaries-'+width+'.png'});
  results.push({width,passed:true,rows:ids.length,facts_ms:factsMs,summaries_settled_ms:Date.now()-start,summary_responses:summaryResponses,pending_rows:0,focus_preserved:true,visible_fixture_anchor_preserved:true,scroll_before:scrollBefore,scroll_after:scrollAfter,anchor_top_before:anchor.top,anchor_top_after:anchorAfter});
  await context.close();
 }
}catch(e){results.push({passed:false,error_category:e.name,reason:e.message?.slice(0,350),stack:e.stack?.slice(0,1100)});process.exitCode=1;}
finally{await fs.writeFile(output+'/results.json',JSON.stringify({results,response_injection:false,production_writes:0},null,2));await browser.close();}
console.log(JSON.stringify(results));
