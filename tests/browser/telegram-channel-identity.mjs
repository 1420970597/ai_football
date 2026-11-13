// Real browser against isolated API fixtures; never contacts Telegram or production.
import {chromium} from '/opt/verification/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import {fixtureRoute} from './visual-fixture.mjs';

const evidence=process.env.BROWSER_EVIDENCE_DIR||'/evidence';
await fs.mkdir(evidence,{recursive:true});
const server=http.createServer(async(req,res)=>{
  try{
    const pathname=new URL(req.url,'http://localhost').pathname;
    const target=path.resolve('/work/web/app',pathname.startsWith('/assets/')?pathname.slice(8):'index.html');
    if(!target.startsWith('/work/web/app/'))throw Error('Invalid path');
    const body=await fs.readFile(target);
    res.writeHead(200,{'Content-Type':target.endsWith('.js')?'text/javascript':target.endsWith('.css')?'text/css':'text/html'}).end(body);
  }catch{res.writeHead(404).end();}
});
await new Promise(resolve=>server.listen(8080,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,args:['--disable-dev-shm-usage']});
const results=[],errors=[];
try{
  for(const width of [1440,390]){
    const context=await browser.newContext({viewport:{width,height:960},locale:'zh-CN'});
    const page=await context.newPage(),writes=[];
    let trusted=false;
    page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/api/v1/**',async route=>{
      const request=route.request(),p=new URL(request.url()).pathname.replace('/api/v1','');
      if(request.method()!=='GET')writes.push({path:p,body:request.postDataJSON()});
      const reply=json=>route.fulfill({json});
      if(p==='/admin/me')return reply({user:{id:'isolated-admin',role:'admin',display_name:'隔离管理员',email:'admin@test.invalid'}});
      if(p==='/admin/sources')return reply({items:[{id:'channel-source',platform_chat_id:'-1001234567',display_name:'隔离频道',enabled:true,allow_media:true}]});
      if(p==='/admin/sources/channel-source/experts'){
        if(request.method()==='POST'){trusted=true;return route.fulfill({status:201,json:{id:'channel-expert'}});}
        return reply({items:trusted?[{id:'channel-expert',platform_sender_id:'-1001234567',display_nickname:'频道发布身份',enabled:true}]:[]});
      }
      if(p==='/admin/telegram/admins'||p==='/admin/telegram/members')return reply({state:'available',items:[{
        sender_id:'-1001234567',nickname:'频道发布身份',admin_kind:'channel',sender_identity_kind:'chat_sender',
        individual_author_resolved:false}],next_cursor:null,trust_automatically_granted:false});
      if(p==='/admin/status')return reply({database:'healthy',telegram_sources:1});
      if(p==='/admin/settings')return reply({items:{},leyu_version:1});
      if(p==='/admin/membership/plans'||p==='/admin/users'||p==='/admin/accounts/orders')return reply({items:[]});
      if(p==='/admin/telegram/status')return reply({connected:true,authorized:true,auth:{state:'authorized'}});
      if(p==='/admin/model-configuration')return reply({revision:1,candidates:[]});
      return fixtureRoute(route);
    });
    await page.goto('http://127.0.0.1:8080/#admin?asadmin=1');
    await page.locator('[data-admin-tab="telegram"]').click();
    const option=page.locator('#source-admins [data-author="-1001234567"]');
    await option.waitFor();
    assert.match(await page.locator('#source-admins').innerText(),/频道身份（无个人ID）/);
    assert.match(await page.locator('#source-admins').innerText(),/不代表具体匿名个人/);
    assert.equal(writes.length,0,'Directory inspection must not grant trust');
    await option.click();
    assert.equal(await page.locator('#expert-source-form [name="sender_id"]').inputValue(),'-1001234567');
    await page.locator('#expert-source-form button').click();
    await page.locator('#trusted-experts [data-untrust="channel-expert"]').waitFor();
    assert.equal(writes.length,1);
    assert.equal(writes[0].body.sender_id,'-1001234567');
    assert.equal(writes[0].path,'/admin/sources/channel-source/experts');
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
    assert.equal(overflow,false);
    await page.locator('#source-admins').screenshot({path:path.join(evidence,`channel-identity-${width}.png`)});
    results.push({width,passed:true,explicit_trust_writes:writes.length,negative_identity_preserved:true,overflow});
    await context.close();
  }
  assert.deepEqual(errors,[]);
}finally{
  await fs.writeFile(path.join(evidence,'telegram-channel-identity.json'),JSON.stringify({results,errors,production_mutations:0},null,2));
  await browser.close();await new Promise(resolve=>server.close(resolve));
}
