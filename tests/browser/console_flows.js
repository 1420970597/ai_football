// Run with playwright-cli run-code --filename tests/browser/console_flows.js.
// Real browser UI, controlled API responses; no collection or LLM requests.
async page => {
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const decisions = ["a", "b"].map((id, i) => ({
    match_id: id, home: "主队" + id, away: "客队" + id, league: "联赛" + i,
    decision: "avoid", picks: [], computations: [], n_computed: 0, n_markets: 0,
    llm_confidence: 0, elapsed_ms: 10
  }));
  await page.route("**/api/v1/**", async route => {
    const path = new URL(route.request().url()).pathname;
    let body = {};
    if (path.endsWith("/decisions")) body = {
      decisions, summary: {n: 2, avoid: 2}, count: 2, llm: {available: false},
      age_s: 5, cycle: {rounds: 1}
    };
    if (path.includes("/trend/")) body = {markets: []};
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
  return "PASS: decision open/back/reselect/navigation";
}
