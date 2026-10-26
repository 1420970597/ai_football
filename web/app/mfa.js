import { $, esc, stamp, api, confirmAction, toast } from "./ui.js";

let app;
let pending = null;
let expiryTimer = null;
let statusSequence = 0;
const mfaApi = (path, options = {}) =>
  api("/admin/mfa" + path, { ...options, context: "admin" });

function stopTimer() {
  clearInterval(expiryTimer);
  expiryTimer = null;
}

export async function loadMfaStatus() {
  const sequence = ++statusSequence;
  try {
    const status = await mfaApi("/status");
    if (sequence !== statusSequence) return;
    pending = status.pending;
    const label =
      {
        configured: "验证器已配置",
        missing: "验证器尚未配置",
        unreadable: "验证器密文不可读，请核对主密钥与恢复路径",
      }[status.configuration_state] || "状态尚未核验";
    $("#mfa-status").innerHTML =
      `<p>${esc(label)} · 未使用备用码 ${esc(status.recovery_codes_remaining ?? "尚未核验")} 个</p>${pending ? `<p class="notice">有当前管理会话发起的待确认登记，有效至 ${stamp(pending.expires_at)}。原验证器保持有效。</p><div class="form-actions"><button class="small-button" data-mfa-continue>继续输入新验证码</button><button class="small-button" data-mfa-cancel>取消待确认登记</button><button class="danger-outline" data-mfa-replace>遗失新密钥，明确替换登记</button></div>` : ""}`;
  } catch (error) {
    $("#mfa-status").textContent = "验证器状态暂不可读：" + error.message;
  }
}

function updateExpiry() {
  if (!pending || !$("#mfa-expiry")) return stopTimer();
  const seconds = Math.ceil(
    (Date.parse(pending.expires_at) - Date.now()) / 1000,
  );
  if (!Number.isFinite(seconds) || seconds <= 0) {
    $("#mfa-expiry").textContent =
      "登记已过期，原验证器保持有效。请取消或明确替换登记。";
    $("#mfa-qr")?.replaceChildren();
    if ($("#mfa-secret")) $("#mfa-secret").textContent = "";
    $("#mfa-enrollment-form").elements.totp_code.disabled = true;
    $("#mfa-enrollment-form button").disabled = true;
    stopTimer();
  } else
    $("#mfa-expiry").textContent =
      `有效至 ${stamp(pending.expires_at)}，约剩 ${Math.floor(seconds / 60)}分${seconds % 60}秒，以服务端校验为准。`;
}

function showPending(enrollment = null) {
  if (!pending) {
    toast("当前会话没有待确认登记，请先刷新状态。");
    return;
  }
  stopTimer();
  app.dialog(
    "登记新管理员验证器",
    `<section class="detail-section" data-mfa-enrollment><p class="notice">确认新验证码前，旧验证器与旧备用码保持有效。请在新手机验证器中添加本密钥，输入新手机显示的六位验证码。</p>${enrollment ? '<div id="mfa-qr"></div><p class="form-note">也可手工录入以下密钥。此密钥只显示一次，不保存到浏览器。</p><pre id="mfa-secret" class="raw-text"></pre>' : '<p class="notice">密钥只在首次发起时返回。如果新手机已保存，请继续输入新验证码；如果没有保存，请明确替换登记，旧验证器仍保持有效。</p>'}<p id="mfa-expiry" class="form-note"></p><form id="mfa-enrollment-form" class="compact-form"><label>新验证器六位验证码<input name="totp_code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required></label><button class="primary-button">确认新验证器并撤销其他管理会话</button><p id="mfa-message" role="status"></p></form><p class="form-note">若提示再次验证管理身份，请使用当前旧验证器完成身份验证，再回来提交新验证码。换机不会撤销普通用户会话。</p><div class="form-actions"><button class="small-button" data-mfa-cancel>取消待确认登记</button><button class="danger-outline" data-mfa-replace>重新生成并替换待确认登记</button></div></section>`,
  );
  if (enrollment) {
    $("#mfa-secret").textContent = enrollment.secret;
    if (!enrollment.otpauth_uri.startsWith("otpauth://totp/"))
      throw new Error("验证器二维码格式不符合要求");
    const code = window.qrcode(0, "M");
    code.addData(enrollment.otpauth_uri);
    code.make();
    $("#mfa-qr").innerHTML = code.createSvgTag({ scalable: true, margin: 2 });
    $("#mfa-qr svg").classList.add("qr-code");
    $("#mfa-qr svg").setAttribute("role", "img");
    $("#mfa-qr svg").setAttribute("aria-label", "新管理员验证器离线生成二维码");
  }
  const form = $("#mfa-enrollment-form");
  form.onsubmit = async (event) => {
    event.preventDefault();
    const button = $("button", form);
    button.disabled = true;
    try {
      const data = await mfaApi(
        "/enrollment/" + encodeURIComponent(pending.enrollment_id) + "/confirm",
        { method: "POST", body: { totp_code: form.elements.totp_code.value } },
      );
      stopTimer();
      pending = null;
      form.reset();
      app.dialog(
        "新验证器已确认",
        `<section class="detail-section" data-once-secret><p class="notice">${esc(data.message)}</p>${data.recovery_codes ? `<h3>十个一次性备用码，请离线保存</h3><pre class="raw-text">${esc(data.recovery_codes.join("\n"))}</pre>` : "<p>该请求已确认，备用码不会再次返回。如首次响应丢失，请在当前会话重新生成备用码。</p>"}<p class="form-note">新验证码已用于本次确认，下一次登录请等待下一枚验证码或使用备用码。关闭后页面不保留密钥与备用码。</p></section>`,
      );
      loadMfaStatus();
    } catch (error) {
      $("#mfa-message").textContent = error.message;
      form.elements.totp_code.value = "";
      if (error.status === 409) {
        $("#mfa-qr")?.replaceChildren();
        if ($("#mfa-secret")) $("#mfa-secret").textContent = "";
      }
    } finally {
      if (form.isConnected) {
        button.disabled = false;
        updateExpiry();
      }
    }
  };
  updateExpiry();
  expiryTimer = setInterval(updateExpiry, 1000);
}

async function startEnrollment(replaceExisting = false) {
  if (
    replaceExisting &&
    !(await confirmAction(
      "明确撤销待确认登记并生成新密钥？先前的新手机登记将无法再确认；当前旧验证器保持有效。",
    ))
  )
    return;
  try {
    const data = await mfaApi("/enrollment", {
      method: "POST",
      body: { replace_existing: replaceExisting },
    });
    pending = {
      enrollment_id: data.enrollment_id,
      expires_at: data.expires_at,
    };
    showPending(data);
    loadMfaStatus();
  } catch (error) {
    toast(error.message);
    await loadMfaStatus();
    if (error.status === 409)
      $("#mfa-status").insertAdjacentHTML(
        "beforeend",
        '<p class="notice">已有待确认登记。如果已遗失其密钥，请明确替换；不会自动覆盖。</p><button class="danger-outline" data-mfa-replace>明确替换待确认登记</button>',
      );
  }
}

async function cancelEnrollment() {
  if (
    !pending ||
    !(await confirmAction(
      "取消尚未确认的新验证器？当前旧验证器与备用码保持有效。",
    ))
  )
    return;
  try {
    await mfaApi("/enrollment/" + encodeURIComponent(pending.enrollment_id), {
      method: "DELETE",
    });
    stopTimer();
    pending = null;
    if ($("[data-mfa-enrollment]")) $("#content-dialog").close();
    toast("新登记已取消，旧验证器保持有效。");
    loadMfaStatus();
  } catch (error) {
    toast(error.message);
  }
}

export function initMfa(context) {
  app = context;
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.id === "mfa-refresh") return loadMfaStatus();
    if (button.id === "mfa-start") return startEnrollment();
    if (button.hasAttribute("data-mfa-continue")) return showPending();
    if (button.hasAttribute("data-mfa-replace")) return startEnrollment(true);
    if (button.hasAttribute("data-mfa-cancel")) return cancelEnrollment();
  });
  $("#content-dialog").addEventListener("close", () => {
    stopTimer();
    if ($("[data-mfa-enrollment]")) $("#dialog-content").replaceChildren();
  });
}
