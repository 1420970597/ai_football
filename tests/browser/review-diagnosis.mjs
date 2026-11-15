// Isolated source contracts, no business writes or executor traffic.
import {chromium} from '/opt/verification/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import {fixtureRoute,instant,expert} from './visual-fixture.mjs';
const output=process.env.BROWSER_EVIDENCE_DIR||'/evidence';
const server=http.createServer(async(req,res)=>{
 try{const p=new URL(req.url,'http://localhost').pathname;
 const file=path.resolve('/work/web/app',p.startsWith('/assets/')?p.slice(8):'index.html');
 if(!file.startsWith('/work/web/app/'))throw Error('path');
 res.writeHead(200,{'Content-Type':file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html'}).end(await fs.readFile(file));
 }catch{res.writeHead(404).end();}});
await new Promise(resolve=>server.listen(8080,'127.0.0.1',resolve));
await fs.mkdir(output,{recursive:true});
const browser=await chromium.launch({headless:true,args:['--disable-dev-shm-usage']});
const results=[],errors=[];
const labels=['原版本已被编辑替代','已形成推荐，剩余审核超时','未形成推荐·查看分析','20秒内未形成推荐'];
try{for(const width of [1440,390]){
 const context=await browser.newContext({viewport:{width,height:960},locale:'zh-CN'});
 const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));let writes=0;
 await page.route('**/api/v1/**',async route=>{
  const req=route.request(),p=new URL(req.url()).pathname.replace('/api/v1','');
  if(req.method()!=='GET'){writes++;return route.fulfill({status:405,json:{detail:'read only'}});}
  if(p==='/auth/me')return route.fulfill({json:{user:{id:'diagnosis-user',display_name:'隔离用户',role:'user'},membership:{name:'专家会员',entitlements:{expert_full_history:true}}}});
  if(p===`/public/experts/${expert.id}/messages`)return route.fulfill({json:{items:labels.map((label,i)=>({id:'msg-'+i,display_nickname:'诊断专家',posted_at:instant,raw_text:'隔离诊断样本 '+i,revision:1,media_objects:[],analysis_state:i===0?'failed':'unresolved',review_diagnosis:{label}})),next_cursor:null}});
  return fixtureRoute(route);
 });
 await page.goto('http://127.0.0.1:8080/#messages');
 await page.locator('#message-expert').selectOption(expert.id);
 await page.waitForSelector('#messages-list .message-card');
 for(const label of labels)assert.ok((await page.locator('#messages-list').innerText()).includes(label));
 assert.equal(await page.locator('#messages-list .message-card').count(),4);
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 assert.equal(writes,0);
 await page.screenshot({path:output+'/review-diagnosis-'+width+'.png',fullPage:true});
 results.push({width,passed:true,labels:labels.length,writes});await context.close();
}assert.deepEqual(errors,[]);await fs.writeFile(output+'/browser.json',JSON.stringify(results,null,2));console.log(JSON.stringify(results));
}finally{await browser.close();server.close();}
