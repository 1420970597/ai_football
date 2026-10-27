import {
  $,
  $$,
  esc,
  stamp,
  decimal,
  toast,
  submit,
  api,
  confirmAction,
  jsonTrace,
  newId,
  signed,
  period,
  selection,
} from "./ui.js";
import { loadMfaStatus } from "./mfa.js";
let app;
let sources = [];
let settings = {};
let leyuVersion = null;
let modelConfiguration = null;
let qrAttempt = null;
let tgTimer = null;
let dialogsCursor = null;
let adminsCursor = null;
let dialogsQuery = "",
  adminsQuery = "";
let dialogsSequence = 0,
  adminsSequence = 0;
let grantKey = newId();
let adminPlans = [];
let userCursor = null;
let userRows = [];
let orderRows = [];
let orderCursor = null;
const adminApi = (path, options = {}) =>
  api("/admin" + path, { ...options, context: "admin", timeout: 30000 });
export function initAdmin(context) {
  app = context;
  bind();
}
export async function loadAdmin() {
  await app.loadIdentities();
  const admin = app.state.admin;
  $("#admin-content").hidden = !admin;
  $("#admin-logout").hidden = !admin;
  $("#admin-identity").innerHTML = admin
    ? `<div class="identity-card"><h2>${esc(admin.user.display_name)} · 管理员</h2><p>${esc(admin.user.email)} · 管理会话独立于普通用户会话</p></div>`
    : '<div class="identity-card"><h2>管理员独立入口</h2><p>使用管理员账号和密码登录；已启用二次验证时还需输入动态验证码。用户会话不会获得管理权限。</p><button class="primary-button" data-login="admin">管理员登录</button></div>';
  if (!admin) return;
  await Promise.allSettled([
    loadStatus(),
    loadSettings(),
    loadSources(),
    loadAdminPlans(),
    loadDiagnostics(),
    loadMfaStatus(),
  ]);
}
async function loadDiagnostics() {
  try {
    const data = await adminApi("/diagnostics");
    const counter = (value) =>
      value ? String(value.count) + (value.capped ? "+" : "") : "未核验";
    const leyu = data.collector?.sources?.leyu || {};
    const websocket = data.collector?.sources?.websocket || {};
    const leyuVersion = leyu.config_version
      ? ` · 乐鱼采集器配置版本 ${esc(leyu.config_version)}`
      : " · 乐鱼采集器配置版本未回执";
    const metric = (value, suffix = "") =>
      value === null || value === undefined ? "暂无样本" : esc(value) + suffix;
    const leyuIssue = leyu.config_validation_error || leyu.last_error || "无错误分类";
    const leyuCode = leyu.last_error_code ? ` code=${esc(leyu.last_error_code)}` : "";
    const recoveryStats = `轮询成功/失败 ${metric(leyu.poll_successes)}/${metric(leyu.poll_failures)} · 故障类别 ${esc(leyu.failure_group || '无')} · 操作 ${esc(leyu.last_operation || '尚未记录')} · 冷却 ${metric(leyu.backoff_seconds, '秒')} · 下次尝试 ${stamp(leyu.retry_at)} · 连续失败 ${metric(leyu.consecutive_failures)} · 本地待发送订阅 ${metric(websocket.subscription_send_missing)}/${metric(websocket.subscription_expected)} · 上游订阅确认 ${websocket.subscription_confirmation_state === 'unverified' ? '未提供确认协议，无法计算真实漏订阅率' : esc(websocket.subscription_confirmation_state || '尚未核验')} · 告警（轮询/订阅/队列/写入）${esc(leyu.poll_alert_state || '未知')}/${esc(websocket.subscription_alert_state || '未知')}/${esc(websocket.queue_alert_state || '未知')}/${esc(websocket.delivery_alert_state || '未知')}`;
    const liveStats = `乐鱼采集 ${esc(leyu.state || "未知")}（${esc(leyuIssue)}${leyuCode}） · 实时连接 ${esc(websocket.state || "未知")} · 重连 ${metric(websocket.reconnects)} · 订阅请求数 ${metric(websocket.subscription_requests_sent)} · 接收帧 ${metric(websocket.received)} · 入队/投递 ${metric(websocket.events_received)}/${metric(websocket.events_delivered)} · 丢弃 ${metric(websocket.dropped)} · 投递失败 ${metric(websocket.delivery_failures)} · 队列 ${metric(websocket.queue_depth)}（高水位 ${metric(websocket.queue_high_water)}） · 事件源->接收 P95 ${metric(websocket.source_lag_ms_p95, "ms")}（${metric(websocket.source_lag_samples)} 样本） · 排队 P95 ${metric(websocket.queue_wait_ms_p95, "ms")} · 事件源->投递 P95 ${metric(websocket.delivery_age_ms_p95, "ms")}（${metric(websocket.delivery_age_samples)} 样本） · 最近事件 ${stamp(websocket.last_event_at)}`;
    const quoteLatency = `盘口源->接收 P95 ${metric(websocket.quote_source_lag_ms_p95, 'ms')}（${metric(websocket.quote_source_lag_samples)} 样本） · 盘口源->成功投递 P95 ${metric(websocket.quote_delivery_age_ms_p95, 'ms')}（${metric(websocket.quote_delivery_age_samples)} 样本） · 接收窗口 ${metric(websocket.quote_source_lag_window_size)} · 未知/无效/未来/过旧 ${metric(websocket.quote_source_lag_unknown_count)}/${metric(websocket.quote_source_lag_invalid_count)}/${metric(websocket.quote_source_lag_future_count)}/${metric(websocket.quote_source_lag_too_old_count)}；各窗口最多256条；投递成功不等于每条报价通过入库新鲜度或版本校验。`;
    const snapshotStats = `单场最大盘口 ${metric(leyu.max_observed_fixture_quotes)} · 隔离赛事 ${metric(leyu.quarantined_fixtures)} · 推送隔离 ${metric(leyu.last_push_quarantined_fixtures)} · 最近投递错误 ${esc(websocket.last_delivery_error_category || "无当前错误")}`;
    const taskStates = { action_required: '需处理', clear: '本次未发现异常', unverified: '未核验' };
    const taskTargets = { telegram: 'telegram', leyu: 'leyu', orders: 'orders', system: 'system' };
    const taskCards = Array.isArray(data.tasks) ? data.tasks.slice(0, 16).map(task => {
      const target = taskTargets[task.target];
      const count = task.count ? ` · ${counter(task.count)}` : '';
      return `<div class="admin-record"><strong>${esc(task.label)}${esc(count)}</strong> · ${esc(taskStates[task.state] || taskStates.unverified)}<p>${esc(task.detail)}</p>${target ? `<button class="small-button" data-admin-task-target="${target}">前往${esc({telegram:'Telegram',leyu:'乐鱼采集',orders:'订单核对',system:'系统运维'}[target])}</button>` : ''}</div>`;
    }).join('') : '<p class="form-note">逐项待办尚未核验，请刷新诊断。</p>';
    $("#admin-diagnostics").innerHTML = `<div id="admin-action-tasks" class="record-list">${taskCards}</div><p class="form-note">${snapshotStats}</p><p class="form-note">${recoveryStats}</p><p class="form-note" id="quote-latency-metrics">${quoteLatency}</p>` +
      `<p class="form-note">检查于 ${stamp(data.checked_at)} · 数据库 ${esc(data.database)} · 结构 ${esc(data.schema === "verified" ? "已核验" : data.schema === "migration_required" ? "需要迁移" : "状态未知")}${leyuVersion}</p><p class="form-note">${liveStats}</p><div class="status-grid">${[
        ["待审核", data.review_queue],
        ["媒体待上传", data.pending_media],
        ["未解决/失败", data.unresolved_reviews],
        ["过期报价", data.stale_quotes],
        ["未知订单", data.unknown_orders],
      ]
        .map(
          ([label, value]) =>
            `<div class="status-card"><span>${label}</span><strong>${counter(value)}</strong>${value?.oldest_at ? `<small>最早 ${stamp(value.oldest_at)}</small>` : ""}</div>`,
        )
        .join(
          "",
        )}</div><p class="form-note">对象存储：${esc({ healthy: "读写与校验成功", unavailable: "读写或校验失败", unconfigured: "未配置", configured_unverified: "已配置，尚未真实验证" }[data.storage] || "状态未知")}。Hermes框架：${data.hermes?.gateway === "reachable" ? "可达" : "暂不可达"}；${esc({recent_success:"近15分钟有框架审核返回，准确性仍需独立核验",historical_success:"仅有历史框架审核返回，当前提供方未验证"}[data.hermes?.provider] || "模型提供方尚未验证")}。</p><p class="form-note">赛事源时间 ${stamp(data.freshness?.fixture_source_at)} · 报价源时间 ${stamp(data.freshness?.quote_source_at)} · 消息接收 ${stamp(data.freshness?.message_received_at)}</p><p class="form-note">计数最多检查5000条，显示“5000+”表示超过上限；来源时间仅为已保存证据，不能代替连续采集健康检查。结构状态不代表失败迁移日志已完整核验。</p>`;
  } catch (error) {
    $("#admin-diagnostics").innerHTML =
      `<p class="notice error">${esc(error.message)}。待办尚未核验。</p>`;
  }
}
async function loadAdminOrders(append = false) {
  try {
    const form = $("#admin-orders-form");
    const query = new URLSearchParams({ limit: "30" });
    if (form.elements.state.value)
      query.set("state", form.elements.state.value);
    if (form.elements.user_id.value.trim())
      query.set("user_id", form.elements.user_id.value.trim());
    if (append && orderCursor) query.set("cursor", orderCursor);
    const data = await adminApi("/orders?" + query);
    orderRows = append ? [...orderRows, ...data.items] : data.items;
    orderCursor = data.next_cursor;
    $("#admin-orders-list").innerHTML =
      orderRows
        .map(
          (order) =>
            `<div class="admin-record"><strong>${esc({ pending: "待执行", submitting: "提交中", accepted: "已接单", rejected: "已拒绝", unknown: "结果未知，需核对", settled: "已结算", cancelled: "已取消" }[order.state] || order.state)}</strong> · ${esc(order.email || order.user_id)} · ${esc(order.account_label || "账户未标注")}<br>${esc(order.home_team || order.fixture_id)} vs ${esc(order.away_team || "")}<br>${esc(order.market_name)} · ${esc(period(order.period))} · ${esc(order.line ?? "")} · ${esc(selection(order.selection))}<br>预留本金 ${decimal(order.stake)} · 预留赔率 ${decimal(order.price)}<br>官方成交本金 ${order.provider_stake == null ? "未知" : decimal(order.provider_stake)} · 成交赔率 ${order.provider_price == null ? "未知" : decimal(order.provider_price)} · 币种 ${esc(order.provider_currency || "未知")}<br>实际净收益 ${order.settled_net == null ? (order.state === "settled" ? "官方回执未提供" : "待结算") : signed(order.settled_net)} · 手续费 ${order.provider_fees == null ? "未知" : decimal(order.provider_fees)}<br><span class="muted">官方下单 ${order.provider_placed_at ? stamp(order.provider_placed_at) : "未知"} · 官方结算 ${order.settled_at ? stamp(order.settled_at) : "未知"}<br>${stamp(order.created_at)} · ${esc(order.provider_order_no || "暂无上游回执")}<br>记录 ${esc(order.id)}</span></div>`,
        )
        .join("") || '<p class="empty-state">当前条件下没有订单</p>';
    $("#admin-orders-more").hidden = !orderCursor;
  } catch (error) {
    toast(error.message + "，保留上次只读订单。");
  }
}
async function loadStatus() {
  try {
    const data = await adminApi("/status");
    $("#admin-status").innerHTML = [
      ["数据库", data.database],
      [
        "Hermes 框架",
        {
          reachable: "框架可达 · 模型尚未验证",
          unavailable: "框架暂不可达 · 模型尚未验证",
          configured: "已配置 · 模型尚未验证",
          unconfigured: "未配置",
        }[data.hermes] || "状态未知 · 模型尚未验证",
      ],
      ["启用监控来源", data.telegram_sources],
      ["待审核任务", data.review_queue],
    ]
      .map(
        ([label, value]) =>
          `<div class="status-card"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`,
      )
      .join("");
  } catch (error) {
    $("#admin-status").innerHTML =
      `<div class="notice error">${esc(error.message)}</div>`;
  }
}
async function loadSettings() {
  try {
    const data = await adminApi("/settings");
    settings = data.items;
    leyuVersion = data.leyu_version;
    if (data.model_configuration) renderModelConfiguration(data.model_configuration);
    const runtime = data.runtime || {};
    const runtimeLabel = {
      applied: "采集器已应用",
      invalid: "新配置校验失败，仍用上版采集",
      pending: "等待采集器读取",
      unavailable: "采集器不可达",
    }[runtime.state] || "采集器状态未知";
    const appliedVersion = runtime.applied_version ?? "无回执";
    const reason = runtime.reason ? " · " + esc(runtime.reason) : "";
    $("#leyu-version").textContent =
      `配置版本 ${leyuVersion ?? "未建立"} · ${runtimeLabel} ${appliedVersion} · 最近校验 ${stamp(runtime.validated_at)}${reason}`;
    for (const form of $$(".settings-form"))
      for (const input of $$("input[name]", form)) {
        const item = settings[input.name];
        if (input.type === "checkbox")
          input.checked =
            item?.value === true ||
            String(item?.value).toLowerCase() === "true";
        else if (item?.masked) {
          input.value = "";
          input.placeholder = item.configured
            ? "已配置 · 留空保持现值"
            : "未配置";
        } else input.value = item?.value ?? "";
        input.title = item?.configured
          ? "已配置" + (item.updated_at ? " · " + stamp(item.updated_at) : "")
          : "未配置";
      }
  } catch (error) {
    toast(error.message);
  }
}
async function saveSettings(form) {
  const values = {};
  for (const input of $$("input[name]", form)) {
    if (input.type === "checkbox")
      values[input.name] = input.checked ? "true" : "false";
    else if (input.value.trim() || input.hasAttribute("data-save-empty"))
      values[input.name] = input.value.trim();
  }
  if (values.hermes_analysis_model === (settings.hermes_analysis_model?.value || ""))
    delete values.hermes_analysis_model;
  if (!Object.keys(values).length) throw new Error("没有需要保存的配置");
  const result = await adminApi("/settings", {
    method: "PUT",
    body: { values, expected_leyu_version: leyuVersion, expected_model_revision: modelConfiguration?.revision },
  });
  if (result.leyu_version) leyuVersion = result.leyu_version;
  toast(
    (result.application?.reason || "配置已保存") +
      (result.leyu_version ? " · 乐鱼版本 " + result.leyu_version : ""),
  );
  await loadSettings();
}
const modelStates = { pending_evidence: "等待比较证据", synthetic_diagnostic_only: "仅合成诊断，不能激活",
  awaiting_human_review: "等待人工逐项复核", reviewed: "已复核，等待显式激活", activated: "已激活" };
function renderModelConfiguration(data) {
  modelConfiguration = data;
  const active = data.active;
  const status = $("#model-configuration-state");
  if (!active || !status) return;
  status.textContent = '管理版本 ' + data.revision + ' · 当前模型 ' + (active.model || 'Hermes 默认路由') +
    ' · 生效版本 ' + active.version + ' · ' + (data.application_state === 'consumed' ? '工作器已消费' : '等待工作器消费') +
    ' · 最近消费 ' + stamp(data.consumption?.consumed_at) + '。' + (data.reason || '');
  const form = $("#model-candidate-form");
  for (const name of ['model_revision','prompt_sha256','skill_version','hermes_version','tool_contract_version'])
    if (!form.elements[name].value) form.elements[name].value = active.provenance[name] || '';
  $("#model-candidates").innerHTML = data.candidates.map(candidate => {
    const report = candidate.report;
    let content = '<details class="settings-section"><summary>' + esc(candidate.model || 'Hermes 默认路由') + ' · ' +
      esc(modelStates[candidate.status] || candidate.status) + '</summary><p class="form-note">候选 ' + esc(candidate.id) +
      ' · ' + stamp(candidate.created_at) + ' · 来源摘要 ' + esc(candidate.fingerprint) + '</p><pre>' +
      esc(JSON.stringify(candidate.provenance,null,2)) + '</pre>';
    if (!report) content += '<form class="compact-form model-evidence-form" data-candidate="' + esc(candidate.id) +
      '"><label>冻结比较报告 JSON<textarea name="report" rows="6" required maxlength="262144"></textarea></label>' +
      '<label>报告规范 JSON SHA-256<input name="report_sha256" pattern="[0-9a-f]{64}" required></label>' +
      '<p class="form-note">报告需来源版本与冻结样本摘要。合成报告只能诊断；真实标注来源仍须人工核验。</p>' +
      '<button class="primary-button">核验并冻结报告</button></form>';
    if (report) content += '<p class="form-note">报告 ' + esc(report.sha256) + ' · ' + esc(report.dataset_id) + ' · ' +
      report.changed_samples + ' 项差异 · ' + (report.synthetic_only ? '合成诊断' : '声明真实人工标注（需人工核验）') + '</p>';
    if (candidate.status === 'awaiting_human_review') {
      content += '<form class="compact-form model-review-form" data-candidate="' + esc(candidate.id) +
        '"><p class="form-note">必须逐项阅读全部差异；拒绝项将阻止激活。</p>';
      for (const item of report.differences || []) content += '<details class="settings-section"><summary>差异 ' + esc(item.sample_id) +
        '</summary><pre>' + esc(JSON.stringify(item,null,2)) + '</pre><label>结论<select name="decision:' + esc(item.sample_id) +
        '"><option value="reject">拒绝，暂不激活</option><option value="accept">接受此差异</option></select></label>' +
        '<label>核验说明<input name="note:' + esc(item.sample_id) + '" minlength="3" maxlength="500" required></label></details>';
      content += '<label>整体复核说明<textarea name="reason" minlength="3" maxlength="500" required></textarea></label>' +
        '<button class="primary-button">提交人工复核</button></form>';
    }
    if (candidate.status === 'reviewed') content += '<button class="primary-button" data-activate-model="' + esc(candidate.id) + '">确认激活此候选</button>';
    return content + '</details>';
  }).join('') || '<p class="form-note">暂无新候选。当前迁移基线继续生效。</p>';
}
async function loadModelConfiguration() {
  renderModelConfiguration(await adminApi('/model-configuration'));
}
function bindModelConfiguration() {
  const form = $('#model-candidate-form');
  if (!form) return;
  $('#refresh-model-configuration').onclick = () => loadModelConfiguration().catch(error=>toast(error.message));
  form.onsubmit = event => {
    event.preventDefault();
    submit(form, async () => {
      if (!modelConfiguration) await loadModelConfiguration();
      const fields = Object.fromEntries(new FormData(form));
      const provenance = Object.fromEntries(['model_revision','prompt_sha256','skill_version','hermes_version','tool_contract_version'].map(key=>[key,fields[key]]));
      provenance.model_id = fields.model;
      const result = await adminApi('/model-configuration/candidates', {method:'POST', body:{model:fields.model,
        provenance, expected_revision:modelConfiguration.revision}});
      renderModelConfiguration(result.configuration);
      toast('候选已冻结，当前模型继续生效');
    });
  };
  $('#model-candidates').onsubmit = event => {
    const target = event.target;
    event.preventDefault();
    submit(target, async () => {
      const candidate = modelConfiguration.candidates.find(item=>item.id===target.dataset.candidate);
      if (!candidate) throw new Error('候选已变化，请刷新');
      const fields = Object.fromEntries(new FormData(target));
      let action, body;
      if (target.classList.contains('model-evidence-form')) {
        if (fields.report.length > 262144) throw new Error('报告超出大小上限');
        let report; try { report=JSON.parse(fields.report); } catch { throw new Error('报告不是有效 JSON'); }
        action='evidence'; body={report,report_sha256:fields.report_sha256};
      } else {
        action='review'; body={report_sha256:candidate.report.sha256,candidate_fingerprint:candidate.fingerprint,
          reason:fields.reason, decisions:candidate.report.changed_sample_ids.map(id=>({sample_id:id,decision:fields['decision:'+id],note:fields['note:'+id]}))};
      }
      renderModelConfiguration(await adminApi('/model-configuration/candidates/'+candidate.id+'/'+action,
        {method:'POST',body:{...body,expected_revision:modelConfiguration.revision}}));
      toast(action==='evidence' ? '比较证据已冻结，请人工核验' : '人工复核已记录，尚未激活');
    });
  };
  $('#model-candidates').onclick = async event => {
    const button=event.target.closest('[data-activate-model]');
    if (!button) return;
    if (!await confirmAction('激活已复核模型将切换后续审核使用的模型，当前任务保留原快照。确认来源与全部差异已核验并激活？')) return;
    try {
      renderModelConfiguration(await adminApi('/model-configuration/candidates/'+button.dataset.activateModel+'/activate',
        {method:'POST',body:{expected_revision:modelConfiguration.revision}}));
      toast('候选已激活，等待工作器消费回执');
      await loadSettings();
    } catch(error) { toast(error.message); }
  };
}
async function loadSources(preferredId = null) {
  const previous = preferredId || $("#source-select").value;
  try {
    const data = await adminApi("/sources");
    sources = data.items;
    $("#source-select").innerHTML =
      '<option value="">选择监控来源</option>' +
      sources
        .map(
          (s) =>
            `<option value="${esc(s.id)}">${esc(s.display_name)} ${s.enabled ? "" : "[已停用]"}</option>`,
        )
        .join("");
    $("#source-select").value = sources.some((s) => s.id === previous)
      ? previous
      : sources[0]?.id || "";
    await selectSource();
  } catch (error) {
    $("#source-state").textContent = error.message;
  }
}
function selectedSource() {
  return sources.find((s) => s.id === $("#source-select").value);
}
async function selectSource() {
  ++adminsSequence;
  adminsQuery = "";
  $("#tg-admin-search").value = "";
  const source = selectedSource();
  $("#source-toggle").disabled = !source;
  $("#tg-admins").disabled = !source;
  $$("input,button", $("#tg-admin-search-form")).forEach(
    (element) => (element.disabled = !source),
  );
  $("#expert-source-form button").disabled = !source;
  $("#source-state").textContent = source
    ? `${source.platform_chat_id || source.chat_id} · ${source.enabled ? "监控中" : "已停用"} · ${source.allow_media ? "接收图片" : "仅文字"}`
    : "先添加或选择监控来源";
  $("#source-toggle").textContent = source?.enabled ? "停用来源" : "启用来源";
  $("#source-admins").innerHTML = "";
  adminsCursor = null;
  $("#tg-admins-more").hidden = true;
  $("#trusted-experts").innerHTML = "";
  if (!source) return;
  try {
    const data = await adminApi("/sources/" + source.id + "/experts");
    if (selectedSource()?.id !== source.id) return;
    $("#trusted-experts").innerHTML =
      "<h3>信任作者</h3>" +
      data.items
        .filter((expert) => expert.enabled !== false)
        .map(
          (e) =>
            `<div class="source-item"><span>${esc(e.display_nickname || e.nickname)}<br><span class="muted">${esc(e.platform_sender_id || e.sender_id)}</span></span><button data-untrust="${esc(e.id)}">移除</button></div>`,
        )
        .join("");
  } catch (error) {
    if (selectedSource()?.id !== source.id) return;
    $("#trusted-experts").textContent = error.message;
  }
  if (selectedSource()?.id === source.id) await listAdmins(false);
}
const tgNames = {
  idle: "未开始授权",
  waiting_qr: "等待扫码",
  waiting_password: "需要两步密码",
  authorized_pending_save: "授权成功，等待远端安全保存",
  authorized: "授权已保存",
  session_invalid: "旧会话已被 Telegram 废弃，请重新扫码授权",
  expired: "二维码已过期，请重新生成",
  failed: "授权失败",
  cancelled: "已取消授权",
};
function renderTelegram(data) {
  const auth = data.auth || data;
  qrAttempt = auth.attempt_id || null;
  $("#telegram-status").textContent =
    `${data.connected ? "连接已建立" : "当前未连接"} · ${data.authorized ? "已授权" : tgNames[auth.state] || auth.state || "待检查"}${data.account ? " · " + (data.account.display_name || data.account.username || data.account.id) : ""}`;
  $("#tg-password-form").hidden = auth.state !== "waiting_password";
  $("#tg-cancel").hidden =
    !qrAttempt ||
    !["waiting_qr", "waiting_password", "authorized_pending_save"].includes(
      auth.state,
    );
  $("#tg-qr-start").disabled = [
    "waiting_qr",
    "waiting_password",
    "authorized_pending_save",
  ].includes(auth.state);
  const target = $("#telegram-auth");
  target.innerHTML = "";
  if (
    auth.qr_url &&
    auth.state === "waiting_qr" &&
    /^tg:\/\/login\?token=/.test(auth.qr_url)
  ) {
    const qr = window.qrcode(0, "M");
    qr.addData(auth.qr_url);
    qr.make();
    target.innerHTML =
      qr.createSvgTag({ scalable: true, margin: 2 }) +
      `<p class="form-note">Telegram → 设置 → 设备 → 连接桌面设备<br>有效至 ${stamp(auth.expires_at)}</p>`;
    const svg = $("svg", target);
    svg.classList.add("qr-code");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "Telegram 官方登录二维码");
  } else if (auth.error) target.textContent = tgError(auth.error);
  else if (auth.state) target.textContent = tgNames[auth.state] || auth.state;
  clearTimeout(tgTimer);
  if (
    ["waiting_qr", "waiting_password", "authorized_pending_save"].includes(
      auth.state,
    ) &&
    app.state.page === "admin"
  )
    tgTimer = setTimeout(loadTelegram, 2500);
}
function tgError(reason) {
  return (
    {
      member_enumeration_restricted:
        "账号无权枚举群成员，可手动添加已核验作者 ID。",
      source_not_accessible: "当前账号无法访问该来源。",
      account_has_not_joined: "账号尚未加入该群组或频道。",
      account_has_not_joined_or_entity_is_unavailable:
        "账号尚未加入该来源，或来源目前不可访问。",
      source_not_group_or_channel: "该来源不是群组或频道。",
      source_migrated: "该群组已迁移，请核对迁移后的群组编号再更新来源。",
      invalid_pagination_cursor: "分页已失效，请重新搜索，不再继续旧游标。",
      invalid_search_query: "搜索内容无效，请缩短关键词或核对编号。",
      network_unavailable: "Telegram 网络暂不可用，请稍后重试。",
      incomplete_admin_page:
        "管理员分页未完整返回，请重试；本次结果不能视为完整名单。",
      directory_pagination_stalled: "目录分页没有继续前进，请重新搜索。",
      invalid_source_identifier: "群组标识无效，请检查数字 ID。",
      unauthorized: "Telegram 尚未授权。",
      flood_wait: "Telegram 触发频率限制，请等待后重试。",
    }[reason] || String(reason || "未知状态")
  );
}
async function loadTelegram() {
  try {
    renderTelegram(await adminApi("/telegram/status"));
  } catch (error) {
    $("#telegram-status").textContent = error.message;
  }
}
async function listDialogs(append = false) {
  const query = $("#tg-dialog-search").value.trim();
  if (append && query !== dialogsQuery) return listDialogs(false);
  if (!append) {
    dialogsCursor = null;
    dialogsQuery = query;
  }
  const sequence = ++dialogsSequence;
  const search = new URLSearchParams({ q: dialogsQuery });
  if (append && dialogsCursor) search.set("cursor", dialogsCursor);
  $("#tg-dialog-list").setAttribute("aria-busy", "true");
  $("#tg-dialogs-more").hidden = true;
  try {
    const data = await adminApi("/telegram/dialogs?" + search);
    if (sequence !== dialogsSequence) return;
    if (!["ok", "available", undefined].includes(data.state))
      throw new Error(
        tgError(data.reason || data.state) +
          (data.retry_after ? ` ${data.retry_after} 秒后重试。` : ""),
      );
    const html = data.items
      .map(
        (d) =>
          `<div class="source-item"><span>${esc(d.title)}<br><span class="muted">${esc(d.chat_id)}</span></span><button data-chat="${esc(d.chat_id)}" data-title="${esc(d.title)}">选用</button></div>`,
      )
      .join("");
    if (append) $("#tg-dialog-list").insertAdjacentHTML("beforeend", html);
    else
      $("#tg-dialog-list").innerHTML =
        html ||
        `<p class="muted" role="status">${dialogsQuery ? "没有匹配的群组或频道，可清空搜索或手工填写已核验 ID。" : "没有可访问的群组或频道"}</p>`;
    dialogsCursor = data.next_cursor;
    $("#tg-dialogs-more").hidden = !dialogsCursor;
  } catch (error) {
    if (sequence !== dialogsSequence) return;
    $("#tg-dialog-list").textContent = error.message;
  } finally {
    if (sequence === dialogsSequence)
      $("#tg-dialog-list").setAttribute("aria-busy", "false");
  }
}
async function listAdmins(append = false, scope = "admins") {
  const source = selectedSource();
  if (!source) return;
  const query = $("#tg-admin-search").value.trim();
  if (append && query !== adminsQuery) return listAdmins(false, scope);
  if (!append) {
    adminsCursor = null;
    adminsQuery = query;
  }
  const sequence = ++adminsSequence;
  const search = new URLSearchParams({
    chat_id: source.platform_chat_id || source.chat_id,
    q: adminsQuery,
  });
  if (append && adminsCursor) search.set("cursor", adminsCursor);
  $("#source-admins").setAttribute("aria-busy", "true");
  $("#tg-admins-more").hidden = true;
  try {
    const data = await adminApi("/telegram/" + scope + "?" + search);
    if (sequence !== adminsSequence) return;
    if (!["ok", "available", undefined].includes(data.state)) {
      $("#source-admins").innerHTML =
        `<p class="notice">${esc(tgError(data.reason || data.state))}${data.retry_after ? " " + esc(data.retry_after) + " 秒后重试。" : ""}</p>`;
      return;
    }
    const html = data.items
      .map(
        (e) => {
          const username = e.username ? `@${e.username}` : "无用户名";
          const role = e.admin_kind === "owner" ? "群主" : e.admin_kind === "administrator" ? "管理员" : e.admin_kind === "member" ? "普通成员" : "身份未确认";
          const avatar = e.has_avatar ? "头像可用" : e.avatar_state === "absent" ? "无头像" : "头像未读取";
          const anonymous = e.sender_identity_kind === "chat_sender" || String(e.sender_id).startsWith("-")
            ? " · 频道/群组发言身份，不代表具体匿名个人"
            : "";
          return `<div class="source-item"><span><strong>${esc(e.nickname || e.username || e.sender_id)}</strong><br><span class="muted">${esc(username)} · ${esc(role)} · ${esc(avatar)}<br>${esc(e.sender_id)}${anonymous}</span></span><button data-author="${esc(e.sender_id)}" data-nickname="${esc(e.nickname || e.username || e.sender_id)}">选用</button></div>`;
        },
      )
      .join("");
    if (append) $("#source-admins").insertAdjacentHTML("beforeend", html);
    else
      $("#source-admins").innerHTML =
        html ||
        `<p class="muted" role="status">${adminsQuery ? "没有匹配的作者，可清空搜索或手工添加已核验作者 ID。" : "未返回可枚举作者；匿名管理员可能不会列出。可手工添加已核验 ID。"}</p>`;
    $("#tg-admins-more").dataset.scope = scope;
    adminsCursor = data.next_cursor;
    $("#tg-admins-more").hidden = !adminsCursor;
  } catch (error) {
    if (sequence !== adminsSequence) return;
    $("#source-admins").textContent = error.message;
  } finally {
    if (sequence === adminsSequence)
      $("#source-admins").setAttribute("aria-busy", "false");
  }
}
async function loadAdminPlans() {
  try {
    const data = await adminApi("/membership/plans");
    adminPlans = data.items;
    $("#grant-plan").innerHTML = data.items
      .filter((p) => p.enabled !== false)
      .map(
        (p) =>
          `<option value="${esc(p.code)}">${esc(p.name)} (${esc(p.code)})</option>`,
      )
      .join("");
    $("#admin-plans").innerHTML = data.items
      .map(
        (p) =>
          `<details class="settings-section"><summary>${esc(p.name)} · ${p.enabled ? "启用" : "停用"}</summary><form class="compact-form plan-form" data-code="${esc(p.code)}"><label>名称<input name="name" value="${esc(p.name)}" required></label><label>价格<input name="price" type="number" min="0" max="1000000" step="0.01" value="${esc(p.price)}" required></label><label>币种<input name="currency" pattern="[A-Z]{3}" value="${esc(p.currency)}" required></label><label>天数<input name="duration_days" type="number" min="1" max="3650" value="${esc(p.duration_days)}" required></label>${["expert_full_history", "expert_live_signals", "automatic_betting"].map((key) => `<label class="check-label"><input type="checkbox" name="${key}" ${p.entitlements[key] ? "checked" : ""}>${esc({ expert_full_history: "完整记录", expert_live_signals: "滚球推荐", automatic_betting: "自动投注" }[key])}</label>`).join("")}<label class="check-label"><input type="checkbox" name="enabled" ${p.enabled ? "checked" : ""}>启用套餐</label><button class="primary-button">保存套餐</button></form></details>`,
      )
      .join("");
  } catch (error) {
    $("#admin-plans").textContent = error.message;
  }
}
async function listUsers(append = false) {
  try {
    const term = $("#user-search-form input").value.trim();
    const query = new URLSearchParams({ limit: "50", q: term });
    if (append && userCursor) query.set("after", userCursor);
    const data = await adminApi("/users?" + query);
    userRows = append ? [...userRows, ...data.items] : data.items;
    userCursor = data.next_cursor;
    $("#admin-users").innerHTML =
      userRows
        .map(
          (u) =>
            `<div class="admin-record"><strong>${esc(u.display_name)}</strong> ${esc(u.email)}<br><span class="muted">${esc(u.role)} · ${u.disabled_at ? "已禁用" : "有效"}</span><button data-user-memberships="${esc(u.id)}">查看授予记录</button>${u.role === "user" && !u.disabled_at ? `<button data-user-recovery="${esc(u.id)}" data-user-label="${esc(u.email)}">人工核验后恢复密码</button>` : ""}<div id="memberships-${esc(u.id)}"></div></div>`,
        )
        .join("") || '<div class="empty-state">没有符合搜索条件的用户</div>';
    if (userCursor)
      $("#admin-users").insertAdjacentHTML(
        "beforeend",
        '<button class="load-more" data-users-more>继续加载用户</button>',
      );
  } catch (error) {
    $("#admin-users").textContent = error.message;
  }
}
async function memberships(id) {
  try {
    const data = await adminApi("/users/" + id + "/memberships");
    $("#memberships-" + CSS.escape(id)).innerHTML =
      data.items
        .map(
          (m) =>
            `<div class="admin-record">${esc(m.name)} · ${esc(m.status)}<br>${stamp(m.starts_at)} 至 ${m.ends_at ? stamp(m.ends_at) : "长期"}<br>${esc(m.grant_reason || "")} ${m.status === "active" ? `<button data-revoke="${esc(m.id)}">撤销授权</button>` : ""}</div>`,
        )
        .join("") || "无授予记录";
  } catch (error) {
    toast(error.message);
  }
}
async function loadAudit() {
  try {
    const data = await adminApi("/audit?limit=100");
    $("#audit-list").innerHTML =
      data.items
        .map(
          (a) =>
            `<div class="audit-row">${stamp(a.created_at)} · ${esc(a.actor_role)} · ${esc(a.action)}<br>${esc(a.resource_type)} ${esc(a.resource_id)}<details><summary>操作详情</summary><pre class="raw-text">${jsonTrace(a.details)}</pre></details></div>`,
        )
        .join("") || '<p class="muted">暂无管理审计记录</p>';
  } catch (error) {
    $("#audit-list").textContent = error.message;
  }
}
function bind() {
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    try {
      if (['telegram','leyu','orders','system'].includes(button.dataset.adminTaskTarget)) {
        $(`.admin-tabs [data-admin-tab="${button.dataset.adminTaskTarget}"]`)?.click();
        return;
      }
      if (button.dataset.adminTab) {
        $$("[data-admin-tab]").forEach((x) => {
          x.classList.toggle("active", x === button);
          x.setAttribute("aria-pressed", String(x === button));
        });
        $$(".admin-panel").forEach(
          (x) => (x.hidden = x.id !== "admin-" + button.dataset.adminTab),
        );
        if (button.dataset.adminTab === "telegram") loadTelegram();
        if (button.dataset.adminTab === "orders") loadAdminOrders();
        return;
      }
      if (button.dataset.chat) {
        $("#source-form").elements.chat_id.value = button.dataset.chat;
        $("#source-form").elements.display_name.value = button.dataset.title;
        return;
      }
      if (button.dataset.author) {
        $("#expert-source-form").elements.sender_id.value =
          button.dataset.author;
        $("#expert-source-form").elements.nickname.value =
          button.dataset.nickname;
        return;
      }
      if (button.dataset.untrust) {
        if (
          !(await confirmAction("移除该来源的信任作者？已有历史记录仍保留。"))
        )
          return;
        await adminApi(
          "/sources/" +
            selectedSource().id +
            "/experts/" +
            button.dataset.untrust,
          { method: "DELETE" },
        );
        await selectSource();
        return;
      }
      if (button.hasAttribute("data-users-more")) return listUsers(true);
      if (button.dataset.userMemberships)
        return memberships(button.dataset.userMemberships);
      if (button.dataset.revoke) {
        const reason = prompt("请输入撤销原因（至少3个字）");
        if (!reason || reason.trim().length < 3) return;
        await adminApi(
          "/membership/grants/" + button.dataset.revoke + "/revoke",
          { method: "POST", body: { reason: reason.trim() } },
        );
        toast("会员授权已撤销");
        listUsers();
      }
    } catch (error) {
      toast(error.message);
    }
  });
  $$(".settings-form").forEach(
    (form) =>
      (form.onsubmit = (e) => {
        e.preventDefault();
        submit(form, () => saveSettings(form));
      }),
  );
  $("#refresh-admin").onclick = () => loadStatus();
  bindModelConfiguration();
  $("#source-select").onchange = () => selectSource();
  $("#source-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    submit(form, async () => {
      const fd = new FormData(form);
      const created = await adminApi("/sources", {
        method: "POST",
        body: {
          chat_id: fd.get("chat_id"),
          display_name: fd.get("display_name"),
          enabled: true,
          allow_media: fd.has("allow_media"),
        },
      });
      form.reset();
      toast("监控来源已添加");
      await loadSources(created.id);
    });
  };
  $("#expert-source-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    submit(form, async () => {
      if (!selectedSource()) throw new Error("请先选择监控来源");
      await adminApi("/sources/" + selectedSource().id + "/experts", {
        method: "POST",
        body: Object.fromEntries(new FormData(form)),
      });
      form.reset();
      toast("信任作者已添加");
      selectSource();
    });
  };
  $("#source-toggle").onclick = async () => {
    const s = selectedSource();
    if (!s) return;
    try {
      await adminApi("/sources/" + s.id, {
        method: "PUT",
        body: {
          chat_id: s.platform_chat_id || s.chat_id,
          display_name: s.display_name,
          enabled: !s.enabled,
          allow_media: s.allow_media,
        },
      });
      loadSources();
    } catch (error) {
      toast(error.message);
    }
  };
  $("#tg-refresh").onclick = () => loadTelegram();
  $("#tg-qr-start").onclick = async () => {
    try {
      const status = await adminApi("/telegram/status");
      if (status.connected || status.authorized) {
        if (
          !(await confirmAction(
            "已有 Telegram 会话，重新授权会替换当前连接。继续？",
          ))
        )
          return;
      }
      renderTelegram(
        await adminApi("/telegram/auth/qr", {
          method: "POST",
          body: { replace_existing: !!status.authorized || !!status.connected },
        }),
      );
    } catch (error) {
      toast(error.message);
    }
  };
  $("#tg-cancel").onclick = async () => {
    if (!qrAttempt) return;
    try {
      renderTelegram(
        await adminApi("/telegram/auth/cancel", {
          method: "POST",
          body: { attempt_id: qrAttempt },
        }),
      );
    } catch (error) {
      toast(error.message);
    }
  };
  $("#tg-disconnect").onclick = async () => {
    if (
      !(await confirmAction("断开 Telegram 并撤销远端保存会话？采集将停止。"))
    )
      return;
    try {
      await adminApi("/telegram/disconnect", { method: "POST", body: {} });
      toast("已断开并撤销会话");
      loadTelegram();
    } catch (error) {
      toast(error.message);
    }
  };
  $("#tg-password-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    submit(form, async () => {
      const password = form.elements.password.value;
      form.elements.password.value = "";
      renderTelegram(
        await adminApi("/telegram/auth/password", {
          method: "POST",
          body: { attempt_id: qrAttempt, password },
        }),
      );
    });
  };
  $("#tg-dialogs").onclick = () => listDialogs();
  $("#tg-dialogs-more").onclick = () => listDialogs(true);
  $("#tg-admins").onclick = () => listAdmins();
  $("#tg-admins-more").onclick = () => listAdmins(true, $("#tg-admins-more").dataset.scope || "admins");
  $("#tg-directory-scope").onchange = () => listAdmins(false, $("#tg-directory-scope").value);
  for (const kind of ["dialog", "admin"]) {
    const load = kind === "dialog" ? listDialogs : () => listAdmins(false, $("#tg-directory-scope").value);
    $("#tg-" + kind + "-search-form").onsubmit = (event) => {
      event.preventDefault();
      load();
    };
    $("#tg-" + kind + "-search-clear").onclick = () => {
      $("#tg-" + kind + "-search").value = "";
      load();
    };
  }
  $("#grant-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    submit(form, async () => {
      const body = Object.fromEntries(new FormData(form));
      body.days = Number(body.days);
      body.idempotency_key = grantKey;
      await adminApi("/membership/grants", { method: "POST", body });
      grantKey = newId();
      form.reset();
      toast("会员已授予");
    });
  };
  $("#grant-form").addEventListener("input", () => {
    grantKey = newId();
  });
  $("#user-search-form").onsubmit = (e) => {
    e.preventDefault();
    listUsers();
  };
  $("#refresh-audit").onclick = () => loadAudit();
  $("#refresh-diagnostics").onclick = () => loadDiagnostics();
  $("#admin-orders-form").onsubmit = (event) => {
    event.preventDefault();
    loadAdminOrders();
  };
  $("#admin-orders-more").onclick = () => loadAdminOrders(true);
  $("#admin-plans").addEventListener("submit", (e) => {
    const form = e.target.closest(".plan-form");
    if (!form) return;
    e.preventDefault();
    submit(form, async () => {
      const fd = new FormData(form);
      const original = adminPlans.find((p) => p.code === form.dataset.code);
      const body = {
        code: form.dataset.code,
        name: fd.get("name"),
        price: fd.get("price"),
        currency: fd.get("currency"),
        duration_days: Number(fd.get("duration_days")),
        enabled: fd.has("enabled"),
        entitlements: { ...original.entitlements },
      };
      for (const key of [
        "expert_full_history",
        "expert_live_signals",
        "automatic_betting",
      ])
        body.entitlements[key] = fd.has(key);
      await adminApi("/membership/plans/" + encodeURIComponent(body.code), {
        method: "PUT",
        body,
      });
      toast("套餐已保存");
      loadAdminPlans();
    });
  });
  $("#leyu-setting-search").oninput = (e) => {
    const term = e.target.value.toLowerCase();
    $$("#leyu-settings details label").forEach((label) => {
      label.hidden =
        !!term &&
        !label.textContent.toLowerCase().includes(term) &&
        !$("input", label).name.includes(term);
      if (!label.hidden && term) label.closest("details").open = true;
    });
  };
  const sourcePanel = $("#admin-telegram");
  const history = document.createElement("button");
  history.className = "small-button";
  history.textContent = "查看全部专家消息与审核";
  history.onclick = () => app.route("messages", { asAdmin: true });
  sourcePanel.append(history);
}
