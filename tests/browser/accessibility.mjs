import fs from "node:fs/promises";
import assert from "node:assert/strict";

// Shares only the existing isolated API fixtures. Never records page HTML,
// input values, MFA seeds, QR content, passwords or recovery codes.
export async function runAudit({ setup, id, caught, evidence }) {
  const baseline = process.argv.includes("--baseline");
  const axeSource = await fs.readFile(
    "/opt/verification/node_modules/axe-core/axe.min.js",
    "utf8",
  );
  const scans = [],
    checks = [];
  async function check(name, action) {
    try {
      const detail = await action();
      checks.push({ name, status: "passed", ...(detail ? { detail } : {}) });
      console.log("PASS " + name);
    } catch (error) {
      checks.push({ name, status: "failed", error: error.message });
      console.log("FAIL " + name + " " + error.message);
    }
  }
  async function scan(page, name) {
    await page.addScriptTag({ content: axeSource });
    const result = await page.evaluate(async () => {
      const data = await window.axe.run(document, {
        runOnly: {
          type: "tag",
          values: [
            "wcag2a",
            "wcag2aa",
            "wcag21aa",
            "wcag22aa",
            "best-practice",
          ],
        },
      });
      const luminance = (color) =>
        color
          .match(/[\d.]+/g)
          .slice(0, 3)
          .map(Number)
          .map((value) => {
            const unit = value / 255;
            return unit <= 0.04045
              ? unit / 12.92
              : ((unit + 0.055) / 1.055) ** 2.4;
          })
          .reduce(
            (total, value, index) =>
              total + value * [0.2126, 0.7152, 0.0722][index],
            0,
          );
      const contrast = (a, b) => {
        const pair = [luminance(a), luminance(b)].sort((a, b) => b - a);
        return +((pair[0] + 0.05) / (pair[1] + 0.05)).toFixed(2);
      };
      const colorReview = data.incomplete
        .filter((rule) => rule.id === "color-contrast")
        .flatMap((rule) => rule.nodes)
        .map((node) => {
          const element = document.querySelector(node.target[0]);
          if (!element)
            return { target: node.target, state: "unverified_selector" };
          const style = getComputedStyle(element);
          const threshold =
            parseFloat(style.fontSize) >= 24 ||
            (parseFloat(style.fontSize) >= 18.666 &&
              parseFloat(style.fontWeight) >= 700)
              ? 3
              : 4.5;
          let parent = element,
            backgrounds = [];
          while (parent) {
            const paint = getComputedStyle(parent);
            if (paint.backgroundImage.startsWith("linear-gradient")) {
              backgrounds = paint.backgroundImage.match(/rgba?\([^)]+\)/g);
              break;
            }
            if (paint.backgroundImage !== "none")
              return { target: node.target, state: "unverified_image" };
            if (paint.backgroundColor !== "rgba(0, 0, 0, 0)") {
              backgrounds = [paint.backgroundColor];
              break;
            }
            parent = parent.parentElement;
          }
          if (!backgrounds?.length)
            return { target: node.target, state: "unverified_background" };
          return {
            target: node.target,
            state: "css_colors_measured",
            foreground: style.color,
            backgrounds,
            threshold,
            minimum_ratio: Math.min(
              ...backgrounds.map((color) => contrast(style.color, color)),
            ),
            decorative: element.closest('[aria-hidden="true"]') !== null,
          };
        });
      return {
        color_review: colorReview,
        violations: data.violations.map((rule) => ({
          id: rule.id,
          impact: rule.impact,
          description: rule.description,
          helpUrl: rule.helpUrl,
          nodes: rule.nodes.map((node) => ({
            target: node.target,
            contrast: [...node.any, ...node.all]
              .filter((item) => item.id === "color-contrast")
              .map((item) => ({ ...item.data })),
          })),
        })),
        incomplete: data.incomplete.map((rule) => ({
          id: rule.id,
          targets: rule.nodes.map((node) => node.target),
        })),
        passed_rule_count: data.passes.length,
      };
    });
    const targets = await page.evaluate(() => {
      const selector = (el) =>
        el.id
          ? "#" + el.id
          : el.tagName.toLowerCase() +
            (el.classList.length ? "." + [...el.classList].join(".") : "");
      const modal = [...document.querySelectorAll("dialog[open]")].at(-1);
      const all = [
        ...(modal || document).querySelectorAll(
          "button,a[href],input:not([type=hidden]),select,textarea,summary,[tabindex='0']",
        ),
      ]
        .filter(
          (el) =>
            !el.disabled &&
            el.getClientRects().length &&
            getComputedStyle(el).visibility !== "hidden",
        )
        .map((el) => ({ el, r: el.getBoundingClientRect() }));
      const small = all.filter(({ r }) => r.width < 24 || r.height < 24);
      return {
        interactive_count: all.length,
        undersized: small.map(({ el, r }) => {
          const cx = r.x + r.width / 2,
            cy = r.y + r.height / 2;
          const collisions = all.filter((other) => {
            if (
              other.el === el ||
              el.contains(other.el) ||
              other.el.contains(el)
            )
              return false;
            const s = other.r;
            if (s.width < 24 || s.height < 24)
              return (
                Math.hypot(cx - s.x - s.width / 2, cy - s.y - s.height / 2) <
                24 - 0.1
              );
            const dx = Math.max(s.x - cx, 0, cx - s.right),
              dy = Math.max(s.y - cy, 0, cy - s.bottom);
            return Math.hypot(dx, dy) < 12 - 0.1;
          });
          return {
            selector: selector(el),
            width: +r.width.toFixed(2),
            height: +r.height.toFixed(2),
            spacing_exception: !collisions.length,
            collisions: collisions.map(({ el }) => selector(el)),
          };
        }),
      };
    });
    scans.push({ name, viewport: page.viewportSize(), ...result, targets });
    console.log(
      "SCAN " +
        name +
        " violations=" +
        result.violations.length +
        " small=" +
        targets.undersized.length,
    );
  }
  async function focusLoop(page, modalId) {
    for (let index = 0; index < 16; index++) {
      await page.keyboard.press(index < 8 ? "Tab" : "Shift+Tab");
      const focused = await page.evaluate((modalId) => {
        const active = document.activeElement,
          modal = document.querySelector(modalId),
          r = active.getBoundingClientRect();
        return {
          inside: modal.contains(active),
          visible: r.width > 0 && r.height > 0,
          outline: getComputedStyle(active).outlineStyle,
          width: getComputedStyle(active).outlineWidth,
        };
      }, modalId);
      assert.ok(
        focused.inside && focused.visible,
        "Tab focus must remain in visible modal content",
      );
      assert.ok(
        focused.outline !== "none" && parseFloat(focused.width) > 0,
        "keyboard focus requires a visible outline",
      );
    }
  }
  for (const mobile of [false, true]) {
    const label = mobile ? "390" : "1440";
    const { page, context, mode, log } = await setup({ mobile, dual: true });
    await scan(page, label + "-scores");
    await check(label + " 跳过导航可用且目标接受焦点", async () => {
      await page.keyboard.press("Tab");
      assert.equal(
        await page.locator(":focus").getAttribute("class"),
        "skip-link",
      );
      const rect = await page.locator(":focus").boundingBox();
      assert.ok(
        rect.width >= 24 && rect.height >= 24,
        "skip link must have an actual usable target while focused",
      );
      await page.keyboard.press("Enter");
      assert.equal(
        await page.evaluate(() => document.activeElement.id),
        "main",
      );
    });
    await check(label + " 输入边界和双色焦点环对比度", async () => {
      await page.locator("#match-search").focus();
      const measured = await page.evaluate(() => {
        const luminance = (rgb) =>
          rgb
            .match(/[\d.]+/g)
            .slice(0, 3)
            .map(Number)
            .map((value) => {
              const s = value / 255;
              return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
            })
            .reduce(
              (sum, value, index) =>
                sum + value * [0.2126, 0.7152, 0.0722][index],
              0,
            );
        const contrast = (a, b) => {
          const values = [luminance(a), luminance(b)].sort((a, b) => b - a);
          return +((values[0] + 0.05) / (values[1] + 0.05)).toFixed(2);
        };
        const search = getComputedStyle(
          document.querySelector("#match-search"),
        );
        const nav = getComputedStyle(document.querySelector(".site-header"));
        return {
          input_border: contrast(search.borderTopColor, "rgb(255,255,255)"),
          focus_outline_on_white: contrast(
            search.outlineColor,
            "rgb(255,255,255)",
          ),
          white_ring_on_purple: contrast(
            "rgb(255,255,255)",
            nav.backgroundColor,
          ),
        };
      });
      for (const value of Object.values(measured))
        assert.ok(value >= 3, JSON.stringify(measured));
      return measured;
    });
    await page.screenshot({
      path: evidence + `/candidate-a11y-${label}-focus-fixture.png`,
    });
    await check(label + " 比分键盘逐项焦点不被手机底栏遮挡", async () => {
      await page.locator("#refresh-matches").focus();
      for (let index = 0; index < 64; index++) {
        await page.keyboard.press("Tab");
        const state = await page.evaluate(() => {
          const el = document.activeElement,
            r = el.getBoundingClientRect(),
            nav = document.querySelector(".mobile-bottom");
          const limit =
            nav.getClientRects().length && !nav.contains(el)
              ? nav.getBoundingClientRect().top
              : innerHeight;
          return { tag: el.tagName, top: r.top, bottom: r.bottom, limit };
        });
        assert.ok(
          state.top >= -0.5 && state.bottom <= state.limit + 0.5,
          JSON.stringify(state),
        );
      }
    });
    await page.locator("#league-filter").click();
    await scan(page, label + "-league-dialog");
    await check(label + " 联赛弹窗键盘循环与关闭返回", async () => {
      await focusLoop(page, "#content-dialog");
      await page.keyboard.press("Escape");
      assert.equal(
        await page.evaluate(() => document.activeElement.id),
        "league-filter",
      );
    });
    if (await page.locator("#content-dialog").isVisible())
      await page.keyboard.press("Escape");
    await page.locator(".team-home").first().click();
    await page.waitForSelector(".market-table");
    await scan(page, label + "-detail-dialog");
    await check(
      label + " 实时列表移除原触发按钮后关闭详情返回页标题",
      async () => {
        await page
          .locator(".match-row")
          .first()
          .evaluate((element) => element.remove());
        await page.keyboard.press("Escape");
        await page.waitForFunction(
          () => !document.querySelector("#content-dialog").open,
        );
        await page.waitForFunction(
          () => document.activeElement.id === "score-title",
          null,
          { timeout: 1000 },
        );
      },
    );
    for (const name of [
      "experts",
      "messages",
      "membership",
      "account",
      "admin",
    ]) {
      await page.evaluate((name) => (location.hash = name), name);
      await page.waitForSelector("#view-" + name + ".active");
      await page.waitForTimeout(160);
      if (name === "messages") {
        await page.locator("#message-expert").selectOption(id);
        await page.waitForSelector(".message-card");
      }
      await scan(page, label + "-" + name);
      if (name === "messages") {
        await page.locator("#messages-list [data-message]").first().click();
        await page.waitForSelector(".media-gallery img");
        await scan(page, label + "-original-dialog");
        await page.keyboard.press("Escape");
      }
      if (name === "account" && mobile)
        await check("390 手机管理入口尺寸与键盘可用", async () => {
          const entry = page.locator("#account-admin-entry");
          await entry.focus();
          const rect = await entry.boundingBox();
          assert.ok(
            rect.height >= 44 && rect.width >= 44,
            "mobile admin action requires 44×44 target",
          );
          const position = await entry.evaluate((el) => {
            el.scrollIntoView({ block: "nearest" });
            const r = el.getBoundingClientRect(),
              nav = document
                .querySelector(".mobile-bottom")
                .getBoundingClientRect();
            return { bottom: r.bottom, top: r.top, limit: nav.top };
          });
          assert.ok(
            position.top >= 0 && position.bottom <= position.limit,
            "focus hidden behind mobile navigation",
          );
          await page.keyboard.press("Enter");
          await page.waitForSelector("#view-admin.active");
        });
    }
    for (const tab of ["telegram", "leyu", "membership", "orders"]) {
      await page.locator(`[data-admin-tab=${tab}]`).click();
      await page.waitForTimeout(120);
      await scan(page, label + "-admin-" + tab);
    }
    await page.locator("[data-admin-tab=system]").click();
    await page
      .locator("#mfa-start")
      .evaluate((el) => (el.closest("details").open = true));
    await scan(page, label + "-admin-mfa-settings");
    await page.locator("#mfa-start").focus();
    await page.screenshot({
      path: evidence + `/candidate-a11y-${label}-mfa-settings-fixture.png`,
    });
    await page.locator("#mfa-start").click();
    await page.waitForSelector("#mfa-secret");
    await scan(page, label + "-mfa-enrollment-dialog");
    await check(label + " 换机长弹窗以标题聚焦并保持键盘约束", async () => {
      assert.equal(
        await page.evaluate(() => document.activeElement.id),
        "dialog-title",
      );
      await focusLoop(page, "#content-dialog");
    });
    await page.locator("#dialog-content [data-mfa-cancel]").click();
    await scan(page, label + "-cancel-confirmation-dialog");
    await check(
      label + " 取消换机确认初始聚焦安全选项且Escape保留登记",
      async () => {
        assert.equal(
          await page
            .getByRole("dialog", { name: "确认操作", exact: true })
            .count(),
          1,
        );
        assert.equal(
          await page.evaluate(() => document.activeElement.id),
          "confirm-no",
        );
        await focusLoop(page, "#confirm-dialog");
        await page.keyboard.press("Escape");
        await page.waitForFunction(
          () => !document.querySelector("#confirm-dialog").open,
        );
        assert.equal(await page.locator("#mfa-secret").count(), 1);
      },
    );
    mode.reauthRequired = true;
    await page.locator("#mfa-enrollment-form [name=totp_code]").fill("222222");
    await page.locator("#mfa-enrollment-form button").click();
    await page.waitForSelector("#reauth-dialog[open]");
    await scan(page, label + "-nested-reauth-dialog");
    await check(label + " 叠加重验Tab约束与Escape保留原窗口", async () => {
      await focusLoop(page, "#reauth-dialog");
      await page.keyboard.press("Escape");
      await page.waitForFunction(
        () => !document.querySelector("#reauth-dialog").open,
      );
      await page.waitForFunction(
        () => document.activeElement.closest("dialog")?.id === "content-dialog",
        null,
        { timeout: 1000 },
      );
      assert.equal(
        await page.locator("#content-dialog").evaluate((el) => el.open),
        true,
      );
      assert.equal(await page.locator("#mfa-secret").count(), 1);
      assert.equal(
        await page.evaluate(() => document.activeElement.closest("dialog")?.id),
        "content-dialog",
      );
    });
    if (await page.locator("#reauth-dialog").isVisible())
      await page.keyboard.press("Escape");
    mode.reauthRequired = false;
    await page.locator("#mfa-enrollment-form [name=totp_code]").fill("333333");
    await page.locator("#mfa-enrollment-form button").click();
    await page.waitForSelector("[data-once-secret] pre");
    await scan(page, label + "-mfa-success-dialog");
    await check(
      label + " 换机成功替换内容后重新聚焦标题并清理秘密",
      async () => {
        assert.equal(
          await page.evaluate(() => document.activeElement.id),
          "dialog-title",
        );
        await page.keyboard.press("Escape");
        assert.equal(await page.locator("[data-once-secret]").count(), 0);
        assert.equal(
          await page.evaluate(() => document.activeElement.id),
          "mfa-start",
        );
      },
    );
    if (await page.locator("#content-dialog").isVisible())
      await page.keyboard.press("Escape");
    await check(label + " 减少动画关闭过渡及装饰动画", async () => {
      await page.emulateMedia({ reducedMotion: "reduce" });
      const active = await page.evaluate(() =>
        [...document.querySelectorAll("*")]
          .filter((el) => el.getClientRects().length)
          .filter((el) => {
            const s = getComputedStyle(el);
            return (
              (s.animationName !== "none" &&
                parseFloat(s.animationDuration) > 0) ||
              s.transitionDuration.split(",").some((v) => parseFloat(v) > 0)
            );
          })
          .map((el) => el.id || el.tagName),
      );
      assert.deepEqual(active, []);
    });
    await check(label + " 本人每日时区读取、保留额外有效项与提交", async () => {
      await page.evaluate(() => (location.hash = "account"));
      await page.waitForSelector("#view-account.active");
      const tz = page.locator("#betting-form [name=daily_timezone]");
      await tz.waitFor({ state: "attached", timeout: 2000 });
      assert.equal(await tz.inputValue(), "America/New_York");
      await page.locator("#betting-form [name=enabled]").check();
      await tz.selectOption("Europe/Berlin");
      await page.locator("#betting-form [name=expert]").first().check();
      for (const name of [
        "stake_per_order",
        "max_per_order",
        "daily_limit",
        "open_exposure_limit",
      ])
        await page.locator(`#betting-form [name=${name}]`).fill("1");
      await page.locator("#betting-form [name=confirmed]").check();
      await page.locator("#betting-form button.primary-button").click();
      await page.waitForTimeout(100);
      assert.equal(
        log
          .filter(
            (item) =>
              item.path === "/accounts/leyu/auto-betting" &&
              item.method === "PUT",
          )
          .at(-1)?.body.daily_timezone,
        "Europe/Berlin",
      );
    });
    await context.close();
    const guest = await setup({ mobile });
    await guest.page.locator("#account-entry").click();
    await scan(guest.page, label + "-auth-dialog");
    await guest.page.screenshot({
      path: evidence + `/candidate-a11y-${label}-login-fixture.png`,
    });
    await check(label + " 登录弹窗有名称、初始焦点与关闭返回", async () => {
      assert.equal(
        await guest.page
          .getByRole("dialog", { name: "用户登录", exact: true })
          .count(),
        1,
      );
      assert.equal(
        await guest.page.evaluate(() =>
          document.activeElement.getAttribute("name"),
        ),
        "email",
      );
      await focusLoop(guest.page, "#auth-dialog");
      await guest.page.keyboard.press("Escape");
      assert.equal(
        await guest.page.evaluate(() => document.activeElement.id),
        "account-entry",
      );
    });
    await guest.context.close();
  }
  await check("页面无JavaScript运行错误", () => assert.deepEqual(caught, []));
  await check("所有固定场景自动规则无已确认违规", () =>
    assert.deepEqual(
      scans
        .filter((scan) => scan.violations.length)
        .map((scan) => ({
          name: scan.name,
          rules: scan.violations.map((rule) => rule.id),
        })),
      [],
    ),
  );
  await check("所有固定场景24px目标或几何间距例外", () =>
    assert.deepEqual(
      scans.flatMap((scan) =>
        scan.targets.undersized
          .filter((target) => !target.spacing_exception)
          .map((target) => ({ name: scan.name, ...target })),
      ),
      [],
    ),
  );
  await check("自动对比度未判定项的CSS颜色补测", () =>
    assert.deepEqual(
      scans.flatMap((scan) =>
        scan.color_review
          .filter(
            (row) =>
              row.state !== "css_colors_measured" ||
              (!row.decorative && row.minimum_ratio < row.threshold),
          )
          .map((row) => ({ name: scan.name, ...row })),
      ),
      [],
    ),
  );
  const result = {
    generated_at: new Date().toISOString(),
    stage: baseline ? "before-fixes" : "after-fixes",
    environment:
      "Docker Chromium 141 / artificial API / no production services",
    axe_version: "4.10.3",
    scans,
    checks,
    browser_errors: caught,
    limitations: [
      "No screen reader, real touch device, voice control or operating-system high-contrast validation",
      "Axe incomplete results require human review; passing rules do not prove WCAG conformance",
      "24px geometry only evaluates current DOM/CSS at these viewports; exceptions require design review",
    ],
  };
  await fs.writeFile(
    `/work/tests/browser/accessibility-${baseline ? "baseline" : "results"}.json`,
    JSON.stringify(result, null, 2),
  );
  return baseline || checks.every((item) => item.status === "passed");
}
