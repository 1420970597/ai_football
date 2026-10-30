import { chromium } from "/opt/verification/node_modules/playwright/index.mjs";
import assert from "node:assert/strict";
import fs from "node:fs/promises";

if (process.env.BROWSER_EXTENDED_FIXTURE === "true") {
  await import("../integration/browser_account_flows.mjs");
  process.exit(0);
}
const expertId = "11111111-1111-4111-8111-111111111111";
const origin = "http://web";
const evidence = "/work/reports/jiebao-score-reference/images";
const results = [];
const errors = [];
const roleAccessMatrix = [];
const leagueDirectoryEvidence = [];
const browser = await chromium.launch({
  headless: true,
  args: ["--disable-dev-shm-usage"],
});
const context = await browser.newContext({
  viewport: { width: 1440, height: 960 },
});
const page = await context.newPage();
page.on("pageerror", (error) => {
  errors.push(error.message);
  console.log("BROWSER_ERROR " + error.message);
});
const failedResponses = [];
page.on("response", (r) => {
  if (r.status() >= 500)
    failedResponses.push({
      path: new URL(r.url()).pathname,
      status: r.status(),
    });
});
async function scenario(name, action) {
  try {
    await action();
    results.push({ name, status: "passed" });
    console.log("PASS " + name);
  } catch (error) {
    results.push({ name, status: "failed", error: error.message });
    console.log("FAIL " + name + " " + error.message);
  } finally {
    // Real nginx rate limits apply to the same headless client across role contexts.
    await page.waitForTimeout(1200);
  }
}
async function api(path, { method = "GET", body, admin = false } = {}) {
  const cookies = await context.cookies();
  const csrf = cookies.find(
    (c) => c.name === (admin ? "v2_admin_csrf" : "v2_csrf"),
  )?.value;
  const response = await page.request.fetch(origin + "/api/v1" + path, {
    method,
    data: body,
    headers: {
      "X-Session-Context": admin ? "admin" : "user",
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
    },
  });
  const raw = await response.text();
  if (!(response.headers()["content-type"] || "").includes("application/json"))
    throw new Error("API returned non-JSON status=" + response.status() + " path=" + path);
  return { status: response.status(), data: JSON.parse(raw) };
}
await scenario("真实API比分投影、联赛多选和赛前赛果", async () => {
  const holdDirectory = async () => {
    let release, ready, completed;
    const fetched = new Promise(resolve => { ready = resolve; });
    const delivered = new Promise(resolve => { completed = resolve; });
    await page.route("**/api/v1/public/leagues?**", async route => {
      const response = await route.fetch();
      const body = await response.json();
      await new Promise(resolve => { release = resolve; ready({status:response.status(),items:body.items}); });
      await route.fulfill({ response });
      completed();
    }, { times: 1 });
    return { fetched, delivered, release: () => release() };
  };
  // Hold the real successful response: clicking before catalog delivery must show loading.
  const initialDirectory = await holdDirectory();
  await page.goto(origin);
  await page.waitForSelector(".match-row");
  assert.equal(await page.locator(".match-row").count(), 4);
  const initialCatalog = await initialDirectory.fetched;
  assert.equal(initialCatalog.status,200);
  assert.deepEqual(initialCatalog.items.map(item=>item.competition_id).sort(),['league-0','league-1']);
  await page.locator("#league-filter").click();
  assert.match(await page.locator('#dialog-content [role=status]').textContent(),/正在读取当前联赛目录/);
  assert.equal(await page.locator('[data-league-id]').count(),0);
  initialDirectory.release();await initialDirectory.delivered;
  await page.locator('[data-league-id="league-0"]').check();
  assert.equal(await page.locator('[data-league-id]').count(),2);
  leagueDirectoryEvidence.push({check:'early_click_waits_for_real_catalog',http_status:200,league_ids:['league-0','league-1'],status:'passed'});
  await page.locator("[data-apply-leagues]").click();
  await page.waitForFunction(
    () => document.querySelectorAll(".match-row").length === 2,
  );
  await page.locator("#league-filter").click();
  await page.locator("[data-clear-leagues]").click();
  await page.waitForFunction(
    () => document.querySelectorAll(".match-row").length === 4,
  );
  await page.locator(".score-tabs [data-phase=scheduled]").click();
  await page.waitForFunction(
    () => document.querySelectorAll(".match-row").length === 1,
  );
  assert.match(await page.locator(".match-row").innerText(), /未开赛/);
  await page.locator(".score-tabs [data-phase=finished]").click();
  await page.waitForFunction(
    () =>
      document.querySelectorAll(".match-row").length === 1 &&
      document.querySelector(".match-phase.finished"),
  );
  assert.match(await page.locator(".match-row").innerText(), /完场/);

  // A closed directory must not overwrite the different match dialog opened afterwards.
  const delayedDirectory = await holdDirectory();
  await page.locator('.score-tabs [data-phase=all]').click();
  await page.waitForFunction(()=>document.querySelectorAll('.match-row').length===4);
  assert.equal((await delayedDirectory.fetched).status,200);
  await page.locator('#league-filter').click();
  assert.match(await page.locator('#dialog-content [role=status]').textContent(),/正在读取当前联赛目录/);
  await page.locator('#content-dialog [data-close-dialog]').click();
  await page.waitForFunction(()=>!document.querySelector('#content-dialog').open);
  await page.locator('.match-row[data-fixture="web-integration-match-0"] [data-match]').first().click();
  await page.waitForFunction(()=>document.querySelector('#dialog-title').textContent==='测试主队甲 vs 测试客队甲');
  const matchTitle=await page.locator('#dialog-title').textContent();
  delayedDirectory.release();await delayedDirectory.delivered;await page.waitForLoadState('networkidle');
  assert.equal(await page.locator('#dialog-title').textContent(),matchTitle);
  assert.equal(await page.locator('#dialog-content .league-choices').count(),0);
  assert.equal(await page.locator('#dialog-content tr[data-quote]').count(),2);
  leagueDirectoryEvidence.push({check:'closed_directory_late_response_cannot_replace_match',http_status:200,status:'passed'});
  await page.locator('#content-dialog [data-close-dialog]').click();
  await page.waitForFunction(()=>!document.querySelector('#content-dialog').open&&!location.hash.includes('match='));

  // Inject a transport failure only, then retry against the actual API and current phase.
  await page.route('**/api/v1/public/leagues?**',route=>route.abort('failed'),{times:1});
  await page.locator('.score-tabs [data-phase=scheduled]').click();
  await page.waitForFunction(()=>document.querySelectorAll('.match-row').length===1);
  await page.locator('#league-filter').click();
  await page.locator('#retry-league-directory').waitFor();
  assert.equal(await page.locator('[data-league-id]').count(),0);
  const retryResponse=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/v1/public/leagues');
  await page.locator('#retry-league-directory').click();
  assert.equal((await retryResponse).status(),200);
  await page.locator('[data-league-id="league-1"]').waitFor();
  assert.equal(await page.locator('[data-league-id]').count(),1);
  leagueDirectoryEvidence.push({check:'transport_failure_explicit_retry_current_phase',fault:'synthetic_transport_failure',retry_http_status:200,league_ids:['league-1'],status:'passed'});
  await page.screenshot({path:evidence+'/integration-league-directory-retry-real-api.png'});
  await page.locator('#content-dialog [data-close-dialog]').click();
  await page.waitForFunction(()=>!document.querySelector('#content-dialog').open);
  await page.locator('.score-tabs [data-phase=all]').click();
  await page.waitForFunction(()=>document.querySelectorAll('.match-row').length===4);
});
await scenario("游客真实鉴权拒绝与用户注册会话", async () => {
  assert.equal(
    (await api("/public/experts/" + expertId + "/messages")).status,
    403,
  );
  await page.locator("#account-entry").click();
  await page.locator("[data-auth-tab=register]").click();
  await page
    .locator("#auth-form [name=email]")
    .fill("member@web-integration.invalid");
  await page.locator("#auth-form [name=display_name]").fill("联调普通用户");
  await page
    .locator("#auth-form [name=password]")
    .fill("test-only-user-password-1234");
  await page.locator("#auth-form button").click();
  await page.waitForSelector("#account-content:not([hidden])");
  const me = await api("/auth/me");
  assert.equal(me.status, 200);
  assert.equal(me.data.user.role, "user");
  assert.equal(me.data.membership.code, "free");
  assert.equal((await api("/admin/users", { admin: true })).status, 401);
});
await scenario("真实管理员仅凭主密码登录且会话保持隔离", async () => {
  await page.locator(".utility-bar [data-page=admin]").click();
  await page.locator("#admin-identity [data-login=admin]").click();
  await page
    .locator("#auth-form [name=email]")
    .fill("admin@web-integration.invalid");
  await page
    .locator("#auth-form [name=password]")
    .fill("test-only-admin-password-1234");
  assert.equal(await page.locator("#auth-form [name=totp_code]").count(), 0);
  await page.locator("#auth-form button").click();
  await page.waitForSelector("#admin-content:not([hidden])");
  assert.equal((await api("/auth/me")).data.user.role, "user");
  assert.equal(
    (await api("/admin/me", { admin: true })).data.user.role,
    "admin",
  );
  assert.equal((await api("/admin/mfa/status", { admin: true })).status, 404);
  assert.equal((await api("/admin/reauthenticate", { admin: true, method: "POST", body: {} })).status, 404);
  const noCsrf = await page.request.put(origin + "/api/v1/admin/settings", {
    data: { values: { hermes_analysis_model: "test" } },
    headers: { "X-Session-Context": "admin" },
  });
  assert.equal(noCsrf.status(), 403);
  await page.locator("[data-admin-tab=system]").click();
  await page.waitForTimeout(200);
});
await scenario("真实会员授予、搜索与幂等审计", async () => {
  await page.locator("[data-admin-tab=membership]").click();
  await page
    .locator("#grant-form [name=email]")
    .fill("member@web-integration.invalid");
  await page.locator("#grant-form [name=plan_code]").selectOption("webvip");
  await page.locator("#grant-form [name=days]").fill("30");
  await page.locator("#grant-form [name=reason]").fill("隔离浏览器联调授权");
  await page.locator("#grant-form button").click();
  await page.waitForFunction(() =>
    document.querySelector("#toast").textContent.includes("会员已授予"),
  );
  const membership = (await api("/auth/me")).data.membership;
  assert.equal(membership.code, "webvip");
  assert.equal(membership.entitlements.expert_full_history, true);
  await page
    .locator("#admin-membership details")
    .nth(1)
    .evaluate((el) => (el.open = true));
  await page
    .locator("#user-search-form input")
    .fill("member@web-integration.invalid");
  await page.locator("#user-search-form button").click();
  await page.waitForSelector("#admin-users [data-user-memberships]");
  assert.match(await page.locator("#admin-users").innerText(), /联调普通用户/);
  const audit = await api("/admin/audit", { admin: true });
  assert.equal(audit.status, 200);
  assert.ok(audit.data.items.some((a) => a.action === "membership_granted"));
});
await scenario("真实原文多买入、报价证据与未解决记录", async () => {
  await page.locator(".main-nav [data-page=experts]").click();
  await page.locator("[data-expert-tab=signals]").click();
  await page.waitForSelector("#signals-list .signal-card");
  assert.equal(await page.locator("#signals-list .signal-card").count(), 2);
  await page.locator("#signals-list [data-message]").first().click();
  await page.waitForSelector("#dialog-content .signal-card");
  assert.equal(await page.locator("#dialog-content .signal-card").count(), 2);
  assert.match(
    await page.locator("#dialog-content").innerText(),
    /系统已接受两条推荐/,
  );
  assert.equal(
    (
      await page.locator("#dialog-content .signal-evidence").allTextContents()
    ).some((text) => text.includes("历史记录未提供")),
    false,
  );
  assert.equal(await page.evaluate(() => window.__bad), undefined);
  await page.screenshot({
    path: evidence + "/integration-desktop-original-real-api-fixture.png",
  });
  await page.keyboard.press("Escape");
  await page.locator(".section-tabs [data-page=messages]").click();
  await page.locator("#message-expert").selectOption(expertId);
  await page.waitForSelector(".message-card");
  assert.equal(await page.locator(".message-card").count(), 2);
  assert.match(await page.locator("#messages-list").innerText(), /未解决/);
});
await scenario(
  "本人绑定只排队、每日时区传输、未开放拒绝及紧急停止",
  async () => {
    await page.locator("#account-entry").click();
    await page.waitForSelector("#account-content:not([hidden])");
    await page.locator("#leyu-form [name=account_label]").fill("隔离人工账户");
    await page
      .locator("#leyu-form [name=username]")
      .fill("no-real-provider-user");
    await page
      .locator("#leyu-form [name=password]")
      .fill("no-real-provider-password");
    await page
      .locator("#leyu-form button[type=submit],#leyu-form .primary-button")
      .click();
    await page.waitForFunction(() =>
      document.querySelector("#account-health").textContent.includes("未校验"),
    );
    assert.equal(
      await page.locator("#leyu-form [name=password]").inputValue(),
      "",
    );
    const account = await api("/accounts/leyu");
    assert.equal(account.data.account.health_state, "unverified");
    assert.ok(
      account.data.tasks.some(
        (t) => t.kind === "verify" && t.state === "queued",
      ),
    );
    assert.equal(account.data.execution_capability.live_enabled, false);
    assert.equal(account.data.betting.daily_timezone, "Asia/Shanghai");
    const timezone = page.locator("#betting-form [name=daily_timezone]");
    assert.equal(await timezone.inputValue(), "Asia/Shanghai");
    await page.locator("#betting-form [name=enabled]").check();
    await timezone.selectOption("Europe/Berlin");
    for (const name of [
      "stake_per_order",
      "max_per_order",
      "daily_limit",
      "open_exposure_limit",
    ])
      await page.locator(`#betting-form [name=${name}]`).fill("1");
    await page.locator("#betting-form [name=expert]").first().check();
    await page.locator("#betting-form [name=confirmed]").check();
    const responsePromise = page.waitForResponse(
      (response) =>
        response.url().endsWith("/accounts/leyu/auto-betting") &&
        response.request().method() === "PUT",
    );
    await page.locator("#betting-form button.primary-button").click();
    const denied = await responsePromise;
    assert.equal(
      denied.request().postDataJSON().daily_timezone,
      "Europe/Berlin",
    );
    assert.equal(denied.status(), 409); // Binding is deliberately unverified; no provider is called.
    assert.equal(
      (await denied.json()).detail,
      "本人乐鱼账户尚未通过无资金校验",
    );
    assert.equal(
      (await api("/accounts/leyu")).data.betting.auto_enabled,
      false,
    );
    assert.equal(
      (await api("/accounts/leyu")).data.betting.daily_timezone,
      "Asia/Shanghai",
    );
    await page.locator("#betting-form [name=enabled]").uncheck();
    await page.locator("#betting-form button.primary-button").click();
    await page.waitForFunction(() =>
      document.querySelector("#toast").textContent.includes("自动投注已关闭"),
    );
    await page.locator("#emergency-stop").click();
    await page.waitForFunction(() =>
      document.querySelector("#toast").textContent.includes("紧急停止"),
    );
    assert.equal(
      (await api("/accounts/leyu")).data.betting.auto_enabled,
      false,
    );
    assert.equal((await api("/accounts/orders")).status, 200);
  },
);
await scenario("真实管理待办、只读订单与本人敏感导出", async () => {
  await page.locator(".utility-bar [data-page=admin]").click();
  await page.locator("[data-admin-tab=overview]").click();
  await page.waitForFunction(() =>
    document
      .querySelector("#admin-diagnostics")
      .textContent.includes("模型提供方尚未验证"),
  );
  const diagnostics = await api("/admin/diagnostics", { admin: true });
  assert.equal(diagnostics.status, 200);
  assert.equal(diagnostics.data.schema, "verified");
  assert.equal(diagnostics.data.storage, "unconfigured");
  await page.locator("[data-admin-tab=orders]").click();
  await page.waitForFunction(() =>
    document
      .querySelector("#admin-orders-list")
      .textContent.includes("没有订单"),
  );
  const orders = await api("/admin/orders", { admin: true });
  assert.equal(orders.data.read_only, true);
  const userOnly = await browser.newContext();
  await userOnly.addCookies(
    (await context.cookies()).filter(
      (cookie) => !cookie.name.startsWith("v2_admin"),
    ),
  );
  const denied = await userOnly.request.get(origin + "/api/v1/admin/orders", {
    headers: { "X-Session-Context": "user" },
  });
  assert.equal(denied.status(), 401);
  await userOnly.close();
  await page.locator("#account-entry").click();
  await page
    .locator("[data-export=profile]")
    .evaluate((el) => (el.closest("details").open = true));
  await page.locator("[data-export=profile]").click();
  await page.waitForSelector("[data-export-download]");
  const exported = await api("/auth/export?section=profile");
  assert.equal(exported.status, 200);
  assert.equal(exported.data.items.length, 1);
  assert.equal(
    exported.data.items[0].profile.email,
    "member@web-integration.invalid",
  );
  assert.equal("password_hash" in exported.data.items[0].profile, false);
  assert.equal(
    "credentials_ciphertext" in exported.data.items[0].leyu_account,
    false,
  );
  const download = page.waitForEvent("download");
  await page.locator("[data-export-download]").click();
  assert.equal(
    (await download).suggestedFilename(),
    "qiuxun-personal-profile.json",
  );
  await page.keyboard.press("Escape");
});
await scenario(
  "真实人工恢复码重置、再次登录和管理员恢复码一次显示",
  async () => {
    await page.locator(".utility-bar [data-page=admin]").click();
    await page.locator("[data-admin-tab=membership]").click();
    await page
      .locator("#user-search-form")
      .evaluate((el) => (el.closest("details").open = true));
    await page
      .locator("#user-search-form input")
      .fill("member@web-integration.invalid");
    await page.locator("#user-search-form button").click();
    await page.locator("[data-user-recovery]").click();
    await page
      .locator("#issue-recovery-form [name=reason]")
      .fill("隔离浏览器人工核验测试账户");
    await page.locator("#issue-recovery-form button").click();
    await page.waitForSelector("#issued-recovery pre");
    const code = await page.locator("#issued-recovery pre").innerText();
    assert.ok(code.length >= 20);
    await page.keyboard.press("Escape");
    await page.waitForFunction(
      () => !document.querySelector("#issued-recovery"),
    );
    await page.locator("#account-entry").click();
    await page.locator("#user-logout").click();
    await page.waitForFunction(
      () => document.querySelector("#account-content").hidden,
    );
    await page.locator("#account-entry").click();
    await page
      .locator("#auth-form [name=email]")
      .fill("member@web-integration.invalid");
    await page.locator("[data-password-recovery]").click();
    await page.waitForSelector("#password-reset-form");
    assert.match(
      await page.locator("#dialog-content").innerText(),
      /未配置自动恢复发送渠道/,
    );
    await page.locator("#password-reset-form [name=code]").fill(code);
    await page
      .locator("#password-reset-form [name=new_password]")
      .fill("updated-test-user-password-1234");
    await page.locator("#password-reset-form button").click();
    await page.waitForFunction(
      () => document.querySelector("#auth-dialog").open,
    );
    await page
      .locator("#auth-form [name=email]")
      .fill("member@web-integration.invalid");
    await page
      .locator("#auth-form [name=password]")
      .fill("updated-test-user-password-1234");
    await page.locator("#auth-form button").click();
    await page.waitForSelector("#account-content:not([hidden])");
    assert.equal((await api("/auth/me")).status, 200);
    assert.equal((await api("/admin/me", { admin: true })).status, 200);
    assert.equal(
      (await api("/accounts/leyu")).data.betting.auto_enabled,
      false,
    );
  },
);
// Separate real browser contexts keep every role's cookies and personal data apart.
for (const role of [
  { key: "guest", name: "游客" },
  { key: "ordinary", name: "普通用户" },
  { key: "expired", name: "真实到期会员" },
  { key: "valid", name: "有效会员" },
  { key: "admin", name: "独立管理员" },
]) {
  await scenario("五身份页面及接口矩阵：" + role.name, async () => {
    const isolated = await browser.newContext({ viewport: { width: 1440, height: 960 } });
    const rolePage = await isolated.newPage();
    rolePage.on("pageerror", (error) => errors.push(role.key + ": " + error.message));
    const observed = { role: role.key, pages: [], http: [] };
    roleAccessMatrix.push(observed);
    async function request(path, { admin = false, method = "GET", body } = {}) {
      const cookies = await isolated.cookies();
      const csrf = cookies.find((cookie) => cookie.name === (admin ? "v2_admin_csrf" : "v2_csrf"))?.value;
      const response = await rolePage.request.fetch(origin + "/api/v1" + path, {
        method, data: body,
        headers: { "X-Session-Context": admin ? "admin" : "user", ...(csrf ? { "X-CSRF-Token": csrf } : {}) },
      });
      observed.http.push({ path, context: admin ? "admin" : "user", method, status: response.status() });
      return { status: response.status(), data: await response.json() };
    }
    async function openPublicPages() {
      await rolePage.locator(".main-nav [data-page=scores]:not([data-phase])").click();
      await rolePage.waitForSelector(".match-row");
      assert.equal((await request("/public/matches")).status, 200);
      await rolePage.locator('.match-row[data-fixture="web-integration-match-0"] .score-button').click();
      await rolePage.waitForFunction(() => document.querySelector("#dialog-content").textContent.includes("测试主队甲"));
      assert.equal((await request("/public/matches/web-integration-match-0")).status, 200);
      await rolePage.keyboard.press("Escape");
      for (const [phase, label] of [["scheduled", "未开赛"], ["finished", "完场"]]) {
        await rolePage.locator(`.main-nav [data-phase=${phase}]`).click();
        await rolePage.waitForFunction((label) => document.querySelectorAll(".match-row").length === 1 && document.querySelector(".match-row").textContent.includes(label), label);
      }
      observed.pages.push("比分、赛程、赛果及比赛详情可读");
      await rolePage.locator(".main-nav [data-page=experts]").click();
      await rolePage.waitForSelector("[data-expert-row]");
      assert.equal((await request("/public/experts")).status, 200);
      observed.pages.push("专家公开摘要可读");
    }
    try {
      if (role.key === "admin") {
        await isolated.addCookies((await context.cookies()).filter((cookie) => cookie.name.startsWith("v2_admin")));
      }
      await rolePage.goto(origin);
      await rolePage.waitForSelector(".match-row");
      const isUser = ["ordinary", "expired", "valid"].includes(role.key);
      if (isUser) {
        await rolePage.locator("#account-entry").click();
        await rolePage.locator("#auth-form [name=email]").fill(role.key + "@role-matrix.invalid");
        await rolePage.locator("#auth-form [name=password]").fill("test-only-role-matrix-password-1234");
        await rolePage.locator("#auth-form button").click();
        await rolePage.waitForSelector("#account-content:not([hidden])");
        const identity = await request("/auth/me");
        assert.equal(identity.status, 200);
        assert.equal(identity.data.user.role, "user");
        assert.equal(identity.data.membership.code, role.key === "valid" ? "webvip" : "free");
        assert.equal(identity.data.membership.entitlements.expert_full_history === true, role.key === "valid");
      }
      await openPublicPages();
      await rolePage.locator(".main-nav [data-page=membership]").click();
      await rolePage.waitForSelector(".plan-card");
      assert.equal((await request("/public/membership/plans")).status, 200);
      await rolePage.waitForFunction(() => !document.querySelector("#membership-current").textContent.includes("读取"));
      const memberText = await rolePage.locator("#membership-current").innerText();
      assert.match(memberText, role.key === "valid" ? /隔离专家会员.*有效至/ : isUser ? /游客与免费用户/ : /登录后查看我的权益/);
      observed.pages.push("会员页展示实际有效权益");
      if (role.key === "expired") {
        const history = await request("/auth/export?section=memberships");
        assert.equal(history.status, 200);
        const expired = history.data.items.find((grant) => grant.code === "webvip");
        assert.equal(expired.status, "active");
        assert.ok(Date.parse(expired.ends_at) < Date.now());
        assert.ok(Date.parse(expired.starts_at) < Date.parse(expired.ends_at));
        observed.expiry_evidence = "active grant ended before browser clock; effective membership free";
      }
      // A direct/deep link must enforce the same gate as the visible history page.
      await rolePage.locator(".left-rail [data-page=messages]").click();
      await rolePage.locator("#message-expert").selectOption(expertId);
      if (role.key === "valid") {
        await rolePage.waitForSelector("#messages-list .message-card");
        await rolePage.locator('#messages-list [data-message="66666666-6666-4666-8666-666666666666"]').click();
        await rolePage.waitForFunction(() => document.querySelector("#dialog-content").textContent.includes("明晚比赛尚未明确方向"));
        assert.match(await rolePage.locator("#dialog-content").innerText(), /用户权益读取/);
        await rolePage.keyboard.press("Escape");
      } else {
        await rolePage.waitForFunction(() => document.querySelector("#messages-state").textContent.includes("需要完整记录权益"));
        assert.equal(await rolePage.locator("#messages-list .message-card").count(), 0);
      }
      assert.equal((await request("/public/experts/" + expertId + "/messages")).status, role.key === "valid" ? 200 : 403);
      assert.equal((await request("/public/messages/66666666-6666-4666-8666-666666666666")).status, role.key === "valid" ? 200 : 403);
      const mediaDetail = role.key === "valid" ? await request("/public/messages/22222222-2222-4222-8222-222222222222") : null;
      const mediaPath = mediaDetail ? mediaDetail.data.message.media_objects[0].url.replace('/api/v1', '') : "/public/messages/22222222-2222-4222-8222-222222222222/media/0";
      assert.equal((await request(mediaPath)).status, role.key === "valid" ? 503 : 403);
      observed.pages.push(role.key === "valid" ? "会员原文可读；未配置媒体如实503" : "原文、深链和媒体一致拒绝");
      await rolePage.locator(".left-rail [data-page=account]").click();
      if (isUser) {
        await rolePage.waitForFunction((key) => document.querySelector("#account-verification").textContent.includes("隔离绑定-" + key), role.key);
        assert.equal(await rolePage.locator("#account-content").isVisible(), true);
        assert.equal(await rolePage.locator("#leyu-form [name=account_label]").inputValue(), "隔离绑定-" + role.key);
        const otherSuffix = role.key === "valid" ? "1" : "3";
        const account = await request("/accounts/leyu?user_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa" + otherSuffix + "&account_id=bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb" + otherSuffix);
        assert.equal(account.status, 200);
        assert.equal(account.data.account.account_label, "隔离绑定-" + role.key);
        assert.equal("encrypted_credentials" in account.data.account, false);
        const exported = await request("/auth/export?section=profile&user_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa" + otherSuffix);
        assert.equal(exported.status, 200);
        assert.equal(exported.data.items.length, 1);
        assert.equal(exported.data.items[0].profile.email, role.key + "@role-matrix.invalid");
        assert.equal(exported.data.items[0].leyu_account.account_label, "隔离绑定-" + role.key);
        const forgedWrite = await request("/accounts/leyu", { method: "PUT", body: { user_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa" + otherSuffix, account_label: "forged-target", username: "not-a-real-account", password: "not-a-provider-password" } });
        assert.equal(forgedWrite.status, 422);
        assert.equal((await request("/accounts/leyu")).data.account.account_label, "隔离绑定-" + role.key);
        assert.equal((await request("/accounts/orders")).status, 200);
        observed.pages.push("个人中心仅本人绑定；他人标识不能切换归属或写入");
      } else {
        await rolePage.waitForFunction(() => document.querySelector("#identity-panel").textContent.includes("登录后管理会员权益"));
        assert.equal(await rolePage.locator("#account-content").isVisible(), false);
        for (const path of ["/auth/me", "/accounts/leyu", "/accounts/orders", "/auth/export?section=profile"]) {
          assert.equal((await request(path)).status, 401);
        }
        observed.pages.push("用户账户及订单入口要求独立用户身份");
      }
      await rolePage.locator(".utility-bar [data-page=admin]").click();
      if (role.key === "admin") {
        await rolePage.waitForSelector("#admin-content:not([hidden])");
        assert.match(await rolePage.locator("#admin-identity").innerText(), /管理员.*独立于普通用户会话/s);
        assert.equal((await request("/admin/me", { admin: true })).status, 200);
        assert.equal((await request("/admin/users", { admin: true })).status, 200);
        assert.equal((await request("/admin/settings", { admin: true })).status, 200);
        assert.equal((await request("/admin/telegram/messages/66666666-6666-4666-8666-666666666666", { admin: true })).status, 200);
        await rolePage.locator("[data-admin-tab=telegram]").click();
        await rolePage.locator("#admin-telegram").getByRole("button", { name: "查看全部专家消息与审核", exact: true }).click();
        await rolePage.waitForSelector("#messages-list .message-card");
        await rolePage.locator('#messages-list [data-message="66666666-6666-4666-8666-666666666666"]').click();
        await rolePage.waitForFunction(() => document.querySelector("#dialog-content").textContent.includes("管理员读取"));
        assert.match(await rolePage.locator("#dialog-content").innerText(), /明晚比赛尚未明确方向/);
        observed.pages.push("管理员独立管理及审核原文可读，未继承用户账户权限");
      } else {
        await rolePage.waitForSelector("#admin-identity [data-login=admin]");
        assert.equal(await rolePage.locator("#admin-content").isVisible(), false);
        assert.match(await rolePage.locator("#admin-identity").innerText(), /用户会话不会获得管理权限/);
        for (const path of ["/admin/me", "/admin/users", "/admin/settings", "/admin/orders"]) {
          assert.equal((await request(path, { admin: true })).status, 401);
        }
        observed.pages.push("管理入口登录门禁与全部管理接口一致拒绝");
      }
      observed.status = "passed";
    } finally {
      await isolated.close();
    }
  });
}
await scenario("真实管理员退出保留本人身份与手机版布局", async () => {
  await page.locator(".utility-bar [data-page=admin]").click();
  await page.waitForSelector("#admin-content:not([hidden])");
  await page.locator("#admin-logout").click();
  await page.waitForSelector("#admin-content[hidden]", { state: "attached" });
  assert.equal((await api("/admin/me", { admin: true })).status, 401);
  assert.equal((await api("/auth/me")).status, 200);
  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .locator(".mobile-bottom [data-page=scores]:not([data-phase])")
    .click();
  await page.waitForSelector(".match-row");
  assert.equal(
    await page.evaluate(() => document.body.scrollWidth > innerWidth),
    false,
  );
  await page.screenshot({
    path: evidence + "/integration-mobile-scores-real-api-fixture.png",
  });
});
await scenario("真实无未结算记录注销撤销本人会话", async () => {
  await page.locator("#account-entry").click();
  await page.locator("#closure-section").evaluate((el) => (el.open = true));
  await page.waitForFunction(
    () =>
      !document
        .querySelector("#closure-status")
        .textContent.includes("读取注销状态后"),
  );
  await page
    .locator("#closure-form [name=confirmation_phrase]")
    .fill("注销我的球讯账户");
  await page.locator("#closure-form button").click();
  await page.waitForFunction(
    () => document.querySelector("#account-content").hidden,
  );
  assert.equal((await api("/auth/me")).status, 401);
});
await scenario("真实Web入口拒绝内部路由并限制API请求大小", async () => {
  assert.equal((await page.request.get(origin + "/internal/collector/config")).status(), 404);
  assert.equal((await page.request.get(origin + "/mcp/tools")).status(), 404);
  const oversized = await page.request.post(origin + "/api/v1/auth/register", {
    headers: { "Content-Type": "application/json" },
    data: "x".repeat(1024 * 1024 + 1),
  });
  assert.equal(oversized.status(), 413);
});
await fs.writeFile(
  "/work/tests/browser/integration-results.json",
  JSON.stringify(
    {
      date: new Date().toISOString(),
      environment:
        "fresh source API + nginx + isolated temporary PostgreSQL; artificial seeded data; live orders disabled; no provider services",
      results,
      browser_errors: errors,
      failed_upstream_responses: failedResponses,
      role_access_matrix: roleAccessMatrix,
      league_directory_evidence: leagueDirectoryEvidence,
    },
    null,
    2,
  ),
);
await browser.close();
if (results.some((r) => r.status === "failed") || errors.length)
  process.exit(1);
