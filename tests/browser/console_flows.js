// Run with playwright-cli run-code --filename tests/browser/console_flows.js.
// Real browser UI, controlled API responses; no collection or LLM requests.
async page => {
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const requests = [];
  let failPricing = false;
  const decisions = ["a", "b"].map((id, i) => ({
    match_id: id, home: "主队" + id, away: "客队" + id, league: "联赛" + i,
    decision: "avoid", picks: [], computations: [], n_computed: 0, n_markets: 0,
    llm_confidence: 0, elapsed_ms: 10
  }));
  await page.route("**/api/v1/**", async route => {
    const url = new URL(route.request().url());
    requests.push(url);
    const path = url.pathname;
    let body = {};
    const rows = decisions.filter(d => !url.searchParams.get("q") ||
      d.home.includes(url.searchParams.get("q")));
    if (path.endsWith("/decisions")) body = {
      decisions: rows, summary: {n: rows.length, avoid: rows.length}, count: rows.length, llm: {available: false},
      age_s: 5, cycle: {rounds: 1}
    };
    if (path.includes("/trend/")) body = {markets: []};
    if (path.endsWith("/matches")) body = {matches: rows.map(d => ({
      ...d, latest_odds: {HAD: [2, 3, 4]}, state: "active"
    }))};
    if (path.includes("/fair/")) {
      if (failPricing) return route.fulfill({status: 503, json: {error: "unavailable"}});
      body = {market: "OU(2.5)", requested_market: "1X2", market_fallback: true,
        odds: [1.8, 2.1], outcomes: ["over", "under"], outcome_labels: ["全场进球数>2.5", "全场进球数<2.5"],
        method: url.searchParams.get("method"), margin: 0.03, probabilities: [0.53, 0.47],
        per_method: {power: [0.53, 0.47]}, method_spread_pp: 0.1, shin_z: null};
    }
    if (path.includes("/markets/")) body = {n_markets: 0, margins: {}, ev_if_fair: {}, markets: []};
    if (path.includes("/model/")) return route.fulfill({status: 404, json: {error: "no model"}});
    await route.fulfill({json: body});
  });
  await page.goto("http://localhost:13004");
  await page.waitForFunction(() => document.querySelectorAll("#tbody tr").length === 2);
  await page.locator("#tbody tr").first().click();
  assert(await page.locator("#decision-panel").isVisible(), "Decision detail is hidden");
  assert((await page.locator("#decision-tag").textContent()).includes("主队a"), "Wrong decision");
  await page.getByRole("button", {name: "返回列表", exact: true}).click();
  assert(await page.locator("#view-dashboard").isVisible(), "Back does not return to list");
  await page.locator("#tbody tr").nth(1).click();
  assert((await page.locator("#decision-tag").textContent()).includes("主队b"), "Old decision shown");
  await page.getByRole("button", {name: "赛事看板", exact: true}).click();
  assert(await page.locator("#view-dashboard").isVisible(), "Navigation stuck on detail");
  await page.locator("#f-q").fill("不存在");
  await page.locator("#f-q").press("Enter");
  await page.locator("#st-empty").waitFor({state: "visible"});
  assert(requests.at(-1).searchParams.get("q") === "不存在", "Enter ignores filters");
  await page.locator("#btn-reset").click();
  await page.waitForFunction(() => document.querySelector("#list-tag").textContent.includes("2 场"));
  await page.locator("#f-q").fill("主队b");
  await page.locator("#btn-load").click();
  await page.waitForFunction(() => document.querySelectorAll("#tbody tr").length === 1);
  await page.evaluate(() => loadList({silent: true}));
  assert(await page.locator("#tbody tr").count() === 1, "Refresh loses filters");
  await page.locator("#f-mode").selectOption("matches");
  await page.waitForFunction(() => document.querySelector("#tbody tr td").textContent === "b");
  assert(await page.locator("#tbl th").count() === 9, "Raw headers wrong");
  assert(await page.locator("#tbody tr td").count() === 9, "Raw columns wrong");
  await page.locator("#f-mode").selectOption("decisions");
  await page.waitForFunction(() => document.querySelector("#tbody tr td").textContent === "无机会");
  assert(await page.locator("#tbl th").count() === 11, "Decision headers wrong");
  await page.locator("#btn-reset").click();
  await page.locator("#f-mode").selectOption("matches");
  await page.waitForFunction(() => document.querySelectorAll("#tbody tr").length === 2 && document.querySelector("#tbody tr td").textContent === "a");
  await page.locator("#tbody tr").nth(1).click();
  await page.waitForFunction(() => document.querySelectorAll("#pricing-tbody tr").length === 2);
  assert((await page.locator("#pricing-tbody tr").first().textContent()).includes("1.80"), "Fallback odds wrong");
  await page.locator("#pricing-method").selectOption("power");
  await page.waitForFunction(() => document.querySelector("#pricing-kv").textContent.includes("power"));
  assert((await page.locator("#pricing-tag").textContent()).includes("主队b"), "Method changed match");
  await page.getByRole("button", {name: "赛事看板", exact: true}).click();
  await page.locator("#f-method").selectOption("shin");
  assert(await page.locator("#view-dashboard").isVisible(), "List method change navigates away");
  assert(await page.locator("#tbody tr").count() === 2, "List method change cleared the list");
  failPricing = true;
  await page.getByRole("button", {name: "定价对比", exact: true}).click();
  await page.locator("#pricing-method").selectOption("shin");
  await page.locator("#msg.err").waitFor({state: "visible"});
  assert(await page.locator("#pricing-tbody tr").count() === 0, "Failed pricing shows previous match");
  return "PASS: navigation; query/Enter/reset/refresh/headers; method selection/fallback/failure";
}
