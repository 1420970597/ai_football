import {
  $,
  esc,
  stamp,
  api,
  submit,
  toast,
  jsonTrace,
  newId,
} from "./ui.js";
let app;
let exportedPage = null;
const adminApi = (path, options = {}) =>
  api("/admin" + path, { ...options, context: "admin" });

async function passwordRecovery() {
  const email = $("#auth-form").elements.email.value;
  $("#auth-dialog").close();
  app.dialog(
    "找回用户密码",
    '<section class="detail-section"><p>正在读取恢复方式…</p></section>',
  );
  const sequence = app.state.dialogSequence;
  try {
    const status = await api("/auth/recovery/status");
    if (sequence !== app.state.dialogSequence) return;
    $("#dialog-content").innerHTML =
      `<section class="detail-section"><p class="notice">${esc(status.message || "自助邮件恢复未配置，请联系管理员完成人工身份核验，取得一次性恢复码。")}</p><p class="form-note">系统不会假称邮件已发送。恢复成功将撤销旧会话并停止原自动投注许可。</p><form id="password-reset-form" class="compact-form"><label>账号邮箱<input name="email" type="email" autocomplete="username" value="${esc(email)}" required></label><label>管理员签发的恢复码<input name="code" type="password" autocomplete="off" required></label><label>新密码<input name="new_password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required></label><button class="primary-button">使用恢复码重置密码</button></form></section>`;
    $("#password-reset-form").onsubmit = (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      submit(form, async () => {
        await api("/auth/recovery/complete", {
          method: "POST",
          body: Object.fromEntries(new FormData(form)),
        });
        form.reset();
        $("#content-dialog").close();
        toast("密码已重置，请使用新密码重新登录。");
        app.auth("login");
      });
    };
  } catch (error) {
    if (sequence === app.state.dialogSequence)
      $("#dialog-content").innerHTML =
        `<section class="detail-section"><p class="notice error">${esc(error.message)}</p></section>`;
  }
}

async function exportSection(section, cursor = null) {
  if (!cursor)
    app.dialog(
      "导出本人资料",
      '<section class="detail-section"><p>正在读取本人数据…</p></section>',
    );
  const sequence = app.state.dialogSequence;
  try {
    const query = new URLSearchParams({ section, limit: "100" });
    if (cursor) query.set("cursor", cursor);
    const data = await api("/auth/export?" + query);
    if (sequence !== app.state.dialogSequence) return;
    exportedPage = data;
    $("#dialog-content").innerHTML =
      `<section class="detail-section"><h3>${esc({ profile: "本人资料", memberships: "会员记录", orders: "本人订单" }[section])}</h3><p class="form-note">生成于 ${stamp(data.generated_at)} · 本页 ${(data.items || []).length} 条。仅含本人记录，凭据与私有会话不导出。</p><p class="form-note">排除字段：${esc(Array.isArray(data.excluded) ? data.excluded.join("、") : JSON.stringify(data.excluded || []))}</p><pre class="raw-text">${jsonTrace(data)}</pre><div class="form-actions"><button class="primary-button" data-export-download>下载本页 JSON</button>${data.next_cursor ? `<button class="small-button" data-export-next="${esc(section)}" data-cursor="${esc(data.next_cursor)}">继续读取下一页</button>` : '<span class="muted">已到最后一页</span>'}</div></section>`;
  } catch (error) {
    if (sequence === app.state.dialogSequence)
      $("#dialog-content").innerHTML =
        `<section class="detail-section"><p class="notice error">${esc(error.message)}</p><button class="small-button" data-export="${esc(section)}">重新读取</button></section>`;
  }
}

async function closureState() {
  try {
    const data = await api("/auth/closure");
    const pending = data.state === "pending_accounting";
    $("#closure-status").textContent =
      data.message ||
      (pending
        ? "账户正在注销对账中，未结算记录仍保留。"
        : "尚未申请注销。注销会停止自动投注并撤销会话；未结算订单需先完成对账。");
    $("#closure-status").textContent += pending
      ? ` 未完成对账订单 ${data.outstanding_orders ?? "尚未核验"} 条。核对完成后可再次确认注销，也可取消申请；自动投注保持关闭。`
      : "";
    $("#closure-cancel").hidden = !pending;
    $("#closure-form").hidden = false;
    $("#closure-form button").textContent = pending
      ? "再次核对并完成注销"
      : "停止投注并申请注销";
  } catch (error) {
    $("#closure-status").textContent = error.message;
  }
}

async function issueRecovery(userId, label) {
  app.dialog(
    "人工核验后的密码恢复",
    `<section class="detail-section"><p>目标账号：${esc(label)}</p><p class="notice">请先通过独立渠道核验用户身份。恢复码只显示一次，请通过核验后的渠道交付。</p><form id="issue-recovery-form" class="compact-form"><label>人工核验依据与原因<textarea name="reason" minlength="3" maxlength="500" required></textarea></label><button class="primary-button">签发一次性恢复码</button></form><div id="issued-recovery" role="status"></div></section>`,
  );
  let key = newId();
  const form = $("#issue-recovery-form");
  form.oninput = () => {
    key = newId();
  };
  form.onsubmit = (event) => {
    event.preventDefault();
    submit(form, async () => {
      const data = await adminApi(
        "/users/" + encodeURIComponent(userId) + "/password-recovery",
        {
          method: "POST",
          body: { reason: form.elements.reason.value, idempotency_key: key },
        },
      );
      form.hidden = true;
      $("#issued-recovery").innerHTML =
        `<p>${esc(data.message || "恢复码已签发")}</p><p>到期：${stamp(data.expires_at)}</p>${data.code ? `<pre class="raw-text">${esc(data.code)}</pre><p class="form-note">关闭后无法再次查看；同一请求重试不再返回恢复码。</p>` : '<p class="notice">该请求已签发恢复码，本次不会再次显示。</p>'}`;
    });
  };
}

export function initIdentity(context) {
  app = context;
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.hasAttribute("data-password-recovery"))
      return passwordRecovery();
    if (button.dataset.export) return exportSection(button.dataset.export);
    if (button.dataset.exportNext)
      return exportSection(button.dataset.exportNext, button.dataset.cursor);
    if (button.dataset.userRecovery)
      return issueRecovery(
        button.dataset.userRecovery,
        button.dataset.userLabel,
      );
    if (button.hasAttribute("data-export-download") && exportedPage) {
      const url = URL.createObjectURL(
        new Blob([JSON.stringify(exportedPage, null, 2)], {
          type: "application/json",
        }),
      );
      const link = document.createElement("a");
      link.href = url;
      link.download = "qiuxun-personal-" + exportedPage.section + ".json";
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  });
  $("#content-dialog").addEventListener("close", () => {
    exportedPage = null;
    if (
      $("#issued-recovery") ||
      $("#issue-recovery-form") ||
      $("[data-once-secret]") ||
      $("#password-reset-form") ||
      $("#cancel-closure-form")
    )
      $("#dialog-content").replaceChildren();
  });
  $("#closure-section").addEventListener("toggle", () => {
    if ($("#closure-section").open) closureState();
  });
  $("#closure-refresh").onclick = () => closureState();
  $("#closure-form").onsubmit = (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    submit(form, async () => {
      const result = await api("/auth/closure", {
        method: "POST",
        body: Object.fromEntries(new FormData(form)),
      });
      form.reset();
      toast(
        result.message ||
          (result.state === "pending_accounting"
            ? "自动投注已停止，注销等待未结算记录对账。"
            : "账户已注销。"),
      );
      await app.loadAccount();
      if (app.state.user) await closureState();
    });
  };
  $("#closure-cancel").onclick = () => {
    app.dialog(
      "取消注销申请",
      '<section class="detail-section"><form id="cancel-closure-form" class="compact-form"><p>取消后自动投注仍保持关闭，需由本人重新授权。</p><button class="primary-button">取消注销</button></form></section>',
    );
    $("#cancel-closure-form").onsubmit = (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      submit(form, async () => {
        await api("/auth/closure/cancel", {
          method: "POST",
        });
        form.reset();
        $("#content-dialog").close();
        toast("注销申请已取消，自动投注保持关闭。");
        await app.loadAccount();
        await closureState();
      });
    };
  };
}
