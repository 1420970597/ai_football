import { chromium } from "/opt/verification/node_modules/playwright/index.mjs";
import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs/promises";
const expertId = "11111111-1111-4111-8111-111111111111";
const origin = "http://web";
const evidence = "/work/reports/jiebao-score-reference/images";
const results = [];
const errors = [];
function otp(secret = "JBSWY3DPEHPK3PXP", stepOffset = 0) {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const char of secret)
    bits += alphabet.indexOf(char).toString(2).padStart(5, "0");
  const key = Buffer.from(bits.match(/.{8}/g).map((x) => parseInt(x, 2)));
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30000) + stepOffset));
  const digest = crypto.createHmac("sha1", key).update(counter).digest();
  const offset = digest.at(-1) & 15;
  return String((digest.readUInt32BE(offset) & 0x7fffffff) % 1000000).padStart(
    6,
    "0",
  );
}
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
  return { status: response.status(), data: await response.json() };
}
await scenario("真实API比分投影、联赛多选和赛前赛果", async () => {
  await page.goto(origin);
  await page.waitForSelector(".match-row");
  assert.equal(await page.locator(".match-row").count(), 4);
  await page.locator("#league-filter").click();
  await page.locator('[data-league-id="league-0"]').check();
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
await scenario("真实管理员MFA与独立用户会话并存", async () => {
  await page.locator(".utility-bar [data-page=admin]").click();
  await page.locator("#admin-identity [data-login=admin]").click();
  await page
    .locator("#auth-form [name=email]")
    .fill("admin@web-integration.invalid");
  await page
    .locator("#auth-form [name=password]")
    .fill("test-only-admin-password-1234");
  await page.locator("#auth-form [name=totp_code]").fill(otp());
  await page.locator("#auth-form button").click();
  await page.waitForSelector("#admin-content:not([hidden])");
  assert.equal((await api("/auth/me")).data.user.role, "user");
  assert.equal(
    (await api("/admin/me", { admin: true })).data.user.role,
    "admin",
  );
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
    await page.locator(".utility-bar [data-page=admin]").click();
    await page.locator("[data-admin-tab=system]").click();
    await page
      .locator("#generate-mfa-recovery")
      .evaluate((el) => (el.closest("details").open = true));
    await page.locator("#generate-mfa-recovery").click();
    await page.locator("#confirm-yes").click();
    await page.waitForSelector("[data-once-secret] pre");
    assert.equal(
      (await page.locator("[data-once-secret] pre").innerText()).split("\n")
        .length,
      10,
    );
    await page.keyboard.press("Escape");
    await page.waitForFunction(
      () => !document.querySelector("[data-once-secret]"),
    );
  },
);
await scenario("真实管理员换机、错误验证码保留旧因子、恢复码登录", async () => {
  await page.locator("#mfa-start").click();
  await page.waitForSelector("#mfa-secret");
  const secret = await page.locator("#mfa-secret").innerText();
  assert.ok(secret.length >= 16);
  assert.notEqual(secret, "JBSWY3DPEHPK3PXP");
  assert.equal(await page.locator("#mfa-qr svg").count(), 1);
  const pending = await api("/admin/mfa/status", { admin: true });
  assert.equal(pending.status, 200);
  assert.ok(pending.data.pending.enrollment_id);
  assert.equal("secret" in pending.data, false);
  const validCodes = new Set([-1, 0, 1].map((offset) => otp(secret, offset)));
  let bad = 0;
  while (validCodes.has(String(bad).padStart(6, "0"))) bad++;
  await page
    .locator("#mfa-enrollment-form [name=totp_code]")
    .fill(String(bad).padStart(6, "0"));
  await page.locator("#mfa-enrollment-form button").click();
  await page.waitForFunction(() =>
    document
      .querySelector("#mfa-message")
      .textContent.includes("原验证器保持有效"),
  );
  assert.equal(await page.locator("#mfa-secret").innerText(), secret);
  assert.ok((await api("/admin/mfa/status", { admin: true })).data.pending);
  const confirmationCode = otp(secret);
  await page
    .locator("#mfa-enrollment-form [name=totp_code]")
    .fill(confirmationCode);
  await page.locator("#mfa-enrollment-form button").click();
  await page.waitForSelector("[data-once-secret] pre");
  const recoveryCodes = (
    await page.locator("[data-once-secret] pre").innerText()
  ).split("\n");
  assert.equal(recoveryCodes.length, 10);
  assert.equal(await page.locator("#mfa-secret").count(), 0);
  await page.keyboard.press("Escape");
  await page.waitForFunction(
    () => !document.querySelector("[data-once-secret]"),
  );
  const changed = await api("/admin/mfa/status", { admin: true });
  assert.equal(changed.data.pending, null);
  assert.equal(changed.data.recovery_codes_remaining, 10);
  assert.equal((await api("/auth/me")).status, 200);
  await page.locator("#admin-logout").click();
  await page.waitForSelector("#admin-content[hidden]", { state: "attached" });
  const replay = await api("/admin/login", {
    admin: true,
    method: "POST",
    body: {
      email: "admin@web-integration.invalid",
      password: "test-only-admin-password-1234",
      totp_code: confirmationCode,
    },
  });
  assert.equal(replay.status, 401);
  await page.locator("#admin-identity [data-login=admin]").click();
  await page.locator("[data-use-admin-recovery]").click();
  await page
    .locator("#auth-form [name=email]")
    .fill("admin@web-integration.invalid");
  await page
    .locator("#auth-form [name=password]")
    .fill("test-only-admin-password-1234");
  await page.locator("#auth-form [name=recovery_code]").fill(recoveryCodes[0]);
  await page.locator("#auth-form button").click();
  await page.waitForSelector("#admin-content:not([hidden])");
  assert.equal((await api("/admin/me", { admin: true })).status, 200);
  assert.equal((await api("/auth/me")).status, 200);
  assert.equal(
    (await api("/admin/mfa/status", { admin: true })).data
      .recovery_codes_remaining,
    9,
  );
});
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
    .locator("#closure-form [name=password]")
    .fill("updated-test-user-password-1234");
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
    },
    null,
    2,
  ),
);
await browser.close();
if (results.some((r) => r.status === "failed") || errors.length)
  process.exit(1);
