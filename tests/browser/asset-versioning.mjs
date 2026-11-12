// Real nginx artifacts plus a deliberately stale, path-keyed edge cache.
// APIs are synthetic and all writes are blocked; no real accounts are used.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import { chromium } from '/opt/verification/node_modules/playwright/index.mjs';
import { fixtureRoute } from './visual-fixture.mjs';

const output = process.env.BROWSER_EVIDENCE_DIR || '/evidence';
const bases = [process.env.ASSET_BASE_A, process.env.ASSET_BASE_B];
assert.ok(bases.every(Boolean), 'two isolated nginx origins required');
const versionFrom = html => {
  const paths = [...html.matchAll(/(?:href|src)="(\/assets\/v([a-f0-9]{64})\/(?:app.css|app.js))"/g)];
  assert.equal(paths.length, 2);
  assert.equal(paths[0][2], paths[1][2]);
  return paths[0][2];
};
const versions = [], results = [], errors = [], writes = [];
const browser = await chromium.launch({ headless: true, args: ['--disable-dev-shm-usage'] });
const client = await browser.newContext();
let origin = bases[0];
const cache = new Map();
const edge = http.createServer(async (req, res) => {
  try {
    const cached = cache.get(req.url);
    if (cached) {
      res.writeHead(200, { ...cached.headers, 'x-isolated-edge-cache': 'HIT' }).end(cached.body);
      return;
    }
    const upstream = await fetch(origin + req.url);
    const body = Buffer.from(await upstream.arrayBuffer());
    const headers = {};
    for (const key of ['content-type', 'cache-control', 'etag', 'x-content-type-options']) {
      const value = upstream.headers.get(key);
      if (value) headers[key] = value;
    }
    if (req.url.startsWith('/assets/') && upstream.ok) cache.set(req.url, { headers, body });
    res.writeHead(upstream.status, { ...headers, 'x-isolated-edge-cache': 'MISS' }).end(body);
  } catch {
    res.writeHead(502).end();
  }
});
await new Promise(resolve => edge.listen(8081, '127.0.0.1', resolve));
try {
  for (const base of bases) {
    const htmlResponse = await client.request.get(base + '/');
    assert.equal(htmlResponse.status(), 200);
    assert.equal(htmlResponse.headers()['cache-control'], 'no-cache');
    const html = await htmlResponse.text(), version = versionFrom(html), prefix = '/assets/v' + version + '/';
    versions.push(version);
    const queue = [prefix + 'app.js', prefix + 'app.css', prefix + 'qrcode.js', prefix + 'qrcode.LICENSE'];
    const seen = new Set();
    while (queue.length) {
      const pathname = queue.shift();
      if (seen.has(pathname)) continue;
      seen.add(pathname);
      assert.ok(pathname.startsWith(prefix), 'module graph escaped its version: ' + pathname);
      const response = await client.request.get(base + pathname);
      assert.equal(response.status(), 200, pathname);
      assert.match(response.headers()['cache-control'], /max-age=31536000, immutable/);
      if (/\.(?:js|css)$/.test(pathname)) {
        const text = await response.text();
        for (const match of text.matchAll(/(?:from\s*|import\s*\()\s*['"](\.\/[^'"]+\.js)['"]/g)) {
          queue.push(new URL(match[1], base + pathname).pathname);
        }
        for (const match of text.matchAll(/['"](\/assets\/[^'"]+)['"]/g)) {
          assert.ok(match[1].startsWith(prefix), 'unversioned absolute resource: ' + match[1]);
          queue.push(match[1]);
        }
      }
    }
    for (const required of ['admin.js', 'identity.js', 'account-history.js', 'ui.js', 'qrcode.js',
      'cc-reference/home_match_vs_icon.png', 'cc-reference/home_announcement_icon.png']) {
      assert.ok(seen.has(prefix + required), 'missing dependency ' + required);
    }
    const css = await client.request.get(base + prefix + 'app.css');
    assert.equal((await client.request.get(base + prefix + 'app.css', {
      headers: { 'If-None-Match': css.headers().etag },
    })).status(), 304);
    assert.equal((await client.request.get(base + prefix + 'app.js', {
      headers: { 'Accept-Encoding': 'gzip' },
    })).headers()['content-encoding'], 'gzip');
    assert.equal((await client.request.get(base + '/assets/app.js')).headers()['cache-control'], 'no-cache');
    for (const pathname of [prefix + 'missing.js', '/assets/v' + '0'.repeat(64) + '/app.css', '/assets/vwrong/app.js']) {
      const response = await client.request.get(base + pathname);
      assert.equal(response.status(), 404);
      assert.equal(response.headers()['cache-control'], 'no-store');
    }
    results.push({ origin: base, version, dependencies: seen.size, headers_and_missing_paths: 'passed' });
  }
  assert.notEqual(versions[0], versions[1], 'CSS-only deployment must change the whole graph version');
  for (const width of [1440, 390]) {
    cache.clear();
    const context = await browser.newContext({ viewport: { width, height: 960 }, serviceWorkers: 'block' });
    const page = await context.newPage();
    const loaded = [], failed = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('response', response => {
      if (new URL(response.url()).pathname.startsWith('/assets/')) {
        loaded.push(response.url());
        if (!response.ok()) failed.push({ url: response.url(), status: response.status() });
      }
    });
    await page.route('**/api/v1/**', route => {
      if (route.request().method() !== 'GET') {
        writes.push(route.request().method() + ' ' + route.request().url());
        return route.abort();
      }
      return fixtureRoute(route);
    });
    for (let generation = 0; generation < 2; generation++) {
      origin = bases[generation];
      loaded.length = 0;
      // A document navigation models a user loading the post-deploy HTML;
      // changing only the fragment would keep the previous document alive.
      await page.goto('http://127.0.0.1:8081/?generation=' + generation + '#scores');
      await page.waitForLoadState('networkidle');
      await page.locator('.match-row').first().waitFor();
      // Exercise lazy ESM and classic QR dependencies from their actual build.
      await page.goto('http://127.0.0.1:8081/?generation=' + generation + '#admin');
      await page.waitForLoadState('networkidle');
      await page.evaluate(async version => {
        await import('/assets/v' + version + '/admin.js');
        await new Promise((resolve, reject) => {
          const script = document.createElement('script');
          script.src = '/assets/v' + version + '/qrcode.js';
          script.onload = resolve; script.onerror = reject;
          document.head.append(script);
        });
        if (typeof window.qrcode !== 'function') throw Error('QR library did not load');
      }, versions[generation]);
      assert.ok(loaded.length >= 8, 'module graph did not load: ' + JSON.stringify({ width, generation, loaded }));
      assert.ok(loaded.every(url => new URL(url).pathname.startsWith('/assets/v' + versions[generation] + '/')),
        'old resources leaked across deployment');
      results.push({ width, generation, version: versions[generation], asset_responses: loaded.length, stale_cache_mix: false });
    }
    assert.ok([...cache.keys()].some(key => key.includes(versions[0])), 'old edge objects must remain cached');
    assert.ok([...cache.keys()].some(key => key.includes(versions[1])), 'new edge objects were not requested');
    assert.deepEqual(failed, []);
    await context.close();
  }
  assert.deepEqual(errors, []);
  assert.deepEqual(writes, []);
  await fs.writeFile(output + '/asset-versioning-results.json', JSON.stringify({ status: 'passed',
    synthetic_data: true, production_evidence: false, versions, results, errors, writes }, null, 2) + '\n');
  console.log('PASS actual nginx: two generations, complete resource graph, ETag/gzip/cache/404, stale edge at 1440/390');
} finally {
  await client.close();
  await browser.close();
  await new Promise(resolve => edge.close(resolve));
}
