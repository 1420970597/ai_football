import assert from "node:assert/strict";
import { chromium } from "/opt/verification/node_modules/playwright/index.mjs";

const url = "http://web";
const endpoint = url + "/api/v1/admin/hermes-connection";
const chat = "http://hermes:8642/v1/chat/completions";
const provider = "http://provider-fixture:8999";
const gatewayToken = "isolated-hermes-gateway-token-xyz-890";
const auth = { Authorization: "Bearer " + gatewayToken, "Content-Type": "application/json" };
const requestChat = (model = "hermes-agent", provider) =>
  fetch(chat, { method: "POST", headers: auth, body: JSON.stringify({
    model, provider, messages: [{ role: "user", content: "Return the fixture label." }],
  }) });
const actual = async (...args) => {
  const response = await requestChat(...args);
  const payload = await response.json();
  assert.equal(response.status, 200, JSON.stringify(payload));
  return payload.choices[0].message.content;
};

if (process.env.VERIFY_RESTART === "1") {
  assert.equal(await actual(), "v1:fixture-same-address");
  console.log("persisted connection restored after Hermes restart");
  process.exit(0);
}

assert.equal((await fetch(endpoint)).status, 401);
assert.equal((await fetch("http://hermes:8642/project/connection", { headers: auth })).status, 401);
assert.equal(await actual(), "v1:fixture-baseline");

const browser = await chromium.launch({ headless: true, args: ["--disable-dev-shm-usage"] });
try {
  for (const width of [1440, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 950 } });
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(url);
    await page.locator(".utility-bar [data-page=admin]").click();
    await page.locator("#admin-identity [data-login=admin]").click();
    await page.locator("#auth-form [name=email]").fill("fixture-admin");
    await page.locator("#auth-form [name=password]").fill("isolated-connection-admin-pass");
    await page.locator("#auth-form button").click();
    await page.locator("[data-admin-tab=system]").click();
    const form = page.locator("#hermes-connection-form");
    const state = form.getByRole("status");
    const key = form.getByLabel("API 密钥");
    const model = form.getByLabel("默认模型");
    const address = form.getByLabel("服务地址");
    const save = form.getByRole("button", { name: "验证并保存" });
    await save.waitFor();
    await page.waitForFunction(() => !document.querySelector("#hermes-connection-fields").disabled);
    const initial = await (await page.request.get(endpoint)).json();
    assert.equal(initial.key_configured, true);
    assert.equal(await key.inputValue(), "");

    const update = (revision, api_key = "") => page.evaluate(async ({ revision, api_key }) => {
      const response = await fetch("/api/v1/admin/hermes-connection", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision, api_key, model: "fixture-prevented",
          base_url: "http://provider-fixture:8999/v1" }),
      });
      return response.status;
    }, { revision, api_key });
    assert.equal(await update(initial.revision, "fixture-key-one"), 403);

    if (width === 1440) {
      await address.fill(provider + "/next");
      await model.fill("fixture-hot-model");
      await key.fill("invalid-fixture-key");
      await save.click();
      await state.getByText(/HTTP 401/).waitFor();
      assert.equal(await actual(), "v1:fixture-baseline");
      await key.fill("fixture-key-two");
      await save.click();
      await state.getByText(/版本 1 · 已生效/).waitFor();
      assert.equal(await actual(), "next:fixture-hot-model");
      assert.equal(await actual("fixture-explicit-model", "custom:cline"), "next:fixture-explicit-model");
      const csrf = (await page.context().cookies()).find(cookie => cookie.name === "v2_admin_csrf").value;
      const stale = await page.request.put(endpoint, { headers: { "X-CSRF-Token": csrf },
        data: { revision: 0, base_url: provider + "/next", model: "fixture-stale" } });
      assert.equal(stale.status(), 409);
    } else {
      assert.match(await state.textContent(), /版本 1 · 已生效/);
      await model.fill("fixture-key-preserved");
      await save.click();
      await state.getByText(/版本 2 · 已生效/).waitFor();
      assert.equal(await actual(), "next:fixture-key-preserved");
      await form.getByRole("button", { name: "清除密钥" }).click();
      await page.locator("#confirm-yes").click();
      await state.getByText(/版本 3 · 密钥未配置/).waitFor();
      assert.ok((await requestChat()).status >= 400);
      await model.fill("fixture-restored-model");
      await key.fill("fixture-key-two");
      await save.click();
      await state.getByText(/版本 4 · 已生效/).waitFor();
      assert.equal(await actual(), "next:fixture-restored-model");
      await address.fill(provider + "/v1");
      await model.fill("fixture-same-address");
      await key.fill("fixture-key-two");
      await save.click();
      await state.getByText(/版本 5 · 已生效/).waitFor();
      assert.equal(await actual(), "v1:fixture-same-address");
      assert.equal((await requestChat()).headers.get("x-hermes-connection-revision"), "5");
      assert.ok((await requestChat("hermes-agent", "openai")).status >= 400);
      assert.equal(await actual("fixture-same-address", "custom:cline"), "v1:fixture-same-address");
    }
    const response = await page.request.get(endpoint);
    assert.match(response.headers()["cache-control"], /no-store/);
    assert.doesNotMatch(await response.text(), /fixture-key-(one|two)/);
    assert.equal(await key.inputValue(), "");
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    await page.close();
    console.log(`${width}px administrator settings passed`);
  }
} finally {
  await browser.close();
}
