// Real UI + API + temporary PG. Synthetic account worker only, no provider or sends.
import { chromium } from "/opt/verification/node_modules/playwright/index.mjs";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
const origin = "http://web";
const email = "extended@web-integration.invalid";
const password = "synthetic-browser-user-password-1234";
const results = [], httpEvidence = [], errors = [];
const browser = await chromium.launch({ headless: true, args: ["--disable-dev-shm-usage"] });
const context = await browser.newContext({ viewport: { width: 1440, height: 960 } });
const page = await context.newPage();
page.setDefaultTimeout(15000);
page.on("pageerror", error => errors.push(error.message));
page.on("response", response => {
  const path = new URL(response.url()).pathname;
  if (path.startsWith("/api/")) httpEvidence.push({ method: response.request().method(), path, status: response.status() });
});
async function api(path, {method="GET", body, admin=false}={}) {
  const cookies = await context.cookies(), csrf = cookies.find(cookie => cookie.name === (admin ? "v2_admin_csrf" : "v2_csrf"))?.value;
  const r = await page.request.fetch(origin + "/api/v1" + path, {method, data:body,
    headers:{"X-Session-Context":admin?"admin":"user", ...(csrf?{"X-CSRF-Token":csrf}:{})}});
  return {status:r.status(), data:await r.json()};
}
async function scenario(name, action) {
  try { await action(); results.push({name,status:"passed"}); console.log("PASS " + name); }
  catch(error) { results.push({name,status:"failed",error:error.message}); console.log("FAIL " + name + " " + error.message); throw error; }
  finally { await page.waitForTimeout(1200); }
}
async function expanded(locator) {
  await locator.evaluate(element => { let parent = element.closest("details"); while(parent) { parent.open=true; parent=parent.parentElement?.closest("details"); } });
}
async function responseAfter(path, method, action) {
  const waiting = page.waitForResponse(response => new URL(response.url()).pathname.endsWith(path) && response.request().method() === method);
  const [response] = await Promise.all([waiting, action()]);
  return {status:response.status(),data:await response.json(),request:method==='GET'?null:response.request().postDataJSON()};
}
async function refreshAccount() {
  const wait = page.waitForResponse(response => response.url().endsWith("/accounts/leyu") && response.request().method()==="GET");
  await page.locator("#refresh-account").click(); await wait;
}
async function holdNextGet(path) {
  let release, started, handled, held = false;
  const ready = new Promise(resolve => { started = resolve; });
  const gate = new Promise(resolve => { release = resolve; });
  const finished = new Promise(resolve => { handled = resolve; });
  const pattern = origin + "/api/v1" + path;
  const handler = async route => {
    if (route.request().method() !== "GET" || held) return route.continue();
    held = true;
    try {
      const snapshot = await route.fetch();
      started();
      await gate;
      await route.fulfill({response:snapshot,headers:{...snapshot.headers(),"x-test-delayed-snapshot":path}});
    } finally { handled(); }
  };
  await page.route(pattern, handler);
  return {
    waitUntilHeld: () => Promise.race([
      ready,
      page.waitForTimeout(15000).then(() => { throw new Error("delayed GET did not start: " + path); }),
    ]),
    async releaseAndWait() {
      const response = page.waitForResponse(response => response.headers()["x-test-delayed-snapshot"] === path);
      release();
      await (await response).finished();
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    },
    async cleanup() {
      release();
      if (held) await finished;
      await page.unroute(pattern, handler);
    },
  };
}
async function pollAccount(check) {
  for(let i=0;i<40;i++) { const value = await api("/accounts/leyu"); if(check(value.data)) return value.data; await page.waitForTimeout(250); }
  throw new Error("synthetic account did not reach expected state");
}
async function configure({stake="2",single="3",daily="10",exposure="5",enabled=true}={}) {
  await expanded(page.locator("#betting-form"));
  await page.locator("#betting-form [name=enabled]").setChecked(enabled);
  if(enabled) {
    for(const [name,value] of Object.entries({stake_per_order:stake,max_per_order:single,daily_limit:daily,open_exposure_limit:exposure}))
      await page.locator('#betting-form [name='+name+']').fill(value);
    await page.locator("#betting-form [name=daily_timezone]").selectOption("Europe/Berlin");
    await page.locator("#betting-form [name=expert]").first().check();
    await page.locator("#betting-form [name=confirmed]").check();
  }
  return responseAfter("/accounts/leyu/auto-betting","PUT",()=>page.locator("#betting-form button.primary-button").click());
}
try {
  await scenario("真实UI注册普通用户与独立管理员登录", async()=>{
    await page.goto(origin); await page.waitForSelector(".match-row");
    await page.locator("#account-entry").click(); await page.locator("[data-auth-tab=register]").click();
    await page.locator("#auth-form [name=email]").fill(email);
    await page.locator("#auth-form [name=display_name]").fill("成功路径隔离用户");
    await page.locator("#auth-form [name=password]").fill(password);
    await page.locator("#auth-form button").click(); await page.waitForSelector("#account-content:not([hidden])");
    assert.equal((await api("/auth/me")).data.user.role,"user");
    await page.locator(".utility-bar [data-page=admin]").click();
    await page.locator("#admin-identity [data-login=admin]").click();
    await page.locator("#auth-form [name=email]").fill("admin@web-integration.invalid");
    await page.locator("#auth-form [name=password]").fill("test-only-admin-password-1234");
    assert.equal(await page.locator("#auth-form [name=totp_code]").count(),0);
    await page.locator("#auth-form button").click(); await page.waitForSelector("#admin-content:not([hidden])");
    assert.equal((await api("/auth/me")).data.user.role,"user");
    assert.equal((await api("/admin/me",{admin:true})).data.user.role,"admin");
  });
  await scenario("管理员来源新增信任作者停用启用与撤销点击",async()=>{
    await page.locator("[data-admin-tab=telegram]").click();
    await expanded(page.locator("#source-form"));
    await page.locator("#source-form [name=chat_id]").fill("-1008888888");
    await page.locator("#source-form [name=display_name]").fill("成功路径隔离来源");
    const added=await responseAfter("/admin/sources","POST",()=>page.locator("#source-form button").click());
    assert.equal(added.status,201);
    await page.locator("#source-select").selectOption(added.data.id);
    await page.locator("#expert-source-form [name=sender_id]").fill("88888888");
    await page.locator("#expert-source-form [name=nickname]").fill("隔离稳定作者");
    const author=await responseAfter('/sources/'+added.data.id+'/experts',"POST",()=>page.locator("#expert-source-form button").click());
    assert.equal(author.status,201);
    await page.waitForSelector('[data-untrust="'+author.data.id+'"]');
    let changed=await responseAfter('/sources/'+added.data.id,"PUT",()=>page.locator("#source-toggle").click());
    assert.equal(changed.status,200);
    await page.waitForFunction(()=>document.querySelector("#source-toggle").textContent.includes("启用"));
    changed=await responseAfter('/sources/'+added.data.id,"PUT",()=>page.locator("#source-toggle").click());
    assert.equal(changed.status,200);
    await page.waitForSelector('[data-untrust="'+author.data.id+'"]');
    const removed=await responseAfter('/sources/'+added.data.id+'/experts/'+author.data.id,"DELETE",async()=>{
      await page.locator('[data-untrust="'+author.data.id+'"]').click(); await page.locator("#confirm-yes").click();
    });
    assert.equal(removed.status,200);
    await page.waitForFunction(id=>!document.querySelector('[data-untrust="'+id+'"]'),author.data.id);
    const sources=await api("/admin/sources",{admin:true});
    assert.equal(sources.data.items.find(source=>source.id===added.data.id).enabled,true);
    const trust=(await api('/admin/sources/'+added.data.id+'/experts',{admin:true})).data.items;
    assert.equal(trust.length,1); assert.equal(trust[0].enabled,false);
  });
  await scenario("管理员套餐自动投注权益及本人会员授予点击",async()=>{
    await page.locator("[data-admin-tab=membership]").click();
    const plan=page.locator('.plan-form[data-code="webvip"]');
    const delayed = await holdNextGet("/admin/membership/plans");
    try {
      await page.locator(".utility-bar [data-page=admin]").click();
      await delayed.waitUntilHeld();
      assert.equal(await page.locator("#admin-plans").getAttribute("aria-busy"),"true");
      assert.equal(await plan.locator('[name=automatic_betting]').isDisabled(),true);
      await responseAfter('/admin/membership/plans',"GET",()=>page.locator(".utility-bar [data-page=admin]").click());
      await expanded(plan);
      await plan.locator('[name=automatic_betting]').check();
      await delayed.releaseAndWait();
      assert.equal(await plan.locator('[name=automatic_betting]').isChecked(),true);
    } finally { await delayed.cleanup(); }
    const saved=await responseAfter('/membership/plans/webvip',"PUT",()=>plan.locator("button").click()); assert.equal(saved.status,200);
    assert.equal(saved.request.entitlements.automatic_betting,true);
    const plans = await api("/admin/membership/plans",{admin:true});
    assert.equal(plans.data.items.find(item=>item.code==="webvip").entitlements.automatic_betting,true);
    await page.locator("#grant-form [name=email]").fill(email);
    await page.locator("#grant-form [name=plan_code]").selectOption("webvip");
    await page.locator("#grant-form [name=days]").fill("7");
    await page.locator("#grant-form [name=reason]").fill("隔离成功路径会员许可");
    const granted=await responseAfter('/membership/grants',"POST",()=>page.locator("#grant-form button").click()); assert.equal(granted.status,201);
    const me=await api("/auth/me"); assert.equal(me.data.membership.entitlements.automatic_betting,true);
  });
  await scenario("本人绑定替身账户与异步真实内部任务达到健康",async()=>{
    await page.locator("#account-entry").click(); await page.waitForSelector("#account-content:not([hidden])");
    await page.locator("#leyu-form [name=account_label]").fill("本人合成账户");
    await page.locator("#leyu-form [name=username]").fill("synthetic-owner-only");
    await page.locator("#leyu-form [name=password]").fill("synthetic-no-real-login");
    const bound=await responseAfter('/accounts/leyu',"PUT",()=>page.locator("#leyu-form button.primary-button").click()); assert.equal(bound.status,200);
    const account=await pollAccount(value=>value.account?.health_state==="healthy");
    assert.equal(Number(account.account.available_balance),123.45);
    assert.equal(account.execution_capability.live_enabled,true);
    assert.ok(account.tasks.some(task=>task.kind==="verify"&&task.state==="succeeded"));
    await refreshAccount(); await page.waitForFunction(()=>document.querySelector("#account-health").textContent.includes("已通过校验"));
    assert.equal(await page.locator("#leyu-form [name=username]").inputValue(),"");
    assert.equal(await page.locator("#leyu-form [name=password]").inputValue(),"");
  });
  await scenario("本人限额错误拒绝并保存专家限额与正常启停",async()=>{
    assert.equal((await configure({stake:"4",single:"3"})).status,422);
    assert.equal((await api("/accounts/leyu")).data.betting.auto_enabled,false);
    assert.equal((await configure()).status,200);
    let value=await api("/accounts/leyu"); assert.equal(value.data.betting.auto_enabled,true);
    assert.equal(value.data.betting.daily_timezone,"Europe/Berlin"); assert.equal(value.data.betting.allowed_experts.length,1);
    assert.equal(Number(value.data.betting.stake_per_order),2);
    assert.equal((await configure({enabled:false})).status,200);
    value=await api("/accounts/leyu"); assert.equal(value.data.betting.auto_enabled,false);
  });
  await scenario("过期历史重验时间不阻断本人配置与紧急停止",async()=>{
    const aged=await page.request.post('http://account-fixture:8998/age-user-session'); assert.equal(aged.status(),200);
    assert.equal((await configure()).status,200);
    assert.equal(await page.locator("#reauth-dialog").count(),0);
    const stopped=await responseAfter('/leyu/emergency-stop',"POST",()=>page.locator("#emergency-stop").click()); assert.equal(stopped.status,200);
    const value=await api("/accounts/leyu"); assert.equal(value.data.betting.auto_enabled,false); assert.equal(value.data.betting.emergency_stopped,true);
  });
  await scenario("本人合成成交单刷新核对官方形状回执与订单展示",async()=>{
    const seed=await page.request.post('http://account-fixture:8998/seed-receipts'); assert.equal(seed.status(),200);
    const verification=await responseAfter('/leyu/verify',"POST",()=>page.locator("#verify-account").click()); assert.equal(verification.status,202);
    for(let i=0;i<40;i++) {const value=await api("/accounts/orders"); if(value.data.items.some(order=>order.state==="settled")) break; await page.waitForTimeout(250);}
    await expanded(page.locator("#refresh-orders"));
    const orders=await responseAfter('/accounts/orders',"GET",()=>page.locator("#refresh-orders").click());
    const order=orders.data.items.find(item=>item.provider_order_no==="SYNTHETIC-BROWSER-RECEIPT");
    assert.equal(order.state,"settled"); assert.equal(Number(order.provider_fees),0.1); assert.equal(Number(order.settled_net),1.82);
    await page.waitForFunction(()=>document.querySelector("#orders-list").textContent.includes("SYNTHETIC-BROWSER-RECEIPT"));
    assert.match(await page.locator("#orders-list").innerText(),/已结算/);
    assert.match(await page.locator("#orders-list").innerText(),/1\.82/);
    await page.screenshot({path:"/work/reports/jiebao-score-reference/images/integration-account-synthetic-success.png"});
    const other=await browser.newContext(); const r=await other.request.get(origin+'/api/v1/accounts/orders'); assert.equal(r.status(),401); await other.close();
  });
  await scenario("管理员撤销会员立即阻断启用且本人停止仍可用",async()=>{
    await page.locator(".utility-bar [data-page=admin]").click(); await page.locator("[data-admin-tab=membership]").click();
    await expanded(page.locator("#user-search-form")); await page.locator("#user-search-form input").fill(email); await page.locator("#user-search-form button").click();
    await page.waitForSelector("[data-user-memberships]"); await page.locator("[data-user-memberships]").click();
    await page.waitForSelector("[data-revoke]");
    const paid=page.locator(".admin-record").filter({has:page.locator("[data-revoke]")}).filter({hasText:"隔离专家会员"}).last().locator("[data-revoke]");
    page.once("dialog",dialog=>dialog.accept("隔离测试撤销自动投注权益"));
    const revoked=page.waitForResponse(response=>response.url().includes('/membership/grants/')&&response.url().endsWith('/revoke'));
    await paid.click(); assert.equal((await revoked).status(),200);
    assert.equal((await api("/auth/me")).data.membership.entitlements.automatic_betting===true,false);
    const delayed = await holdNextGet("/accounts/leyu");
    try {
      await page.locator("#account-entry").click();
      await page.waitForSelector("#account-content:not([hidden])");
      await delayed.waitUntilHeld();
      assert.equal(await page.locator("#betting-form").getAttribute("aria-busy"),"true");
      assert.equal(await page.locator("#betting-form [name=enabled]").isDisabled(),true);
      assert.equal(await page.locator("#emergency-stop").isDisabled(),false);
      await responseAfter('/accounts/leyu',"GET",()=>page.locator("#refresh-account").click());
      await page.locator("#betting-form [name=enabled]").check();
      await page.locator("#betting-form [name=daily_limit]").fill("10");
      await delayed.releaseAndWait();
      assert.equal(await page.locator("#betting-form [name=enabled]").isChecked(),true);
      assert.equal(await page.locator("#betting-form [name=daily_limit]").inputValue(),"10");
    } finally {
      await delayed.cleanup();
    }
    assert.equal((await configure()).status,403);
    assert.equal((await responseAfter('/leyu/emergency-stop',"POST",()=>page.locator("#emergency-stop").click())).status,200);
  });
  const fixture=await (await page.request.get('http://account-fixture:8998/evidence')).json();
  assert.equal(fixture.kind,"synthetic_no_provider"); assert.equal(fixture.transaction_calls,0);
  assert.ok(fixture.verified_jobs>=1&&fixture.refresh_jobs>=1&&fixture.receipts===1); assert.deepEqual(fixture.failures,[]);
  assert.deepEqual(errors,[]);
  console.log("synthetic_executor_evidence "+JSON.stringify(fixture));
} finally {
  await fs.writeFile("/work/tests/browser/integration-account-results.json",JSON.stringify({date:new Date().toISOString(),environment:"real browser/API/PG, synthetic account worker only, no provider or order-send calls",results,http:httpEvidence,browser_errors:errors},null,2));
  await browser.close();
}
if(results.some(result=>result.status!=="passed")||errors.length) process.exit(1);
