async page => {
  await page.unrouteAll({behavior:'wait'});
  const errors=[],writes=[];
  page.on('pageerror',error=>errors.push(error.message));
  await page.route('**/api/v1/**',route=>{
    if(route.request().method()!=='GET'){writes.push(route.request().method()+' '+route.request().url());return route.abort();}
    return route.continue();
  });
  const origin=new URL(page.url()).origin;
  await page.setViewportSize({width:1440,height:1000});
  const response=await page.request.get(origin+'/api/v1/data-model');
  if(response.status()!==200) throw new Error('production data-model HTTP '+response.status());
  const actual=await response.json();
  if(actual.service!=='model-worker') throw new Error('response is not the real worker dashboard');
  const loaded=page.waitForResponse(response=>response.url().includes('/api/v1/data-model')&&response.request().method()==='GET');
  await page.goto(origin+'/?production-readonly='+Date.now()+'#data-model');
  await loaded;
  await page.locator('.shadow-performance').waitFor();
  const model=await page.locator('#model-summary').textContent();
  const shadow=await page.locator('.shadow-performance').textContent();
  const storage=await page.locator('#storage-summary').textContent();
  if(/undefined|NaN/.test(model+shadow+storage)) throw new Error('undefined/NaN production dashboard');
  await page.screenshot({path:'/root/ai_football/output/playwright/data-model-production-readonly.png',fullPage:true});
  if(errors.length||writes.length) throw new Error('errors or writes '+JSON.stringify({errors,writes}));
  return {http:response.status(),service:actual.service,heartbeat:actual.heartbeat_at,model,shadow,storage,errors,writes};
}
