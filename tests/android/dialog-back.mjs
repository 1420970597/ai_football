// Executes the exact bundled Android script against real Chromium HTML dialogs.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import { createRequire } from 'node:module';
const require = createRequire('/opt/verification/package.json');
const { chromium } = require('playwright');
const source = await fs.readFile('/source/android/app/src/main/res/raw/close_dialog.js', 'utf8');
const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
const page = await browser.newPage();
const results = [];
async function check(name, run) {
  await page.setContent('<main><button id="base">Base</button><dialog id="one"><button>One</button></dialog><dialog id="two"><button>Two</button></dialog></main>');
  await page.evaluate(() => {
    window.events = [];
    for (const id of ['one','two']) {
      const dialog = document.getElementById(id);
      dialog.addEventListener('cancel', () => window.events.push(id + ':cancel'));
      dialog.addEventListener('close', () => { window.events.push(id + ':close'); dialog.dataset.cleaned = 'true'; });
    }
  });
  await run();
  results.push({name, passed: true});
}
try {
  await check('no-dialog-allows-history', async () => assert.equal(await page.evaluate(source), 'none'));
  await check('modal-cancel-then-close-runs-cleanup', async () => {
    await page.evaluate(() => document.getElementById('one').showModal());
    assert.equal(await page.evaluate(source), 'handled');
    await page.waitForFunction(() => document.getElementById('one').dataset.cleaned === 'true');
    assert.deepEqual(await page.evaluate(() => window.events), ['one:cancel','one:close']);
    assert.equal(await page.evaluate(source), 'none');
  });
  await check('prevented-cancel-consumes-back-without-close', async () => {
    await page.evaluate(() => { const dialog=document.getElementById('one'); dialog.addEventListener('cancel', e=>e.preventDefault()); dialog.showModal(); });
    assert.equal(await page.evaluate(source), 'handled');
    assert.equal(await page.locator('#one').evaluate(d => d.open), true);
    assert.deepEqual(await page.evaluate(() => window.events), ['one:cancel']);
  });
  await check('top-modal-only-lower-dialog-preserved', async () => {
    await page.evaluate(() => { document.getElementById('one').showModal(); document.getElementById('two').showModal(); });
    assert.equal(await page.evaluate(source), 'handled');
    await page.waitForFunction(() => document.getElementById('two').dataset.cleaned === 'true');
    assert.equal(await page.locator('#one').evaluate(d => d.open), true);
    assert.deepEqual(await page.evaluate(() => window.events), ['two:cancel','two:close']);
  });
  await check('cancel-handler-may-close-without-double-close', async () => {
    await page.evaluate(() => { const dialog=document.getElementById('one'); dialog.addEventListener('cancel', ()=>dialog.close('page-cleanup')); dialog.showModal(); });
    assert.equal(await page.evaluate(source), 'handled');
    await page.waitForFunction(() => document.getElementById('one').dataset.cleaned === 'true');
    assert.deepEqual(await page.evaluate(() => window.events), ['one:cancel','one:close']);
    assert.equal(await page.locator('#one').evaluate(d=>d.returnValue), 'page-cleanup');
  });
  await check('non-modal-open-dialog-also-closes', async () => {
    await page.evaluate(() => { document.getElementById('one').show(); document.getElementById('base').focus(); });
    assert.equal(await page.evaluate(source), 'handled');
    await page.waitForFunction(() => document.getElementById('one').dataset.cleaned === 'true');
  });
  console.log(JSON.stringify({passed: results.length, results, scope:'Bundled script in Chromium; not Android WebView device acceptance'}));
} finally { await browser.close(); }
