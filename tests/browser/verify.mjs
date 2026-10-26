import { chromium } from "/opt/verification/node_modules/playwright/index.mjs";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";
const base = "/work/web/app";
const evidence = "/work/reports/jiebao-score-reference/images";
const server = http.createServer(async (req, res) => {
  try {
    const pathname = new URL(req.url, "http://localhost").pathname;
    const target = path.resolve(
      base,
      pathname.startsWith("/assets/") ? pathname.slice(8) : "index.html",
    );
    if (!target.startsWith(base + "/")) throw new Error("invalid path");
    const body = await fs.readFile(target);
    res.writeHead(200, {
      "Content-Type": target.endsWith(".js")
        ? "text/javascript"
        : target.endsWith(".css")
          ? "text/css"
          : "text/html",
    });
    res.end(body);
  } catch {
    res.writeHead(404).end();
  }
});
await new Promise((resolve) => server.listen(8080, "127.0.0.1", resolve));
const browser = await chromium.launch({
  headless: true,
  args: ["--disable-dev-shm-usage"],
});
const id = "11111111-1111-4111-8111-111111111111",
  mid = "22222222-2222-4222-8222-222222222222",
  sid = "33333333-3333-4333-8333-333333333333";
const expert = {
  id,
  display_nickname: "林老师 TG",
  total_signals: 25,
  settled_signals: 20,
  win: 10,
  half_win: 2,
  loss: 5,
  half_loss: 1,
  push: 1,
  void: 1,
  pending: 5,
  effective_stake: 16.5,
  unit_net: 3.42,
  accuracy: 11 / 16.5,
  average_unit_net: 0.171,
  unit_roi: 3.42 / 16.5,
};
const quote = {
  quote_id: "q-system-1",
  market_name: "亚洲让球",
  market_kind: "handicap",
  period: "full_time",
  line: "-0.25",
  selection: "home",
  price: "1.96",
  status: "open",
  is_main: true,
  fresh: true,
  source_updated_at: "2026-10-26T11:10:00Z",
};
const fixtures = Array.from({ length: 16 }, (_, i) => ({
  id: "match-" + i,
  sport: "football",
  competition_name: ["英格兰超级联赛", "西班牙甲级联赛", "欧洲冠军联赛"][i % 3],
  home_team: ["利物浦", "皇家马德里", "国际米兰", "阿森纳"][i % 4],
  away_team: ["曼城", "巴塞罗那", "拜仁慕尼黑", "切尔西"][i % 4],
  phase: i < 3 ? "live" : i === 7 ? "finished" : "scheduled",
  match_state: "active",
  kickoff_at: "2026-10-26T11:00:00Z",
  source_updated_at: "2026-10-26T11:10:00Z",
  home_score: i < 3 ? 1 : null,
  away_score: i < 3 ? 0 : null,
  clock_minute: i < 3 ? 63 : null,
  quote_fresh: i !== 3,
  markets: [{ ...quote, fresh: i !== 3 }],
}));
const signal = {
  id: sid,
  message_id: mid,
  expert_id: id,
  display_nickname: expert.display_nickname,
  fixture_id: "match-0",
  home_team: "利物浦",
  away_team: "曼城",
  competition_name: "英格兰超级联赛",
  kickoff_at: "2026-10-26T11:00:00Z",
  phase: "live",
  market_name: "亚洲让球",
  period: "full_time",
  line: "-0.25",
  selection: "home",
  recommendation_price: "1.96",
  quote_id: "q-system-1",
  quote_time: "2026-10-26T11:10:00Z",
  created_at: "2026-10-26T11:11:00Z",
  outcome: "pending",
  unit_net: null,
};
const message = {
  id: mid,
  expert_id: id,
  display_nickname: expert.display_nickname,
  posted_at: "2026-10-26T11:11:00Z",
  revision: 2,
  raw_text:
    '利物浦 -0.25，皇家马德里 大2.5。两条买入。\n<img src=x onerror="window.__xss=1">',
  media_state: "ready",
  media_objects: [{ url: "/api/v1/public/messages/" + mid + "/media/0" }],
  analysis_state: "completed",
};
const plans = [
  {
    code: "member",
    name: "专家会员",
    price: "88",
    currency: "CNY",
    duration_days: 30,
    entitlements: {
      expert_full_history: true,
      expert_live_signals: true,
      automatic_betting: true,
    },
    enabled: true,
  },
];
const record = {
  message,
  signals: [
    signal,
    {
      ...signal,
      id: "44444444-4444-4444-8444-444444444444",
      fixture_id: "match-1",
      home_team: "皇家马德里",
      away_team: "巴塞罗那",
      market_name: "进球大小",
      line: "2.5",
      selection: "over",
      quote_id: "q-system-2",
    },
  ],
  analysis: [
    {
      trace_kind: "model_input",
      payload: { text: message.raw_text, images: [{ type: "image_url" }] },
      created_at: "2026-10-26T11:11:00Z",
    },
    {
      trace_kind: "tool_result",
      payload: { quote_id: "q-system-1", search_complete: true },
      created_at: "2026-10-26T11:11:01Z",
    },
    {
      trace_kind: "model_output",
      payload: { summary: "已匹配两场比赛和两个盘口，接受2条推荐。" },
      created_at: "2026-10-26T11:11:02Z",
    },
  ],
};
const results = [];
let caught = [];
async function setup({
  mobile = false,
  dual = false,
  initialError = false,
  captureLoading = false,
} = {}) {
  const context = await browser.newContext({
    viewport: mobile
      ? { width: 390, height: 844 }
      : { width: 1440, height: 960 },
    isMobile: mobile,
    deviceScaleFactor: 1,
  });
  await context.addCookies([
    { name: "v2_csrf", value: "user-test-csrf", url: "http://127.0.0.1:8080" },
    {
      name: "v2_admin_csrf",
      value: "admin-test-csrf",
      url: "http://127.0.0.1:8080",
    },
  ]);
  const log = [];
  const mode = {
    user: dual,
    admin: dual,
    error: initialError,
    slow: captureLoading,
    tg: "idle",
    version: 7,
  };
  const page = await context.newPage();
  page.on("pageerror", (error) => caught.push(error.message));
  await page.route("**/api/v1/**", async (route) => {
    const req = route.request(),
      url = new URL(req.url()),
      p = url.pathname.slice(7),
      method = req.method();
    const body = req.postDataJSON();
    log.push({
      path: p,
      query: url.search,
      method,
      body,
      headers: req.headers(),
    });
    const reply = (data, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(data),
      });
    if (p.includes("/media/"))
      return route.fulfill({
        status: 200,
        contentType: "image/png",
        body: Buffer.from(
          "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6gS4AAAAASUVORK5CYII=",
          "base64",
        ),
      });
    if (p === "/auth/me" || p === "/admin/me") {
      const admin = p.includes("/admin/");
      if (!(admin ? mode.admin : mode.user))
        return reply({ detail: "尚未登录" }, 401);
      return reply({
        user: {
          id,
          email: admin ? "admin@example.test" : "user@example.test",
          display_name: admin ? "管理测试" : "普通测试",
          role: admin ? "admin" : "user",
        },
        membership: admin
          ? null
          : {
              name: "专家会员",
              code: "member",
              ends_at: "2026-11-26T11:00:00Z",
              entitlements: plans[0].entitlements,
            },
      });
    }
    if (
      p === "/auth/login" ||
      p === "/auth/register" ||
      p === "/admin/login" ||
      p === "/admin/recovery-login"
    ) {
      if (p.startsWith("/admin/")) mode.admin = true;
      else mode.user = true;
      return reply({ user: { id }, csrf_token: "test-csrf" });
    }
    if (p === "/auth/recovery/status")
      return reply({
        delivery_configured: false,
        self_service_available: false,
        mode: "administrator_assisted",
        message: "邮件恢复未配置，请联系管理员人工核验后获取恢复码。",
      });
    if (p === "/auth/recovery/complete")
      return reply({ reset: true, reauthentication_required: true });
    if (p === "/auth/export")
      return reply({
        schema_version: 1,
        section: url.searchParams.get("section"),
        generated_at: "2026-10-26T11:00:00Z",
        items: [{ display_name: "普通测试" }],
        next_cursor: null,
        excluded: ["password_hash", "session", "credentials"],
      });
    if (p === "/auth/closure" && method === "POST") {
      mode.closure = "pending_accounting";
      return reply(
        { state: mode.closure, message: "注销等待未结算订单对账。" },
        202,
      );
    }
    if (p === "/auth/closure" && method === "GET")
      return reply({
        state: mode.closure || "not_requested",
        outstanding_orders: mode.closure ? 1 : 0,
      });
    if (p === "/auth/closure/cancel") {
      mode.closure = null;
      return reply({ state: "not_requested", auto_betting: false });
    }
    if (p.endsWith("/password-recovery"))
      return reply(
        {
          code: "FIXTURE-ONE-TIME-RESET",
          expires_at: "2026-10-26T12:00:00Z",
          message: "人工核验恢复码，只显示一次",
        },
        201,
      );
    if (p === "/admin/mfa/recovery-codes")
      return reply({
        codes: ["FIXTURE-MFA-RECOVERY-1", "FIXTURE-MFA-RECOVERY-2"],
        message: "只显示一次",
      });
    if (p.endsWith("/reauthenticate")) {
      mode.reauthRequired = false;
      return reply({ expires_at: "2026-10-26T12:00:00Z" });
    }
    if (
      mode.reauthRequired &&
      method !== "GET" &&
      (p === "/admin/settings" || p === "/accounts/leyu")
    )
      return reply(
        {
          detail: {
            code: "reauthentication_required",
            session_kind: p.startsWith("/admin") ? "admin" : "user",
            message: "此操作需要再次验证身份",
          },
        },
        403,
      );
    if (p === "/auth/logout") {
      mode.user = false;
      return reply({ ok: true });
    }
    if (p === "/admin/logout") {
      mode.admin = false;
      return reply({ ok: true });
    }
    if (p === "/public/matches") {
      if (mode.slow) await new Promise((r) => setTimeout(r, 700));
      if (mode.error) return reply({ detail: "隔离测试：上游读取超时" }, 503);
      if (mode.long) {
        const all = Array.from({ length: 125 }, (_, i) => ({
          ...fixtures[i % fixtures.length],
          id: "long-" + i,
        }));
        if (mode.removeFirst) all.shift();
        const start = Number(url.searchParams.get("cursor") || 0);
        const end = start + Number(url.searchParams.get("page_size") || 50);
        return reply({
          items: all.slice(start, end),
          next_cursor: end < all.length ? String(end) : null,
          server_time: "2026-10-26T11:15:00Z",
        });
      }
      return reply({
        items: fixtures.filter(
          (f) =>
            url.searchParams.get("phase") === "all" ||
            f.phase === url.searchParams.get("phase"),
        ),
        next_cursor: url.searchParams.has("cursor") ? null : "next-fixtures",
        server_time: "2026-10-26T11:12:00Z",
      });
    }
    if (p === "/public/leagues")
      return reply({
        items: [
          {
            competition_id: "1",
            competition_name: "英格兰超级联赛",
            match_count: 8,
          },
          {
            competition_id: "2",
            competition_name: "西班牙甲级联赛",
            match_count: 6,
          },
        ],
      });
    if (p.startsWith("/public/matches/"))
      return reply({
        fixture: fixtures[0],
        markets: [
          { ...quote, price: mode.quotePrice || quote.price },
          { ...quote, quote_id: "q-old", fresh: false },
          {
            ...quote,
            quote_id: "q-half-extra",
            period: "first_half",
            is_main: false,
          },
        ],
        expert_signals: [signal],
        server_time: "2026-10-26T11:12:00Z",
      });
    if (p === "/public/experts")
      return reply({
        items: [
          expert,
          {
            ...expert,
            id: "55555555-5555-4555-8555-555555555555",
            display_nickname: "阿杰聊球",
            total_signals: 0,
            settled_signals: 0,
            accuracy: null,
            unit_roi: null,
            unit_net: 0,
          },
        ],
      });
    if (p.endsWith("/trend"))
      return reply({
        items: [
          { day: "2026-10-24", cumulative_unit_net: -0.5 },
          { day: "2026-10-25", cumulative_unit_net: 1.2 },
          { day: "2026-10-26", cumulative_unit_net: 3.42 },
        ],
        basis: "latest_settlement_at_latest_result",
      });
    if (p.startsWith("/public/experts/") && p.endsWith("/messages"))
      return mode.user || mode.admin
        ? reply({
            items: [
              message,
              {
                ...message,
                id: "66666666-6666-4666-8666-666666666666",
                raw_text: "明晚巴塞罗那场，待明确方向",
                analysis_state: "unresolved",
                media_objects: [],
              },
            ],
            next_cursor: url.searchParams.has("cursor")
              ? null
              : "next-messages",
          })
        : reply({ detail: "完整专家记录需要会员权益" }, 403);
    if (p.startsWith("/public/experts/"))
      return reply({
        expert,
        from_utc: "2026-09-26T11:00:00Z",
        to_utc: "2026-10-26T11:00:00Z",
      });
    if (p === "/public/signals")
      return reply({ items: [signal], next_cursor: null, limited: !mode.user });
    if (
      mode.longMessage &&
      p.startsWith("/public/messages/") &&
      (mode.user || mode.admin)
    )
      return reply({
        ...record,
        message: {
          ...message,
          display_nickname: "改名后的专家 TG",
          raw_text: "比赛下注说明。".repeat(700) + "最终盘口仍是全场大2.5",
          reply_context: { raw_text: "上一条讨论赛前比赛，请核对明晚时间。" },
          media_objects: [
            { url: "/api/v1/public/messages/" + mid + "/media/0" },
            { url: "/api/v1/public/messages/" + mid + "/media/1" },
          ],
        },
      });
    if (p.startsWith("/public/messages/") || p.startsWith("/public/signals/"))
      return mode.user || mode.admin
        ? reply(record)
        : reply({ detail: "完整专家记录需要会员权益" }, 403);
    if (p === "/public/membership/plans" || p === "/admin/membership/plans")
      return reply({ items: plans });
    if (p === "/accounts/leyu" && method === "GET")
      return reply({
        account: { account_label: "个人测试账户", health_state: "healthy" },
        betting: {
          auto_enabled: false,
          allowed_experts: [],
          stake_per_order: "1",
          max_per_order: "2",
          daily_limit: "10",
          open_exposure_limit: "5",
          emergency_stopped: true,
        },
      });
    if (p === "/accounts/orders") return reply({ items: [] });
    if (p === "/admin/status")
      return reply({
        database: "healthy",
        hermes: "configured",
        telegram_sources: 1,
        review_queue: 2,
      });
    if (p === "/admin/settings" && method === "GET")
      return reply({
        items: {
          leyu_api_base: { configured: true, value: "https://example.test" },
          leyu_request_id: { configured: true, masked: true },
          telegram_api_id: { value: "10001" },
          telegram_api_hash: { configured: true, masked: true },
        },
        leyu_version: mode.version,
      });
    if (p === "/admin/settings") {
      mode.version++;
      return reply({
        updated: Object.keys(body.values),
        leyu_version: mode.version,
      });
    }
    if (p === "/admin/sources")
      return reply({
        items: [
          {
            id,
            platform_chat_id: "-1001234567",
            display_name: "测试专家群",
            enabled: true,
            allow_media: true,
          },
        ],
      });
    if (p.endsWith("/experts"))
      return reply({ items: [{ ...expert, platform_sender_id: "12345" }] });
    if (p === "/admin/telegram/status")
      return reply({
        connected: false,
        authorized: false,
        auth: {
          state: mode.tg,
          attempt_id: mode.tg === "idle" ? null : id,
          qr_url:
            mode.tg === "waiting_qr"
              ? "tg://login?token=test-only-qr-token"
              : null,
          expires_at: "2026-10-26T11:30:00Z",
        },
      });
    if (p === "/admin/telegram/auth/qr") {
      mode.tg = "waiting_qr";
      return reply({
        state: mode.tg,
        attempt_id: id,
        qr_url: "tg://login?token=test-only-qr-token",
        expires_at: "2026-10-26T11:30:00Z",
      });
    }
    if (p === "/admin/telegram/auth/cancel") {
      mode.tg = "cancelled";
      return reply({ state: mode.tg });
    }
    if (p === "/admin/telegram/admins")
      return reply({
        state: "unavailable",
        reason: "member_enumeration_restricted",
        manual_id_allowed: true,
        items: [],
      });
    if (p === "/admin/telegram/dialogs")
      return reply({
        state: "ok",
        items: [{ chat_id: "-1001234567", title: "测试专家群" }],
        next_cursor: null,
      });
    if (p === "/admin/telegram/messages")
      return reply({ items: [message], next_cursor: null });
    if (p.startsWith("/admin/telegram/messages/")) return reply(record);
    if (p === "/admin/users")
      return reply({
        items: [
          {
            id,
            role: "user",
            display_name: "普通测试",
            email: "user@example.test",
          },
        ],
        next_cursor: null,
      });
    if (p === "/admin/diagnostics")
      return reply({
        checked_at: "2026-10-26T11:00:00Z",
        database: "reachable",
        schema: "verified",
        hermes: { gateway: "reachable", provider: "unverified" },
        storage: "unconfigured",
        review_queue: { count: 5000, capped: true },
        pending_media: { count: 2, capped: false },
        unknown_orders: { count: 1, capped: false },
        unresolved_reviews: { count: 3, capped: false },
        stale_quotes: { count: 4, capped: false },
        freshness: {},
      });
    if (p === "/admin/orders")
      return reply({
        items: [
          {
            id,
            state: "unknown",
            email: "user@example.test",
            account_label: "人工样本账户",
            home_team: "利物浦",
            away_team: "曼城",
            market_name: "亚洲让球",
            period: "full_time",
            line: "-0.25",
            selection: "home",
            stake: "1",
            price: "1.96",
            settled_net: null,
          },
        ],
        next_cursor: null,
        read_only: true,
      });
    if (p === "/admin/audit") return reply({ items: [] });
    return reply({ ok: true, state: "unverified", message: "隔离测试已保存" });
  });
  await page.goto("http://127.0.0.1:8080");
  if (captureLoading) {
    await page.waitForSelector(".skeleton-row");
    await page.screenshot({
      path: evidence + "/candidate-mobile-loading-fixture.png",
    });
  }
  await page.waitForSelector(
    initialError ? "#matches-state [data-retry]" : ".match-row",
  );
  return { context, page, log, mode };
}
async function test(name, action) {
  try {
    await action();
    results.push({ name, status: "passed" });
    console.log("PASS " + name);
  } catch (error) {
    results.push({ name, status: "failed", error: error.message });
    console.log("FAIL " + name + " " + error.message);
  }
}
await test("桌面比分密度、状态查询、日期与详情", async () => {
  const { page, context, log } = await setup();
  assert.equal(await page.locator(".match-row").count(), 16);
  assert.ok(
    (await page
      .locator(".match-row")
      .first()
      .evaluate((el) => el.getBoundingClientRect().height)) <= 45,
  );
  assert.ok(
    (await page
      .locator(".match-league")
      .first()
      .evaluate((el) => el.getBoundingClientRect().width)) >= 110,
  );
  assert.equal(
    await page.locator(".league-label").first().getAttribute("title"),
    "英格兰超级联赛",
  );
  assert.equal(
    await page.evaluate(() => document.body.scrollWidth > innerWidth),
    false,
  );
  await page.screenshot({
    path: evidence + "/candidate-desktop-scores-fixture.png",
  });
  await page.locator('.score-tabs [data-phase="finished"]').click();
  await page.waitForFunction(
    () => document.querySelectorAll(".match-row").length === 1,
  );
  assert.ok(
    log.some(
      (r) => r.path === "/public/matches" && r.query.includes("phase=finished"),
    ),
  );
  await page.locator('.score-tabs [data-phase="all"]').click();
  await page.waitForSelector(".match-row");
  await page.locator(".detail-button").first().click();
  await page.waitForSelector(".market-table");
  assert.match(await page.locator("#dialog-content").innerText(), /q-system-1/);
  assert.match(await page.locator("#dialog-content").innerText(), /过期/);
  await page.keyboard.press("Escape");
  await context.close();
});
await test("手机无横向溢出、关注与弹层", async () => {
  const { page, context } = await setup({ mobile: true });
  assert.equal(
    await page.evaluate(() => document.body.scrollWidth > innerWidth),
    false,
  );
  assert.ok(
    (await page
      .locator(".match-row")
      .first()
      .evaluate((el) => el.getBoundingClientRect().height)) <= 80,
  );
  await page.locator(".favorite").first().click();
  assert.equal(
    await page.locator(".favorite").first().getAttribute("aria-pressed"),
    "true",
  );
  await page.locator("#favorites-filter").click();
  assert.equal(await page.locator(".match-row").count(), 1);
  await page.locator("#favorites-filter").click();
  await page.screenshot({
    path: evidence + "/candidate-mobile-scores-fixture.png",
  });
  await page.locator(".team-home").first().click();
  await page.waitForSelector(".market-table");
  assert.equal(
    await page
      .locator("#content-dialog")
      .evaluate((el) => el.scrollWidth > el.clientWidth),
    false,
  );
  await page.screenshot({
    path: evidence + "/candidate-mobile-detail-fixture.png",
  });
  await context.close();
});
await test("完整原文、两条系统买入、工具证据与图片", async () => {
  const { page, context } = await setup({ dual: true });
  await page.locator('.main-nav [data-page="experts"]').click();
  await page.locator('[data-expert-tab="signals"]').click();
  await page.locator("#signals-list [data-message]").click();
  await page.waitForSelector(".media-gallery img");
  assert.equal(await page.locator("#dialog-content .signal-card").count(), 2);
  assert.match(await page.locator("#dialog-content").innerText(), /q-system-2/);
  assert.match(await page.locator("#dialog-content").innerText(), /已匹配两场/);
  assert.equal(await page.evaluate(() => window.__xss), undefined);
  assert.equal(await page.locator(".raw-text img").count(), 0);
  await page.screenshot({
    path: evidence + "/candidate-desktop-original-fixture.png",
  });
  await page.keyboard.press("Escape");
  await page.locator('.section-tabs [data-page="messages"]').click();
  await page.locator("#message-expert").selectOption(id);
  await page.waitForSelector(".message-card");
  assert.match(await page.locator("#messages-list").innerText(), /未解决/);
  assert.equal(await page.locator("#messages-more").isVisible(), true);
  await page.locator("#messages-more").click();
  await context.close();
});
await test("独立身份与CSRF、热配置版本、留空保密字段、授予幂等", async () => {
  const { page, context, log } = await setup({ dual: true });
  await page.locator('.utility-bar [data-page="admin"]').click();
  await page.waitForSelector("#admin-content:not([hidden])");
  await page.locator('[data-admin-tab="leyu"]').click();
  await page
    .locator('#leyu-settings input[name="leyu_request_id"]')
    .fill("new-test-request-id");
  await page.locator("#leyu-settings button").click();
  await page.waitForFunction(() =>
    document.querySelector("#toast").textContent.includes("配置已保存"),
  );
  const settings = log.find(
    (r) => r.path === "/admin/settings" && r.method === "PUT",
  );
  assert.equal(settings.headers["x-session-context"], "admin");
  assert.equal(settings.headers["x-csrf-token"], "admin-test-csrf");
  assert.equal(settings.body.expected_leyu_version, 7);
  assert.equal(settings.body.values.leyu_request_id, "new-test-request-id");
  assert.ok(!("leyu_api_token" in settings.body.values));
  await page.locator('[data-admin-tab="membership"]').click();
  await page.locator("#grant-form [name=email]").fill("member@example.test");
  await page.locator("#grant-form [name=days]").fill("30");
  await page.locator("#grant-form [name=reason]").fill("测试授权说明");
  await page.locator("#grant-form button").click();
  await page.waitForTimeout(100);
  const grant = log.find(
    (r) => r.path === "/admin/membership/grants" && r.method === "POST",
  );
  assert.match(grant.body.idempotency_key, /^[0-9a-f-]{36}$/);
  assert.equal(grant.body.reason, "测试授权说明");
  await page.locator("#admin-logout").click();
  await page.waitForSelector("#admin-content[hidden]", { state: "attached" });
  await page.locator("#account-entry").click();
  await page.waitForSelector("#account-content:not([hidden])");
  await page.locator("#emergency-stop").click();
  await page.waitForTimeout(100);
  const stop = log.find((r) => r.path === "/accounts/leyu/emergency-stop");
  assert.equal(stop.headers["x-session-context"], "user");
  assert.equal(stop.headers["x-csrf-token"], "user-test-csrf");
  assert.equal(
    await page
      .locator("#identity-panel")
      .innerText()
      .then((s) => s.includes("普通测试")),
    true,
  );
  await context.close();
});
await test("Telegram二维码、过期与成员枚举回退", async () => {
  const { page, context, mode } = await setup({ dual: true });
  await page.locator('.utility-bar [data-page="admin"]').click();
  await page.waitForSelector("#admin-content:not([hidden])");
  await page.locator('[data-admin-tab="telegram"]').click();
  await page.locator("#tg-qr-start").click();
  await page.waitForSelector("svg.qr-code");
  assert.equal((await page.locator("svg.qr-code path").count()) > 0, true);
  mode.tg = "waiting_password";
  await page.locator("#tg-refresh").click();
  await page.waitForSelector("#tg-password-form:not([hidden])");
  mode.tg = "expired";
  await page.locator("#tg-refresh").click();
  await page.waitForTimeout(100);
  assert.match(await page.locator("#telegram-auth").innerText(), /过期/);
  assert.equal(await page.locator("#tg-qr-start").isEnabled(), true);
  await page.locator("#tg-admins").click();
  await page.waitForFunction(() =>
    document.querySelector("#source-admins").textContent.includes("手动添加"),
  );
  assert.equal(
    await page.locator("#expert-source-form [name=sender_id]").isVisible(),
    true,
  );
  await page.screenshot({
    path: evidence + "/candidate-desktop-admin-telegram-fixture.png",
  });
  await context.close();
});
await test("超时保留已读赛事，恢复刷新后清除错误", async () => {
  const { page, context, mode } = await setup();
  mode.error = true;
  await page.locator("#refresh-matches").click();
  await page.waitForFunction(() =>
    document
      .querySelector("#matches-notice")
      .textContent.includes("保留上次结果"),
  );
  assert.equal(await page.locator(".match-row").count(), 16);
  await page.screenshot({
    path: evidence + "/candidate-desktop-stale-fixture.png",
  });
  mode.error = false;
  await page.locator("#refresh-matches").click();
  await page.waitForFunction(
    () =>
      !document
        .querySelector("#matches-notice")
        .textContent.includes("保留上次结果"),
  );
  assert.equal(await page.locator(".match-row").count(), 16);
  await context.close();
});
await test("游客权益拒绝、会员未配置支付、注册校验", async () => {
  const { page, context } = await setup({ mobile: true });
  await page.locator('.mobile-bottom [data-page="experts"]').click();
  await page.locator('[data-expert-tab="signals"]').click();
  await page.locator("#signals-list [data-message]").click();
  await page.waitForFunction(() =>
    document
      .querySelector("#dialog-content")
      .textContent.includes("完整专家记录需要会员权益"),
  );
  await page.locator('#dialog-content [data-page="membership"]').click();
  await page.waitForSelector(".plan-card");
  assert.equal(await page.locator(".plan-card button").isDisabled(), true);
  await page.screenshot({
    path: evidence + "/candidate-mobile-membership-fixture.png",
  });
  await page.locator("#account-entry").click();
  await page.locator('[data-auth-tab="register"]').click();
  assert.equal(
    await page.locator("#auth-form [name=password]").getAttribute("minlength"),
    "12",
  );
  await page.locator('[data-auth-tab="admin"]').click();
  assert.equal(await page.locator("#totp-field").isVisible(), true);
  await context.close();
});
await test("初始失败与无赛事状态、请求竞态不覆盖新筛选", async () => {
  const { page, context, mode } = await setup();
  await page.locator('.score-tabs [data-phase="cancelled"]').click();
  await page.waitForFunction(() =>
    document
      .querySelector("#matches-state")
      .textContent.includes("暂无符合条件"),
  );
  await page.screenshot({
    path: evidence + "/candidate-desktop-empty-fixture.png",
  });
  mode.slow = true;
  await page.locator('.score-tabs [data-phase="scheduled"]').click();
  await page.locator('.score-tabs [data-phase="live"]').click();
  await page.waitForFunction(
    () => document.querySelectorAll(".match-row").length === 3,
  );
  assert.match(await page.locator(".score-tabs .active").innerText(), /滚球/);
  await context.close();
});
await test("详情深链、前进后退、主附线筛选与保留筛选刷新", async () => {
  const { page, context } = await setup({ dual: true });
  await page.locator(".detail-button").first().click();
  await page.waitForSelector("#market-scope");
  assert.match(page.url(), /match=match-0/);
  await page.locator("#market-period").selectOption("first_half");
  await page.locator("#market-scope").selectOption("additional");
  assert.equal(await page.locator(".market-table tbody tr:visible").count(), 1);
  await page.locator("#refresh-detail").click();
  await page.waitForTimeout(100);
  assert.equal(await page.locator("#market-period").inputValue(), "first_half");
  await page.reload();
  await page.waitForSelector("#market-scope");
  assert.match(
    await page.locator("#dialog-content").innerText(),
    /q-half-extra/,
  );
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !location.hash.includes("match="));
  await page.goForward();
  await page.waitForSelector("#market-scope");
  await context.close();
});
await test("受限原文登录后恢复原消息与统计分项", async () => {
  const { page, context } = await setup();
  await page.locator('.main-nav [data-page="experts"]').click();
  await page.locator('[data-expert-tab="signals"]').click();
  await page.locator("#signals-list [data-message]").click();
  await page.waitForSelector("#dialog-content [data-login]");
  await page.locator("#dialog-content [data-login]").click();
  await page.locator("#auth-form [name=email]").fill("user@example.test");
  await page.locator("#auth-form [name=password]").fill("fixture-password");
  await page.locator("#auth-form button").click();
  await page.waitForSelector("#dialog-content .signal-card");
  assert.match(page.url(), new RegExp(mid));
  assert.equal(await page.locator("#dialog-content .signal-card").count(), 2);
  await page.keyboard.press("Escape");
  await page.waitForTimeout(100);
  await page.locator('[data-expert-tab="ranking"]').click();
  await page.locator("#experts-list [data-expert]").first().click();
  await page.waitForSelector(".metrics-grid");
  assert.match(await page.locator("#dialog-content").innerText(), /全赢 10/);
  await context.close();
});
await test("初次错误、加载与离线五状态", async () => {
  const first = await setup({ mobile: true, initialError: true });
  assert.match(
    await first.page.locator("#matches-state").innerText(),
    /上游读取超时/,
  );
  await first.page.screenshot({
    path: evidence + "/candidate-mobile-error-fixture.png",
  });
  first.mode.error = false;
  await first.page.locator("[data-retry]").click();
  await first.page.waitForSelector(".match-row");
  await first.context.setOffline(true);
  console.log(
    "OFFLINE",
    await first.page.evaluate(() => ({
      online: navigator.onLine,
      notice: document.querySelector("#matches-notice").textContent,
    })),
  );
  await first.page.waitForFunction(() =>
    document
      .querySelector("#matches-notice")
      .textContent.includes("网络已断开"),
  );
  await first.context.close();
  const loading = await setup({ mobile: true, captureLoading: true });
  await loading.context.close();
});
await test("360、768、1280的核心页面不溢出", async () => {
  const { page, context } = await setup({ dual: true });
  for (const width of [360, 768, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    for (const pageName of [
      "scores",
      "experts",
      "membership",
      "account",
      "admin",
    ]) {
      await page.evaluate((page) => {
        location.hash = page;
      }, pageName);
      await page.waitForSelector("#view-" + pageName + ".active");
      await page.waitForTimeout(100);
      assert.equal(
        await page.evaluate(() => document.body.scrollWidth > innerWidth),
        false,
        `${width} ${pageName} overflow`,
      );
    }
    await page.screenshot({
      path: evidence + `/candidate-${width}-admin-fixture.png`,
    });
  }
  await context.close();
});
await test("联赛多选按系统编号过滤并可清空", async () => {
  const { page, context, log } = await setup();
  await page.locator("#league-filter").click();
  await page.locator('[data-league-id="1"]').check();
  await page.locator('[data-league-id="2"]').check();
  await page.locator("[data-apply-leagues]").click();
  await page.waitForTimeout(100);
  const req = log.filter((r) => r.path === "/public/matches").at(-1);
  assert.deepEqual(new URLSearchParams(req.query).getAll("league_ids"), [
    "1",
    "2",
  ]);
  await page.locator("#league-filter").click();
  await page.locator("[data-clear-leagues]").click();
  await page.waitForTimeout(100);
  assert.deepEqual(
    new URLSearchParams(
      log.filter((r) => r.path === "/public/matches").at(-1).query,
    ).getAll("league_ids"),
    [],
  );
  await context.close();
});
await test("本人关闭不受空额度阻挡，启用必须完整许可", async () => {
  const { page, context, log } = await setup({ dual: true });
  await page.locator("#account-entry").click();
  await page.waitForSelector("#account-content:not([hidden])");
  await page.waitForFunction(() =>
    document.querySelector("#account-health").textContent.includes("已通过"),
  );
  await page.locator("#betting-form").evaluate((form) => {
    for (const input of form.querySelectorAll("input[type=number]"))
      input.value = "";
  });
  await page.locator("#betting-form button").click();
  await page.waitForTimeout(100);
  assert.equal(
    log.find((r) => r.path === "/accounts/leyu/auto-betting").body.enabled,
    false,
  );
  await page.locator("#betting-form [name=enabled]").check();
  for (const [key, value] of Object.entries({
    stake_per_order: "1",
    max_per_order: "2",
    daily_limit: "10",
    open_exposure_limit: "5",
  }))
    await page.locator("#betting-form [name=" + key + "]").fill(value);
  await page.locator("#betting-form input[name=expert]").first().check();
  await page.locator("#betting-form [name=confirmed]").check();
  await page.locator("#betting-form button").click();
  await page.waitForTimeout(100);
  const req = log
    .filter((r) => r.path === "/accounts/leyu/auto-betting")
    .at(-1);
  assert.equal(req.body.enabled, true);
  assert.equal(req.body.confirmation_phrase, "I_UNDERSTAND");
  assert.deepEqual(req.body.allowed_experts, [id]);
  await context.close();
});
await test("推荐定位准确报价并标示价格变化", async () => {
  const { page, context, mode } = await setup({ dual: true });
  await page.locator(".main-nav [data-page=experts]").click();
  await page.locator("[data-expert-tab=signals]").click();
  await page.locator("#signals-list [data-match]").click();
  await page.waitForSelector(".focused-quote");
  assert.equal(
    await page.locator(".focused-quote").getAttribute("data-quote"),
    "q-system-1",
  );
  mode.quotePrice = "2.05";
  await page.locator("#refresh-detail").click();
  await page.waitForFunction(() =>
    document
      .querySelector('tr[data-quote="q-system-1"]')
      .textContent.includes("2.05"),
  );
  assert.equal(
    await page.locator('tr[data-quote="q-system-1"] .price-up').isVisible(),
    true,
  );
  await context.close();
});
await test("敏感操作再次验证保留原输入并隔离身份", async () => {
  const { page, context, mode, log } = await setup({ dual: true });
  await page.locator(".utility-bar [data-page=admin]").click();
  await page.locator("[data-admin-tab=leyu]").click();
  await page
    .locator("#leyu-settings [name=leyu_request_id]")
    .fill("keep-input-after-reauth");
  mode.reauthRequired = true;
  await page.locator("#leyu-settings button").click();
  await page.waitForSelector("#reauth-dialog[open]");
  assert.equal(await page.locator("#reauth-totp").isVisible(), true);
  assert.equal(
    await page.locator("#leyu-settings [name=leyu_request_id]").inputValue(),
    "keep-input-after-reauth",
  );
  await page.locator("#reauth-form [name=password]").fill("test-only-password");
  await page.locator("#reauth-form [name=totp_code]").fill("123456");
  await page.locator("#reauth-form button[type=submit]").click();
  await page.waitForFunction(
    () => !document.querySelector("#reauth-dialog").open,
  );
  const verification = log.find((r) => r.path === "/admin/reauthenticate");
  assert.equal(verification.headers["x-session-context"], "admin");
  assert.equal(verification.headers["x-csrf-token"], "admin-test-csrf");
  assert.equal(
    log.filter((r) => r.path === "/admin/settings" && r.method !== "GET")
      .length,
    1,
  );
  await page.locator("#leyu-settings button").click();
  await page.waitForTimeout(100);
  assert.equal(
    log.filter((r) => r.path === "/admin/settings" && r.method !== "GET")
      .length,
    2,
  );
  await page.locator("#account-entry").click();
  await page.waitForFunction(
    () => !document.querySelector("#leyu-form input").disabled,
  );
  await page.locator("#leyu-form [name=account_label]").fill("本人备注待提交");
  await page.locator("#leyu-form [name=username]").fill("user-only-name");
  await page.locator("#leyu-form [name=password]").fill("user-only-password");
  mode.reauthRequired = true;
  await page.locator("#leyu-form button.primary-button").click();
  await page.waitForSelector("#reauth-dialog[open]");
  assert.equal(await page.locator("#reauth-totp").isVisible(), false);
  assert.equal(
    await page.locator("#leyu-form [name=account_label]").inputValue(),
    "本人备注待提交",
  );
  await page.locator("#reauth-form [name=password]").fill("user-password-test");
  await page.locator("#reauth-form button[type=submit]").click();
  await page.waitForFunction(
    () => !document.querySelector("#reauth-dialog").open,
  );
  const userVerification = log.find((r) => r.path === "/auth/reauthenticate");
  assert.equal(userVerification.headers["x-session-context"], "user");
  assert.equal(userVerification.headers["x-csrf-token"], "user-test-csrf");
  await context.close();
});
await test("多页赛事刷新保留加载深度与滚动并移除消失赛事", async () => {
  const { page, context, mode, log } = await setup();
  mode.long = true;
  await page.locator("#refresh-matches").click();
  await page.waitForFunction(
    () => document.querySelectorAll(".match-row").length === 50,
  );
  await page.locator("#load-more").click();
  await page.waitForFunction(
    () => document.querySelectorAll(".match-row").length === 100,
  );
  await page.evaluate(() => scrollTo(0, 1200));
  const scroll = await page.evaluate(() => scrollY);
  mode.removeFirst = true;
  await page.locator("#refresh-matches").evaluate((button) => button.click());
  await page.waitForFunction(
    () => !document.querySelector('[data-fixture="long-0"]'),
  );
  assert.equal(await page.locator(".match-row").count(), 100);
  assert.equal(await page.locator('[data-fixture="long-100"]').count(), 1);
  assert.ok(Math.abs((await page.evaluate(() => scrollY)) - scroll) < 5);
  assert.equal(
    new URLSearchParams(
      log.filter((r) => r.path === "/public/matches").at(-1).query,
    ).get("page_size"),
    "100",
  );
  await context.close();
});
await test("专家关注独立于资金授权并在当前浏览器保留", async () => {
  const { page, context, log } = await setup();
  await page.locator(".main-nav [data-page=experts]").click();
  await page.locator("#followed-experts").click();
  assert.equal(await page.locator("#experts-follow-empty").isVisible(), true);
  await page.locator("#followed-experts").click();
  await page.locator("[data-follow-expert]").first().click();
  await page.locator("#followed-experts").click();
  assert.equal(await page.locator(".expert-row:visible").count(), 1);
  await page.reload();
  await page.waitForSelector("[data-follow-expert]");
  assert.equal(
    await page
      .locator("[data-follow-expert]")
      .first()
      .getAttribute("aria-pressed"),
    "true",
  );
  assert.equal(
    log.filter((request) => request.path === "/accounts/leyu/auto-betting")
      .length,
    0,
  );
  await context.close();
});
await test("找回入口如实提示未配置邮件并完成恢复码提交", async () => {
  const { page, context, log } = await setup();
  await page.locator("#account-entry").click();
  await page.locator("#auth-form [name=email]").fill("user@example.test");
  await page.locator("[data-password-recovery]").click();
  await page.waitForSelector("#password-reset-form");
  assert.match(
    await page.locator("#dialog-content").innerText(),
    /邮件恢复未配置/,
  );
  assert.equal(
    await page.locator("#password-reset-form [name=email]").inputValue(),
    "user@example.test",
  );
  await page
    .locator("#password-reset-form [name=code]")
    .fill("FIXTURE-RESET-CODE");
  await page
    .locator("#password-reset-form [name=new_password]")
    .fill("new-test-password-1234");
  await page.locator("#password-reset-form button").click();
  await page.waitForFunction(() => document.querySelector("#auth-dialog").open);
  assert.equal(
    log.some((request) => request.path === "/auth/recovery/request"),
    false,
  );
  assert.equal(
    log.find((request) => request.path === "/auth/recovery/complete").body.code,
    "FIXTURE-RESET-CODE",
  );
  await context.close();
});
await test("本人导出与待对账注销取消不恢复投注许可", async () => {
  const { page, context, log } = await setup({ dual: true });
  await page.locator("#account-entry").click();
  await page
    .locator("[data-export=profile]")
    .evaluate((el) => (el.closest("details").open = true));
  await page.locator("[data-export=profile]").click();
  await page.waitForSelector("[data-export-download]");
  assert.match(
    await page.locator("#dialog-content").innerText(),
    /password_hash/,
  );
  const download = page.waitForEvent("download");
  await page.locator("[data-export-download]").click();
  assert.equal(
    (await download).suggestedFilename(),
    "qiuxun-personal-profile.json",
  );
  await page.keyboard.press("Escape");
  await page.locator("#closure-section").evaluate((el) => (el.open = true));
  await page.waitForFunction(() =>
    document.querySelector("#closure-status").textContent.includes("尚未申请"),
  );
  await page.locator("#closure-form [name=password]").fill("test-password");
  await page
    .locator("#closure-form [name=confirmation_phrase]")
    .fill("注销我的球讯账户");
  await page.locator("#closure-form button").click();
  await page.waitForSelector("#closure-cancel:visible");
  assert.equal(await page.locator("#closure-form").isVisible(), true);
  assert.match(
    await page.locator("#closure-form button").innerText(),
    /再次核对/,
  );
  await page.locator("#closure-cancel").click();
  await page
    .locator("#cancel-closure-form [name=password]")
    .fill("test-password");
  await page.locator("#cancel-closure-form button").click();
  await page.waitForFunction(
    () => !document.querySelector("#content-dialog").open,
  );
  assert.ok(log.some((request) => request.path === "/auth/closure/cancel"));
  assert.equal(
    log.filter((request) => request.path === "/accounts/leyu/auto-betting")
      .length,
    0,
  );
  assert.equal(
    log.find((request) => request.path === "/auth/export").headers[
      "x-session-context"
    ],
    "user",
  );
  await context.close();
});
await test("管理员恢复码只显示一次与只读待办订单", async () => {
  const { page, context, log } = await setup({ dual: true });
  await page.locator(".utility-bar [data-page=admin]").click();
  await page.waitForFunction(() =>
    document.querySelector("#admin-diagnostics").textContent.includes("5000+"),
  );
  assert.match(
    await page.locator("#admin-diagnostics").innerText(),
    /模型提供方尚未验证/,
  );
  await page.locator("[data-admin-tab=orders]").click();
  await page.waitForSelector("#admin-orders-list .admin-record");
  assert.match(
    await page.locator("#admin-orders-list").innerText(),
    /结果未知/,
  );
  assert.equal(await page.locator("#admin-orders-list button").count(), 0);
  await page.locator("[data-admin-tab=system]").click();
  await page
    .locator("#generate-mfa-recovery")
    .evaluate((el) => (el.closest("details").open = true));
  await page.locator("#generate-mfa-recovery").click();
  await page.locator("#confirm-yes").click();
  await page.waitForSelector("[data-once-secret]");
  assert.match(
    await page.locator("[data-once-secret]").innerText(),
    /FIXTURE-MFA/,
  );
  await page.keyboard.press("Escape");
  await page.waitForFunction(
    () => !document.querySelector("[data-once-secret]"),
  );
  await page.locator("[data-admin-tab=membership]").click();
  await page
    .locator("#user-search-form")
    .evaluate((el) => (el.closest("details").open = true));
  await page.locator("#user-search-form button").click();
  await page.locator("[data-user-recovery]").click();
  await page
    .locator("#issue-recovery-form [name=reason]")
    .fill("隔离测试人工核验");
  await page.locator("#issue-recovery-form button").click();
  await page.waitForSelector("#issued-recovery pre");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("#issued-recovery"));
  assert.equal(
    log.find((request) => request.path.endsWith("/password-recovery")).headers[
      "x-session-context"
    ],
    "admin",
  );
  await context.close();
});
await test("手机长文、相册、回复及昵称变更保持完整阅读", async () => {
  const { page, context, mode } = await setup({ dual: true, mobile: true });
  mode.longMessage = true;
  await page.locator(".mobile-bottom [data-page=experts]").click();
  await page.locator("[data-expert-tab=signals]").click();
  await page.locator("#signals-list [data-message]").first().click();
  await page.waitForFunction(
    () => document.querySelectorAll("#message-media img").length === 2,
  );
  assert.match(await page.locator("#dialog-title").innerText(), /改名后的专家/);
  assert.match(
    await page.locator(".raw-text").first().innerText(),
    /最终盘口仍是全场大2.5$/,
  );
  await page.locator(".trace-block").first().locator("summary").click();
  assert.match(
    await page.locator("#dialog-content").innerText(),
    /上一条讨论赛前比赛/,
  );
  assert.equal(await page.locator("#dialog-content .signal-card").count(), 2);
  assert.equal(
    await page
      .locator("#content-dialog")
      .evaluate((el) => el.scrollWidth > el.clientWidth),
    false,
  );
  await page.screenshot({
    path: evidence + "/candidate-mobile-long-album-fixture.png",
  });
  await context.close();
});
assert.deepEqual(caught, []);
await fs.writeFile(
  "/work/tests/browser/results.json",
  JSON.stringify(
    {
      date: "2026-10-26",
      environment: "isolated Docker / mock API / no real credentials or bets",
      results,
      browser_errors: caught,
    },
    null,
    2,
  ),
);
await browser.close();
await new Promise((resolve) => server.close(resolve));
if (results.some((r) => r.status === "failed")) process.exit(1);
