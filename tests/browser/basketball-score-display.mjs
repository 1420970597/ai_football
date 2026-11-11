// Synthetic basketball score display contract; no provider or write endpoint.
import { chromium } from "/opt/verification/node_modules/playwright/index.mjs";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import http from "node:http";
import path from "node:path";

const output = process.env.BROWSER_EVIDENCE_DIR || "/evidence";
const instant = "2026-11-10T11:11:00Z";
const basketball = {
  id: "basketball-display-match",
  sport: "basketball",
  competition_id: "basketball-league",
  competition_name: "隔离篮球联赛",
  home_team: "篮球主队",
  away_team: "篮球客队",
  phase: "live",
  match_state: "active",
  kickoff_at: instant,
  source_updated_at: instant,
  home_score: 72,
  away_score: 68,
  half_home_score: 36,
  half_away_score: 32,
  clock_minute: 27,
};
const market = {
  quote_id: "basketball-display-quote",
  market_id: "39",
  market_name: "全场让分",
  market_kind: "handicap",
  period: "full_time",
  line: "-4.5",
  selection: "home",
  price: "1.91",
  status: "active",
  is_main: true,
  fresh: true,
  source_updated_at: instant,
};

const server = http.createServer(async (request, response) => {
  try {
    const requestPath = new URL(request.url, "http://localhost").pathname;
    const asset = requestPath.startsWith("/assets/")
      ? requestPath.slice(8)
      : "index.html";
    const target = path.resolve("/work/web/app", asset);
    if (!target.startsWith("/work/web/app/")) throw new Error("invalid path");
    const raw = await fs.readFile(target);
    const type = target.endsWith(".js")
      ? "text/javascript"
      : target.endsWith(".css")
        ? "text/css"
        : "text/html";
    response.writeHead(200, { "Content-Type": type }).end(raw);
  } catch {
    response.writeHead(404).end();
  }
});

await fs.mkdir(output, { recursive: true });
await new Promise((resolve) => server.listen(8080, "127.0.0.1", resolve));
const browser = await chromium.launch({
  headless: true,
  args: ["--disable-dev-shm-usage"],
});
const results = [];
const errors = [];
try {
  for (const width of [1440, 390]) {
    const context = await browser.newContext({
      viewport: { width, height: 960 },
      locale: "zh-CN",
    });
    const page = await context.newPage();
    const reads = [];
    const writes = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.route("**/api/v1/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const endpoint = url.pathname.replace("/api/v1", "");
      if (request.method() !== "GET") writes.push(endpoint);
      if (endpoint === "/auth/me" || endpoint === "/admin/me")
        return route.fulfill({ status: 401, json: { detail: "尚未登录" } });
      if (endpoint === "/public/matches") {
        const sport = url.searchParams.get("sport");
        reads.push({ endpoint, sport });
        if (sport === "basketball")
          return route.fulfill({
            json: {
              items: [{ ...basketball, markets: [market], quote_fresh: true }],
              next_cursor: null,
              markets_included: true,
              server_time: instant,
            },
          });
        return route.fulfill({
          json: {
            items: [],
            next_cursor: null,
            markets_included: true,
            server_time: instant,
          },
        });
      }
      if (endpoint === "/public/leagues")
        return route.fulfill({
          json: {
            items: [
              {
                competition_id: basketball.competition_id,
                competition_name: basketball.competition_name,
                match_count: 1,
              },
            ],
          },
        });
      if (endpoint === "/public/matches/" + basketball.id) {
        // Exercise the loading state before the response is rendered.
        await new Promise((resolve) => setTimeout(resolve, 150));
        return route.fulfill({
          json: { fixture: basketball, markets: [market], expert_signals: [] },
        });
      }
      if (endpoint === "/public/experts")
        return route.fulfill({ json: { items: [], source_options: [] } });
      if (endpoint === "/public/signals")
        return route.fulfill({ json: { items: [], next_cursor: null, limited: true } });
      return route.fulfill({ status: 404, json: { detail: "未包含此隔离接口" } });
    });

    await page.goto("http://127.0.0.1:8080/#scores?scope=all");
    const basketballMatches = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        url.pathname === "/api/v1/public/matches" &&
        url.searchParams.get("sport") === "basketball"
      );
    });
    await page.locator('.sports-bar [data-sport="basketball"]').click();
    await basketballMatches;
    await page.locator('.match-row[data-sport="basketball"]').waitFor();
    assert.equal(await page.locator("#score-title").textContent(), "篮球即时比分");
    assert.match(await page.locator("#sport-context").textContent(), /篮球比分与盘口/);
    assert.match(await page.locator(".match-row").innerText(), /半场 36 : 32/);
    assert.match(await page.locator(".match-row").innerText(), /全场让分/);
    assert.match(await page.locator(".match-row").innerText(), /篮球主队/);
    assert.match(page.url(), /sport=basketball/);
    await page.locator('.match-row[data-sport="basketball"] .score-button').click();
    await page.locator("#content-dialog").waitFor({ state: "visible" });
    await page.locator("#dialog-content .detail-hero").waitFor({ state: "visible" });
    assert.match(await page.locator("#dialog-content").innerText(), /篮球/);
    assert.match(await page.locator("#dialog-content").innerText(), /半场比分 36 : 32/);
    assert.match(await page.locator("#dialog-content").innerText(), /全场让分/);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({ path: `${output}/basketball-score-${width}.png` });
    assert.deepEqual(writes, []);
    assert.ok(reads.some((item) => item.sport === "basketball"));
    results.push({ width, passed: true, basketball_score: true, production_writes: 0 });
    await context.close();
  }
} catch (error) {
  results.push({ passed: false, category: error.name, message: error.message });
  process.exitCode = 1;
} finally {
  await browser.close();
  await new Promise((resolve) => server.close(resolve));
  await fs.writeFile(
    `${output}/basketball-score-display-results.json`,
    JSON.stringify(
      { results, errors, synthetic_data: true, production_writes: 0 },
      null,
      2,
    ) + "\n",
  );
}
console.log(JSON.stringify(results));
