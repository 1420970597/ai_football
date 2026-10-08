// Run with playwright-cli run-code --filename tests/browser/collection_status.js.
// Serve the console on localhost:13017; all API traffic is intercepted locally.
async page => {
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  let rt = {connected: 0, subscribed: 0, last_error: "6001 token已过期 private-marker",
    live_book: {fresh_rows: 0}};
  let failStatus = false;
  let pending = false;
  let release;
  const firstResponse = new Promise(resolve => { release = resolve; });
  let first = true;
  const decisions = [{match_id: "a", home: "主队", away: "客队", league: "测试联赛",
    decision: "avoid", picks: [], computations: [], n_computed: 1, n_markets: 1}];
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/realtime")) {
      if (failStatus) return route.fulfill({status: 503, json: {error: "unavailable"}});
      return route.fulfill({json: rt});
    }
    if (path.endsWith("/decisions")) {
      if (first) { first = false; await firstResponse; }
      return route.fulfill({json: pending ? {pending: true, decisions: []} : {
        decisions, count: 1, summary: {n: 1, avoid: 1}, llm: {}, age_s: 5, stale: false
      }});
    }
    return route.fulfill({json: {}});
  });
  await page.goto("http://localhost:13017");
  await page.waitForFunction(() => document.getElementById("collection-status").textContent.includes("已过期"));
  assert(await page.locator("#st-loading").isVisible(), "First load has no loading state");
  release();
  await page.waitForFunction(() => document.querySelectorAll("#tbody tr").length === 1);
  assert((await page.locator("#collection-status").textContent()).includes("已保存数据"), "Expired session hides retained data semantics");
  assert(!(await page.locator("#collection-status").textContent()).includes("private-marker"), "Raw provider error exposed");
  assert((await page.locator("#decision-summary").textContent()).includes("分析结果生成于"), "Compute age mislabeled as quote freshness");
  assert(!(await page.locator("#decision-summary").textContent()).includes("数据新鲜"), "Historical rerun claims fresh quotes");
  assert((await page.locator("#cards").textContent()).includes("行情订阅0实时推送未连接"), "Disconnected status claims live match count");

  rt = {connected: 1, subscribed: 2, live_book: {fresh_rows: 0}};
  await page.evaluate(() => loadCollectionStatus());
  assert((await page.locator("#collection-status").textContent()).includes("尚未收到"), "Connected without quotes claims fresh data");
  rt.live_book.fresh_rows = 4;
  await page.evaluate(() => loadCollectionStatus());
  assert((await page.locator("#collection-status").textContent()).includes("订阅 2 场"), "Healthy subscription count incorrect");
  assert(!(await page.locator("#collection-status").getAttribute("class")).includes("err"), "Recovered connection keeps error banner");

  rt = {connected: 0, subscribed: 0, live_book: {fresh_rows: 0}};
  await page.evaluate(() => loadCollectionStatus());
  assert((await page.locator("#collection-status").textContent()).includes("不能用于判断"), "No connection treated as zero matches");
  failStatus = true;
  await page.evaluate(() => loadCollectionStatus());
  assert((await page.locator("#cards").textContent()).includes("行情订阅未知"), "Failed status preserves previous success");
  assert(await page.locator("#tbody tr").count() === 1, "Status failure removed saved decisions");

  pending = true;
  await page.evaluate(() => loadDecisions());
  assert((await page.locator("#st-empty-title").textContent()).includes("正在准备"), "Pending confused with no matches");
  assert(!(await page.locator("#btn-empty-reset").isVisible()), "Pending suggests changing dates");
  return "PASS: loading, expired session, retained data, compute age, subscriptions, no quotes, recovery, unknown status, pending";
}
