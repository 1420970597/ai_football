import { chromium } from "/opt/verification/node_modules/playwright/index.mjs";
import fs from "node:fs/promises";
const browser = await chromium.launch({
  headless: true,
  args: ["--disable-dev-shm-usage"],
});
const root = "/work/reports/jiebao-score-reference";
const observations = [];
for (const item of [
  {
    name: "desktop-scores",
    url: "https://live.nowscore.com/",
    viewport: { width: 1440, height: 960 },
  },
  {
    name: "mobile-scores",
    url: "https://m.nowscore.com/",
    viewport: { width: 390, height: 844 },
    isMobile: true,
  },
  {
    name: "desktop-home",
    url: "https://www.nowscore.com/",
    viewport: { width: 1440, height: 960 },
  },
  { name:"tablet-scores",url:"https://m.nowscore.com/",viewport:{width:768,height:1024},isMobile:true },
  { name:"desktop-match-detail",url:"https://live.nowscore.com/analysis/2995379.html",viewport:{width:1440,height:960} },
  { name:"mobile-membership",url:"https://guess.nowscore.com/vip",viewport:{width:390,height:844},isMobile:true },
]) {
  const context = await browser.newContext({
    viewport: item.viewport,
    isMobile: item.isMobile || false,
    deviceScaleFactor: 1,
  });
  const page = await context.newPage();
  try {
    await page.goto(item.url, {
      waitUntil: "domcontentloaded",
      timeout: 45000,
    });
    await page.waitForTimeout(5000);
    const measured = await page.evaluate(() => ({
      title: document.title,
      width: innerWidth,
      bodyWidth: document.body.scrollWidth,
      fonts: [
        ...new Set(
          [...document.querySelectorAll("body,nav,a,li,table")]
            .slice(0, 60)
            .map((x) => getComputedStyle(x).fontFamily),
        ),
      ],
      colors: [
        ...new Set(
          [...document.querySelectorAll("header,nav,a,td,li")]
            .slice(0, 150)
            .map((x) => getComputedStyle(x).backgroundColor),
        ),
      ].slice(0, 25),
      resources: performance
        .getEntriesByType("resource")
        .map((x) => x.name)
        .filter((x) => /\.(css|png|jpg|svg|woff)/.test(x))
        .slice(0, 60),
      body: document.body.innerText.slice(0, 2500),
    }));
    await page.screenshot({
      path: `${root}/images/${item.name}-verified-2026-10-26.png`,
      fullPage: false,
    });
    observations.push({ ...item, ...measured, status: "captured" });
  } catch (error) {
    observations.push({ ...item, status: "failed", error: error.message });
  }
  await context.close();
}
await fs.writeFile(
  `${root}/data/browser-observations.json`,
  JSON.stringify(observations, null, 2),
);
await browser.close();
console.log(
  JSON.stringify(
    observations.map(({ name, status, viewport, title }) => ({
      name,
      status,
      viewport,
      title,
    })),
  ),
);
