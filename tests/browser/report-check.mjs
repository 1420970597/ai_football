import { chromium } from "/opt/verification/node_modules/playwright/index.mjs";
import fs from "node:fs/promises";
import path from "node:path";
import assert from "node:assert/strict";
import http from "node:http";
import crypto from "node:crypto";

const root = "/work/reports/jiebao-score-reference";
const compiler = "/opt/verification/node_modules/mermaid/dist";
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, "http://localhost");
    const isCompiler = url.pathname.startsWith("/compiler/");
    const base = isCompiler ? compiler : root;
    const relative = isCompiler
      ? url.pathname.slice(10)
      : url.pathname === "/"
        ? "REPORT.html"
        : url.pathname.slice(1);
    const target = path.resolve(base, relative);
    if (!target.startsWith(base + "/")) throw new Error("Invalid path");
    const body = await fs.readFile(target);
    const type =
      target.endsWith(".mjs") || target.endsWith(".js")
        ? "text/javascript"
        : target.endsWith(".svg")
          ? "image/svg+xml"
          : target.endsWith(".png")
            ? "image/png"
            : target.endsWith(".html")
              ? "text/html"
              : "text/plain";
    res.writeHead(200, { "Content-Type": type });
    res.end(body);
  } catch {
    res.writeHead(404).end();
  }
});
await new Promise((resolve) => server.listen(8090, "127.0.0.1", resolve));
const browser = await chromium.launch({
  headless: true,
  args: ["--disable-dev-shm-usage"],
});
const external = [];
const errors = [];
const context = await browser.newContext();
await context.route("**/*", (route) => {
  const url = new URL(route.request().url());
  if (
    url.hostname !== "127.0.0.1" &&
    !["data:", "file:"].includes(url.protocol)
  ) {
    external.push(url.href);
    return route.abort();
  }
  return route.continue();
});
const page = await context.newPage();
page.on("pageerror", (error) => errors.push(error.message));
if (process.argv[2] === "compile") {
  const markdown = await fs.readFile(root + "/REPORT.md", "utf8");
  const diagrams = [...markdown.matchAll(/```mermaid\r?\n([\s\S]*?)```/g)].map(
    (match) => match[1].trim(),
  );
  await page.goto("http://127.0.0.1:8090/");
  const version = JSON.parse(
    await fs.readFile(
      "/opt/verification/node_modules/mermaid/package.json",
      "utf8",
    ),
  ).version;
  const evidence = [];
  for (let i = 0; i < diagrams.length; i++) {
    const source = diagrams[i];
    let svg = await page.evaluate(
      async ({ source, i }) => {
        const { default: mermaid } = await import(
          "/compiler/mermaid.esm.min.mjs"
        );
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: "strict",
          fontFamily: "Arial, sans-serif",
        });
        const result = await mermaid.render("report-flow-" + i, source);
        return result.svg;
      },
      { source, i },
    );
    assert.match(svg, /<svg/);
    const viewBox = svg.match(/viewBox="[^"]*?([\d.]+) ([\d.]+)"/);
    assert.ok(viewBox, "Generated diagram needs fixed viewBox dimensions");
    const dimensions = svg
      .match(/viewBox="([^"]+)"/)[1]
      .split(/\s+/)
      .map(Number);
    svg = svg.replace(
      /(<svg\b[^>]*?)width="100%"/,
      `$1width="${Math.ceil(dimensions[2])}"`,
    );
    await fs.writeFile(root + "/images/flow-" + (i + 1) + ".svg", svg + "\n");
    await fs.writeFile(root + "/data/flow-" + (i + 1) + ".mmd", source + "\n");
    evidence.push({
      index: i + 1,
      compiler: "mermaid",
      version,
      source_sha256: crypto.createHash("sha256").update(source).digest("hex"),
      svg_sha256: crypto
        .createHash("sha256")
        .update(svg + "\n")
        .digest("hex"),
    });
  }
  assert.ok(diagrams.length > 0);
  await fs.writeFile(
    root + "/data/diagram-build.json",
    JSON.stringify(evidence, null, 2) + "\n",
  );
  console.log("Compiled offline diagrams: " + diagrams.length);
} else {
  const results = [];
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 960 });
    await page.goto("http://127.0.0.1:8090/", { waitUntil: "networkidle" });
    const images = await page.evaluate(async () => {
      await Promise.all(
        [...document.images].map((image) => image.decode().catch(() => {})),
      );
      return [...document.images].map((image) => ({
        src: image.getAttribute("src"),
        loaded: image.complete && image.naturalWidth > 0,
        width: image.naturalWidth,
      }));
    });
    assert.ok(images.length > 15);
    assert.ok(
      images.every((image) => image.loaded),
      JSON.stringify(images.filter((image) => !image.loaded)),
    );
    assert.equal(
      await page.evaluate(() => document.body.scrollWidth > innerWidth),
      false,
    );
    assert.ok((await page.locator(".diagram-scroll img").count()) > 0);
    const pan = await page.locator(".diagram-scroll").evaluate((element) => {
      element.scrollLeft = element.scrollWidth;
      const moved = element.scrollLeft > 0;
      element.scrollLeft = 0;
      return moved;
    });
    assert.equal(
      pan,
      true,
      "Flow diagram must be readable by local horizontal scrolling",
    );
    await page.locator("details").first().locator("summary").click();
    assert.match(
      await page.locator(".mermaid-source").innerText(),
      /flowchart LR/,
    );
    await page.locator(".diagram-scroll").scrollIntoViewIfNeeded();
    await page.screenshot({
      path: root + "/images/report-offline-" + width + "-verified.png",
    });
    results.push({
      width,
      images_loaded: images.length,
      diagram_loaded: true,
      source_readable: true,
      body_overflow: false,
    });
  }
  await page.goto("file://" + root + "/REPORT.html", { waitUntil: "load" });
  const fileImages = await page.evaluate(async () => {
    await Promise.all(
      [...document.images].map((image) => image.decode().catch(() => {})),
    );
    return [...document.images].every(
      (image) => image.complete && image.naturalWidth > 0,
    );
  });
  assert.equal(fileImages, true);
  results.push({
    mode: "direct_file",
    width: 390,
    images_loaded: await page.locator("img").count(),
    diagram_loaded: true,
    source_readable: true,
  });
  assert.deepEqual(external, []);
  assert.deepEqual(errors, []);
  await fs.writeFile(
    "/work/tests/browser/report-results.json",
    JSON.stringify(
      {
        recorded_at: new Date().toISOString(),
        environment:
          "Docker network none; local report server; no external requests",
        results,
        external_requests: external,
        browser_errors: errors,
      },
      null,
      2,
    ) + "\n",
  );
  console.log("Offline report verified: " + JSON.stringify(results));
}
await browser.close();
await new Promise((resolve) => server.close(resolve));
