// Read-only live messages; candidate modules served only inside this browser.
import {chromium} from '/opt/verification/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
const auth=JSON.parse(await fs.readFile('/run/browser-auth.json','utf8'));
const ids=['2b0ebdc1-e39c-4cf9-8aec-07a85191ff25','130057dc-75ca-443a-92bd-8c91c695b6ca'];
const browser=await chromium.launch({headless:true,args:['--disable-dev-shm-usage']});
const results=[];let page;
try {
 for(const width of [1440,390]) {
  const context=await browser.newContext({viewport:{width,height:960}});
  for(const name of ['app.js','ui.js'])await context.route('**/assets/'+name,async route=>route.fulfill({status:200,contentType:'text/javascript',body:await fs.readFile('/source/web/app/'+name,'utf8')}));
  await context.addInitScript(()=>{
   window.__deferClose=false;window.__deferredCloses=[];
   document.addEventListener('close',event=>{
    if(window.__deferClose&&event.target.id==='content-dialog'){
     event.stopImmediatePropagation();window.__deferredCloses.push(event.target);
    }
   },true);
  });
  page=await context.newPage();page.setDefaultTimeout(30000);
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('http://web/#admin',{waitUntil:'domcontentloaded'});
  await page.locator('#admin-identity [data-login=admin]').click();
  await page.locator('#auth-form [name=email]').fill(auth.email);
  await page.locator('#auth-form [name=password]').fill(auth.password);
  const login=page.waitForResponse(r=>r.url().endsWith('/api/v1/admin/login')&&r.request().method()==='POST');
  await page.locator('#auth-form .primary-button').click();assert.equal((await login).status(),200);
  await page.waitForSelector('#admin-content:not([hidden])');
  await page.goto('http://web/#admin?message='+ids[0]+'&asadmin=1');
  await page.locator('.system-review-result').waitFor();assert.equal(await page.locator('#message-signals .signal-card').count(),6);
  await page.evaluate(()=>{window.__deferClose=true;});
  await page.locator('#content-dialog [data-close-dialog]').click();
  await page.waitForFunction(()=>window.__deferredCloses.length===1);
  let release,started;
  const held=new Promise(resolve=>started=resolve);
  await page.route('**/api/v1/admin/telegram/messages/'+ids[1],async route=>{
   const response=await route.fetch();assert.equal(response.status(),200);
   await new Promise(resolve=>{release=resolve;started();});
   await route.fulfill({response});
  },{times:1});
  await page.goto('http://web/#admin?message='+ids[1]+'&asadmin=1');
  await held;
  await page.evaluate(()=>{
   window.__deferClose=false;
   for(const modal of window.__deferredCloses.splice(0)){
    if(!modal.open)throw Error('new detail must already be open');
    modal.dispatchEvent(new Event('close'));
   }
  });
  assert.ok(page.url().includes(ids[1]));assert.equal(await page.locator('#content-dialog').evaluate(e=>e.open),true);
  release();
  await page.locator('.system-review-result').waitFor();assert.equal(await page.locator('#message-signals .signal-card').count(),4);
  assert.match(await page.locator('.system-review-result').innerText(),/系统已接受 4 条推荐/);
  assert.equal(await page.locator('#content-dialog').evaluate(e=>e.scrollWidth>e.clientWidth),false);
  await page.keyboard.press('Tab');assert.equal(await page.locator('#content-dialog').evaluate(e=>e.contains(document.activeElement)),true);
  await page.keyboard.press('Escape');await page.waitForFunction(()=>!document.querySelector('#content-dialog').open);
  await page.waitForFunction(()=>!location.hash.includes('message='));
  await page.goto('http://web/#admin?message='+ids[0]+'&asadmin=1');
  await page.locator('.system-review-result').waitFor();assert.equal(await page.locator('#message-signals .signal-card').count(),6);
  await page.locator('#content-dialog [data-close-dialog]').click();await page.waitForFunction(()=>!location.hash.includes('message='));
  assert.deepEqual(errors,[]);
  results.push({width,status:'passed',delayed_close_after_reopen:true,accepted:[6,4,6],tab_focus_inside:true,escape_and_normal_close:true,production_configuration_or_order_writes:false});
  await context.close();
 }
}catch(e){results.push({status:'failed',reason:e.message});if(page)await page.screenshot({path:'/evidence/failure.png'});process.exitCode=1;}
finally{await fs.writeFile('/evidence/dialog-reopen.json',JSON.stringify(results,null,2));await browser.close();}
console.log(JSON.stringify(results));
