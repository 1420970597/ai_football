async page => {
  await page.route("**/api/v1/ledger/history**", route => route.fulfill({json: {
    overall: {total: 1}, entries_total: 1, entries_shown: 1,
    settled: {graded: 1, won: 1, lost: 0, push: 0, hit_rate: 1,
      roi: 0.2, clv_mean: 0.1, clv_n: 1, stake_units: 1, pending: 0},
    entries: [{at: "2026-10-08T07:00:00Z", league: "L", home: "A", away: "B",
      market: "HAD", outcome: "home", label: "A胜", odds: 2.2, closing_odds: 2.0,
      clv: 0.1, status: "won", pnl: 1.2}],
    timeline: [], by_league: {}, by_market: {}, by_trigger: {}
  }}));
  await page.goto("http://localhost:13004");
  await page.getByRole("button", {name: "历史战绩", exact: true}).click();
  await page.waitForFunction(() => document.querySelector("#h-cards").textContent.includes("10.00%"));
  const card = await page.locator("#h-cards").textContent();
  if (!card.includes("买入赔率/收盘赔率−1")) throw new Error("Wrong CLV formula text: " + card);
  if (!(await page.locator("#h-entries-tbody").textContent()).includes("10.00%")) throw new Error("Detail CLV sign differs from summary");
  return "PASS: positive CLV shown consistently in history summary and detail";
}
