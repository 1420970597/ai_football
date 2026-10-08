// Controlled quote changes in a real browser; API alignment tested in unittest.
async page => {
  let changed = false;
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body = {pending: true};
    if (path.endsWith("/board")) body = {
      count: 1, decided: 1, live: 1, live_known: true, buy: 0,
      matches: [{match_id: "a", home: "A", away: "B", decided: true,
        decision_stale: changed, is_live: true, picks: [], markets: [{
          market: "HAD", label: "全场胜平负", outcomes: ["home", "draw", "away"],
          odds: [changed ? 2.2 : 2.0, 3.1, 3.4], decision_odds: [2.0, 3.1, 3.4],
          quote_available: true, quote_source: "live", decided: true,
          decision_stale: changed, decided_at: "2026-10-08T07:00:00Z",
          p_fair: changed ? [] : [0.5, 0.3, 0.2], edges: changed ? [] : [0.1, -0.1, -0.2],
          gates: changed ? [] : [{passed: true}, {passed: false}, {passed: false}]
        }]}]
    };
    await route.fulfill({json: body});
  });
  await page.goto("http://localhost:13004");
  await page.getByRole("button", {name: "实时盘口", exact: true}).click();
  await page.locator("#board-body summary").click();
  await page.locator("#board-body table").waitFor({state: "visible"});
  changed = true;
  await page.getByRole("button", {name: "立即刷新", exact: true}).click();
  await page.waitForFunction(() => document.querySelector("#board-body").textContent.includes("决策已失效"));
  if (!await page.locator("#board-body table").isVisible()) throw new Error("Refresh collapses market details");
  const text = await page.locator("#board-body table tbody tr").first().textContent();
  if (!text.includes("2.200") || !text.includes("2.000") || !text.includes("待重算")) {
    throw new Error("Current and decision prices are not distinguished: " + text);
  }
  return "PASS: changed quote labeled; old gate hidden; expanded details preserved";
}
