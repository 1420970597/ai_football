import {chromium} from '/opt/verification/node_modules/playwright/index.mjs';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import {fixtureRoute,instant} from './visual-fixture.mjs';
import {comparePixels,decodePng} from './visual-pixels.mjs';
const root='/work/web/app',baseline='/work/tests/browser/visual-baselines',output=process.env.VISUAL_RESULTS||'/evidence';
const update=process.argv.includes('--update');
if(update&&process.env.VISUAL_BASELINE_UPDATE!=='reviewed')throw Error('explicit reviewed baseline update required');
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
const fixtureSha=hash(await fs.readFile('/work/tests/browser/visual-fixture.mjs'));
const policy={format:'football-visual-baseline-v1',fixture_sha256:fixtureSha,browser_version:null,locale:'zh-CN',
  timezone:'Asia/Shanghai',instant,device_scale_factor:1,channel_threshold:16,max_changed_ratio:.002,
  synthetic_data:true,production_evidence:false,source_site_parity:false};
const server=http.createServer(async(req,res)=>{
  try{
    const url=new URL(req.url,'http://localhost'),target=path.resolve(root,url.pathname.startsWith('/assets/')?url.pathname.slice(8):'index.html');
    if(!target.startsWith(root+'/'))throw Error('invalid local path');
    const raw=await fs.readFile(target);
    res.writeHead(200,{'Content-Type':target.endsWith('.js')?'text/javascript':target.endsWith('.css')?'text/css':target.endsWith('.png')?'image/png':'text/html'}).end(raw);
  }catch{res.writeHead(404).end();}
});
await fs.mkdir(output,{recursive:true});
await new Promise(resolve=>server.listen(8080,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,args:['--disable-dev-shm-usage']});
policy.browser_version=browser.version();
const entries=[],errors=[];let expected,completed=false;
try{
  if(!update){expected=JSON.parse(await fs.readFile(baseline+'/manifest.json'));for(const [key,value] of Object.entries(policy))assert.deepEqual(expected[key],value,'baseline policy changed: '+key);}
  for(const width of [1440,390]){
    const height=width===390?844:960;
    const context=await browser.newContext({viewport:{width,height},locale:policy.locale,timezoneId:policy.timezone,deviceScaleFactor:1,isMobile:width===390,reducedMotion:'reduce',colorScheme:'light'});
    await context.addInitScript(({instant})=>{const NativeDate=Date;class FixedDate extends NativeDate{constructor(...args){super(...(args.length?args:[instant]));}static now(){return new NativeDate(instant).getTime();}}window.Date=FixedDate;},{instant});
    const page=await context.newPage();page.on('pageerror',error=>errors.push(error.message));
    await page.route('**/api/v1/**',fixtureRoute);
    const screens=[['scores','#scores'],['schedule','#scores?phase=scheduled'],['experts','#experts'],['membership','#membership'],['account-guest','#account'],['admin-guest','#admin'],['match-detail','#scores?match=visual-match-0']];
    for(const [name,route] of screens){
      await page.goto('http://127.0.0.1:8080/'+route);await page.waitForLoadState('networkidle');
      if(name==='scores'||name==='schedule')await page.locator('.match-row').first().waitFor({timeout:10000}).catch(async error=>{
        console.log('fixture empty scores: '+await page.locator('#matches-state').textContent());throw error;
      });
      if(name==='experts')await page.locator('.expert-row').first().waitFor();
      if(name==='membership')await page.locator('.plan-card').first().waitFor();
      if(name==='match-detail')await page.locator('.market-table').first().waitFor();
      await page.evaluate(()=>document.fonts.ready);
      await page.addStyleTag({content:'*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}'});
      if(process.env.VISUAL_PROBE_MUTATION==='header'&&name==='scores')await page.addStyleTag({content:'.site-header{background:#ff0000!important}'});
      await page.evaluate(()=>window.scrollTo(0,0));
      const key=name+'-'+width+'.png',raw=await page.screenshot({animations:'disabled',fullPage:false});
      const row={name,viewport:{width,height},file:key,sha256:hash(raw)};
      if(update){await fs.mkdir(baseline,{recursive:true});await fs.writeFile(baseline+'/'+key,raw);row.status='baseline_written';}
      else{
        const stored=expected.screens.find(item=>item.file===key);assert.ok(stored,'missing baseline screen '+key);
        const previous=await fs.readFile(baseline+'/'+key);assert.equal(hash(previous),stored.sha256,'baseline image hash mismatch');
        row.diff=comparePixels(decodePng(previous),decodePng(raw));row.status=row.diff.passed?'passed':'failed';
        await fs.writeFile(output+'/'+key,raw);
      }
      entries.push(row);
    }
    await context.close();
  }
  assert.deepEqual(errors,[],'browser errors');
  if(update)await fs.writeFile(baseline+'/manifest.json',JSON.stringify({...policy,screens:entries},null,2)+'\n');
  else assert.equal(expected.screens.length,entries.length,'baseline screen list changed');
  completed=true;
}finally{
  await browser.close();await new Promise(resolve=>server.close(resolve));
  const passed=completed&&entries.length===14&&!errors.length&&entries.every(item=>item.status!=='failed');
  await fs.writeFile(output+'/visual-results.json',JSON.stringify({...policy,status:passed?'passed':'failed',mode:update?'baseline_update':'compare',errors,screens:entries},null,2)+'\n');
}
if(entries.some(item=>item.status==='failed'))throw Error('visual differences exceed reviewed threshold');
console.log('PASS frozen fixture screenshot '+(update?'baseline':'comparison')+' '+entries.length+' screens');
