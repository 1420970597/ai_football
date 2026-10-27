export const $ = (query, root = document) => root.querySelector(query);
export const $$ = (query, root = document) => [...root.querySelectorAll(query)];
export function newId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
export const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
export const time = (value) =>
  value && Number.isFinite(new Date(value).getTime())
    ? new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(new Date(value))
    : "时间未知";
export const stamp = (value) =>
  value && Number.isFinite(new Date(value).getTime())
    ? new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        hour12: false,
      }).format(new Date(value))
    : "时间未知";
export const dayKey = (value = new Date()) =>
  new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(value);
export const shiftDay = (key, offset) =>
  new Date(Date.parse(key + "T12:00:00Z") + offset * 86400000)
    .toISOString()
    .slice(0, 10);
export const percent = (value) =>
  value === null || value === undefined
    ? "样本不足"
    : `${(Number(value) * 100).toFixed(1)}%`;
export const decimal = (value, digits = 2) =>
  value === null || value === undefined ? "—" : Number(value).toFixed(digits);
export const signed = (value) =>
  value === null || value === undefined
    ? "—"
    : `${Number(value) > 0 ? "+" : ""}${decimal(value)}`;
export const color = (value) =>
  Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "";
export const phases = {
  live: "滚球",
  scheduled: "未开赛",
  finished: "完场",
  cancelled: "取消 / 延期",
};
export const outcomes = {
  pending: "待结算",
  win: "全赢",
  half_win: "半赢",
  loss: "全输",
  half_loss: "半输",
  push: "走盘",
  void: "作废",
};
export const selections = {
  home: "主队",
  away: "客队",
  draw: "平局",
  over: "大",
  under: "小",
  yes: "是",
  no: "否",
};
export const periods = {
  full_time: "全场",
  first_half: "上半场",
  second_half: "下半场",
  full: "全场",
  half: "半场",
};
export const selection = (value) => selections[value] || value || "方向未知";
export const period = (value) => periods[value] || value || "期间未知";
let toastTimer;
export function toast(message) {
  $("#toast").textContent = message;
  $("#toast").classList.add("visible");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("visible"), 4500);
}
export function stateBox(
  element,
  { title, description = "", retry = false, loading = false },
) {
  element.hidden = false;
  element.innerHTML = loading
    ? '<div class="skeleton-row"></div>'.repeat(5)
    : `<strong>${esc(title)}</strong><div>${esc(description)}</div>${retry ? '<button class="small-button" data-retry>重新加载</button>' : ""}`;
}
const modalStack = [];
const modalOrigins = new WeakMap();
const boundModals = new WeakSet();
function visibleFocusables(modal) {
  return $$(
    "button,a[href],input,select,textarea,summary,[tabindex]",
    modal,
  ).filter(
    (element) =>
      !element.disabled &&
      element.tabIndex >= 0 &&
      element.getClientRects().length,
  );
}
export function showDialog(modal, initialFocus) {
  if (!boundModals.has(modal)) {
    boundModals.add(modal);
    modal.addEventListener("keydown", (event) => {
      if (
        event.key !== "Tab" ||
        modalStack.filter((item) => item.open).at(-1) !== modal
      )
        return;
      const items = visibleFocusables(modal),
        current = document.activeElement;
      const index = items.indexOf(current);
      if (!items.length) {
        event.preventDefault();
        modal.focus();
        return;
      }
      if (
        index < 0 ||
        (!event.shiftKey && index === items.length - 1) ||
        (event.shiftKey && index === 0)
      ) {
        event.preventDefault();
        (event.shiftKey ? items.at(-1) : items[0]).focus();
      }
    });
    modal.addEventListener("close", () => {
      const index = modalStack.indexOf(modal);
      if (index >= 0) modalStack.splice(index, 1);
      const origin = modalOrigins.get(modal);
      queueMicrotask(() => {
        const parent = modalStack.filter((item) => item.open).at(-1);
        if (
          origin?.isConnected &&
          origin.getClientRects().length &&
          !origin.disabled &&
          (!parent || parent.contains(origin))
        )
          origin.focus({ preventScroll: true });
        else if (parent)
          (
            parent.querySelector("[tabindex='-1']") ||
            visibleFocusables(parent)[0]
          )?.focus();
        else {
          const heading = $(".view.active h1");
          if (heading) {
            heading.tabIndex = -1;
            heading.focus({ preventScroll: true });
          }
        }
      });
    });
  }
  if (!modal.open) {
    modalOrigins.set(modal, document.activeElement);
    modalStack.push(modal);
    modal.showModal();
  }
  initialFocus?.focus();
}
export function dialog(title, html) {
  $("#dialog-title").textContent = title;
  $("#dialog-content").innerHTML = html;
  const modal = $("#content-dialog");
  showDialog(modal, $("#dialog-title"));
}
export function confirmAction(message) {
  return new Promise((resolve) => {
    const modal = $("#confirm-dialog");
    $("#confirm-message").textContent = message;
    const finish = (value) => {
      modal.close();
      resolve(value);
    };
    $("#confirm-yes").onclick = () => finish(true);
    $("#confirm-no").onclick = () => finish(false);
    modal.oncancel = () => resolve(false);
    showDialog(modal, $("#confirm-no"));
  });
}
export async function submit(form, action) {
  const buttons = $$("button", form);
  buttons.forEach((b) => (b.disabled = true));
  try {
    return await action();
  } catch (error) {
    toast(error.message);
    return null;
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}
export class ApiError extends Error {
  constructor(message, status = 0, requestId = "", detail = null) {
    super(message);
    this.status = status;
    this.requestId = requestId;
    this.detail = detail;
  }
}
function requestReauthentication(context) {
  const modal = $("#reauth-dialog");
  if (!modal || modal.open) return;
  const form = $("#reauth-form");
  form.reset();
  form.dataset.context = context;
  $("#reauth-title").textContent =
    context === "admin" ? "再次验证管理身份" : "再次验证本人身份";
  $("#reauth-totp").hidden = context !== "admin";
  form.elements.totp_code.required = false;
  $("#reauth-message").textContent =
    "当前操作需要近期身份验证。已填写内容会保留；验证成功后请再次提交原操作。";
  form.onsubmit = async (event) => {
    event.preventDefault();
    const button = $("button[type=submit]", form);
    button.disabled = true;
    try {
      await api(
        context === "admin" ? "/admin/reauthenticate" : "/auth/reauthenticate",
        {
          method: "POST",
          context,
          body: {
            password: form.elements.password.value,
            ...(context === "admin" && form.elements.totp_code.value
              ? { totp_code: form.elements.totp_code.value }
              : {}),
          },
        },
      );
      form.reset();
      modal.close();
      toast("身份验证成功，请再次提交刚才的操作。原表单内容已保留。");
    } catch (error) {
      $("#reauth-message").textContent = error.message;
      form.elements.password.value = "";
      form.elements.totp_code.value = "";
    } finally {
      button.disabled = false;
    }
  };
  modal.onclose = () => form.reset();
  showDialog(modal, form.elements.password);
}
export async function api(
  path,
  { method = "GET", body, context = "user", signal, timeout = 15000 } = {},
) {
  const controller = new AbortController();
  const relay = () => controller.abort(signal.reason);
  if (signal?.aborted) relay();
  else signal?.addEventListener("abort", relay, { once: true });
  const timer = setTimeout(
    () => controller.abort(new DOMException("读取超时", "TimeoutError")),
    timeout,
  );
  const headers = { Accept: "application/json", "X-Session-Context": context };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (!["GET", "HEAD"].includes(method)) {
    const name = context === "admin" ? "v2_admin_csrf" : "v2_csrf";
    const cookie = document.cookie
      .split("; ")
      .find((x) => x.startsWith(name + "="));
    if (cookie)
      headers["X-CSRF-Token"] = decodeURIComponent(
        cookie.slice(name.length + 1),
      );
  }
  try {
    const response = await fetch("/api/v1" + path, {
      method,
      body: body === undefined ? undefined : JSON.stringify(body),
      headers,
      credentials: "same-origin",
      signal: controller.signal,
    });
    let data;
    try {
      data = await response.json();
    } catch {
      throw new ApiError("服务响应格式异常，请稍后重试", response.status);
    }
    if (!response.ok) {
      const detail =
        typeof data.detail === "string"
          ? data.detail
          : data.detail?.message
            ? data.detail.message
            : response.status === 422
              ? "输入内容不符合要求，请检查表单"
              : `请求失败（${response.status}）`;
      if (
        response.status === 403 &&
        data.detail?.code === "reauthentication_required" &&
        !path.endsWith("/reauthenticate")
      )
        requestReauthentication(
          data.detail.session_kind === "admin" ? "admin" : context,
        );
      throw new ApiError(
        detail,
        response.status,
        response.headers.get("X-Request-ID") || "",
        data.detail,
      );
    }
    return data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (signal?.aborted) throw error;
    if (error.name === "AbortError" || error.name === "TimeoutError")
      throw new ApiError("读取超时，请稍后重试");
    throw new ApiError("网络暂不可用，请检查连接后重试");
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", relay);
  }
}
export function jsonTrace(value) {
  return esc(
    typeof value === "string" ? value : JSON.stringify(value, null, 2),
  );
}
