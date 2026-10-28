import {
  showDialog,
  $,
  $$,
  esc,
  time,
  stamp,
  dayKey,
  shiftDay,
  percent,
  decimal,
  signed,
  color,
  phases,
  outcomes,
  selection,
  period,
  toast,
  stateBox,
  dialog as renderDialog,
  confirmAction,
  submit,
  api,
  jsonTrace,
} from "./ui.js";
import { initAdmin, loadAdmin } from "./admin.js";
import { initIdentity } from "./identity.js";
import { initMfa } from "./mfa.js";
const initial = new URLSearchParams(location.hash.split("?")[1] || "");
const state = {
  page: "scores",
  date: validDate(initial.get("date")) ? initial.get("date") : dayKey(),
  phase: ["all", "live", "scheduled", "finished", "cancelled"].includes(
    initial.get("phase"),
  )
    ? initial.get("phase")
    : "all",
  sport: "football",
  leagueIds: initial.getAll("league_ids").slice(0, 100),
  leagueOptions: [],
  leagueRequest: null,
  leagueError: null,
  q: "",
  matches: [],
  cursor: null,
  experts: [],
  expertFilters: { days: 30, phase: 'all', market_kind: 'all', source_id: '' },
  expertSequence: 0,
  signals: [],
  signalCursor: null,
  messages: [],
  messageCursor: null,
  user: null,
  admin: null,
  authTab: "login",
  messageContext: "user",
  favoritesOnly: false,
  followedExpertsOnly: false,
  matchAbort: null,
  matchSequence: 0,
  matchQueryKey: null,
  leagueSequence: 0,
  retryTimer: null,
  plans: [],
  mediaUrls: [],
  accountBound: false,
  accountSequence: 0,
  orders: [],
  orderCursor: null,
  dialogSequence: 0,
  pendingDetail: null,
};
function dialog(title, html) {
  clearTimeout(state.detailTimer);
  state.dialogSequence++;
  renderDialog(title, html);
}
let favorites = new Set();
let followedExperts = new Set();
try {
  const raw = JSON.parse(localStorage.getItem("qiuxun-favorites") || "[]");
  if (Array.isArray(raw))
    favorites = new Set(raw.filter((x) => typeof x === "string").slice(0, 500));
} catch {}
try {
  const raw = JSON.parse(
    localStorage.getItem("qiuxun-followed-experts") || "[]",
  );
  if (Array.isArray(raw))
    followedExperts = new Set(
      raw.filter((value) => typeof value === "string").slice(0, 500),
    );
} catch {}
function validDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return false;
  const date = new Date(value + "T12:00:00Z");
  return (
    Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value
  );
}
function params(values) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (Array.isArray(value)) {
      value.forEach((item) => query.append(key, item));
    } else if (value !== null && value !== undefined && value !== "")
      query.append(key, value);
  }
  return query;
}
function currentMatches() {
  return state.favoritesOnly
    ? state.matches.filter((m) => favorites.has(m.id))
    : state.matches;
}
function displayError(element, error, retry = true) {
  stateBox(element, {
    title: error.status === 403 ? "需要完整记录权益" : error.message,
    description:
      error.status === 403
        ? "登录有相应权益的账号，或查看会员方案。"
        : "已读内容会保留；稍后可重新加载。",
    retry,
  });
}
function route(
  page,
  { phase, asAdmin = false, replace = false, preserveHash = false } = {},
) {
  if (
    ![
      "scores",
      "experts",
      "messages",
      "membership",
      "account",
      "admin",
    ].includes(page)
  )
    page = "scores";
  if (phase) state.phase = phase;
  state.page = page;
  if (page === "messages") state.messageContext = asAdmin ? "admin" : "user";
  const hash =
    "#" +
    page +
    (page === "scores"
      ? "?" +
        params({
          date: state.date,
          phase: state.phase,
          league_ids: state.leagueIds,
        })
      : "");
  if (!preserveHash && location.hash !== hash)
    history[replace ? "replaceState" : "pushState"]({}, "", hash);
  $(".main-layout").dataset.page = page;
  $$(".view").forEach((x) =>
    x.classList.toggle("active", x.id === "view-" + page),
  );
  $$(".main-nav button,.mobile-bottom button").forEach((x) => {
    const genericScores = x.closest(".mobile-bottom")
      ? state.phase !== "scheduled"
      : !["scheduled", "finished"].includes(state.phase);
    x.classList.toggle(
      "active",
      x.dataset.page === page &&
        (x.dataset.phase
          ? x.dataset.phase === state.phase
          : page !== "scores" || genericScores),
    );
    if (x.classList.contains("active")) x.setAttribute("aria-current", "page");
    else x.removeAttribute("aria-current");
  });
  if (page === "scores") {
    renderDates();
    loadMatches();
    loadLeagues();
  }
  if (page === "experts") loadExperts();
  if (page === "membership") loadPlans();
  if (page === "messages") loadMessages();
  if (page === "account") loadAccount();
  if (page === "admin") loadAdmin();
  document.title =
    {
      scores: "足球即时比分",
      experts: "专家推荐",
      messages: "发言与审核原文",
      membership: "会员服务",
      account: "个人中心",
      admin: "独立管理中心",
    }[page] + " · 球讯";
}
function renderDates() {
  const selected = state.date;
  $("#date-picker").value = selected;
  $("#date-buttons").innerHTML = [-2, -1, 0, 1, 2]
    .map((offset) => {
      const date = shiftDay(selected, offset),
        today = dayKey();
      const label =
        date === today
          ? "今日"
          : date === shiftDay(today, -1)
            ? "昨日"
            : date === shiftDay(today, 1)
              ? "明日"
              : date.slice(5).replace("-", "/");
      return `<button data-date="${date}" aria-pressed="${offset === 0}" class="${offset === 0 ? "active" : ""}">${label}<small>${date.slice(5).replace("-", "/")}</small></button>`;
    })
    .join("");
  for (const button of $$(".score-tabs [data-phase],[data-sport]")) {
    const selected = button.dataset.phase
      ? button.dataset.phase === state.phase
      : button.dataset.sport === state.sport;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  }
  $("#score-title").textContent =
    state.sport === "basketball" ? "篮球即时比分" : "足球即时比分";
}
function setDate(date) {
  if (!validDate(date)) return;
  state.date = date;
  state.matches = [];
  route("scores", { replace: true });
}
function quoteLabel(q) {
  return `${q.market_name || q.market_kind || "盘口"} ${period(q.period)} ${q.line ?? ""} ${selection(q.selection)} ${decimal(q.price)}`;
}
function renderMatches() {
  const items = currentMatches();
  $("#matches-list").innerHTML = items
    .map((m) => {
      const all = m.markets || [],
        first = all.find((q) => q.is_main !== false) || all[0];
      const related = first
          ? all.filter(
              (q) =>
                q.market_id === first.market_id &&
                q.period === first.period &&
                q.line === first.line,
            )
          : [],
        quotes = related.slice(0, 3);
      const score = m.home_score === null || m.home_score === undefined
        ? (m.phase === "scheduled"
          ? '<img class="match-vs-icon" src="/assets/cc-reference/home_match_vs_icon.png" alt="VS" width="24" height="18">'
          : "－")
        : esc(m.home_score) + " : " + esc(m.away_score ?? "－");
      return `<article class="match-row" data-fixture="${esc(m.id)}"><div class="match-league"><span class="league-label" title="${esc(m.competition_name)}">${esc(m.competition_name || "联赛未标注")}</span><button class="favorite" data-favorite="${esc(m.id)}" aria-label="关注 ${esc(m.home_team)} 对 ${esc(m.away_team)}" aria-pressed="${favorites.has(m.id)}">${favorites.has(m.id) ? "★" : "☆"}</button></div><span class="match-time">${time(m.kickoff_at)}</span><span class="match-phase ${esc(m.phase)}">${m.phase === "live" && m.clock_minute !== null ? esc(m.clock_minute) + "′" : esc(phases[m.phase] || m.match_state || "未知")}</span><button class="team-button team-home" data-match="${esc(m.id)}">${esc(m.home_team)}</button><button class="score-button" data-match="${esc(m.id)}" aria-label="查看比赛详情">${score}</button><button class="team-button team-away" data-match="${esc(m.id)}">${esc(m.away_team)}</button><div class="match-odds">${quotes.length ? quotes.map((q) => `<span class="${q.fresh === false || !m.quote_fresh ? "stale" : ""}" title="${esc(quoteLabel(q))}">${esc(selection(q.selection))} ${esc(q.line ?? "")} ${decimal(q.price)}</span>`).join("") : '<span class="stale">暂无有效赔率</span>'}${all.length > quotes.length ? '<button class="detail-button" data-match="' + esc(m.id) + '">更多盘口 ›</button>' : ""}</div><button class="detail-button" data-match="${esc(m.id)}" aria-label="查看 ${esc(m.home_team)} 比赛详情">析</button></article>`;
    })
    .join("");
  $("#fixture-count").textContent =
    state.matches.length + (state.cursor ? "+" : "");
  $("#load-more").hidden = !state.cursor;
  if (items.length) $("#matches-state").hidden = true;
  else
    stateBox($("#matches-state"), {
      title: state.favoritesOnly
        ? "当前筛选下没有关注比赛"
        : "暂无符合条件的比赛",
      description: state.favoritesOnly
        ? "可先在全部赛事中点击星标关注。"
        : "尝试切换日期、状态或联赛；没有比赛不会补入演示数据。",
    });
}
async function loadMatches(append = false) {
  const queryKey = JSON.stringify([state.date, state.phase, state.sport, [...state.leagueIds].sort(), state.q]);
  if (queryKey !== state.matchQueryKey) {
    state.matchAbort?.abort();
    ++state.matchSequence;
    state.matchQueryKey = queryKey;
    state.matches = [];
    state.cursor = null;
    append = false;
    renderMatches();
  }
  if (!navigator.onLine) {
    $("#matches-notice").hidden = false;
    $("#matches-notice").textContent = "筛选已更新；网络断开，恢复连接后重新读取赛事。";
    return;
  }
  clearTimeout(state.retryTimer);
  state.matchAbort?.abort();
  const controller = new AbortController();
  state.matchAbort = controller;
  const sequence = ++state.matchSequence;
  const button = $("#load-more");
  button.disabled = true;
  if (!append && !state.matches.length)
    stateBox($("#matches-state"), { loading: true });
  $("#matches-notice").hidden = true;
  $("#matches-notice").className = "notice";
  try {
    const targetCount = append ? 50 : Math.max(50, state.matches.length);
    const refreshPages = !append && state.matches.length > 50;
    const refreshDeadline = performance.now() + 30000;
    let cursor = append ? state.cursor : null;
    const items = [];
    let result;
    const seenCursors = new Set();
    do {
      if (performance.now() > refreshDeadline)
        throw new Error("赛事刷新超过30秒，请稍后重试");
      result = await api(
        "/public/matches?" +
          params({
            date: state.date,
            tz: "Asia/Shanghai",
            phase: state.phase,
            sport: state.sport,
            league_ids: state.leagueIds,
            q: state.q,
            cursor,
            page_size: Math.min(200, Math.max(50, targetCount - items.length)),
          }),
        { signal: controller.signal },
      );
      if (sequence !== state.matchSequence) return;
      const ids = new Set(items.map((match) => match.id));
      items.push(...result.items.filter((match) => !ids.has(match.id)));
      cursor = result.next_cursor;
      if (cursor && seenCursors.has(cursor))
        throw new Error("赛事分页游标重复，请稍后重试");
      if (cursor) seenCursors.add(cursor);
    } while (refreshPages && cursor && items.length < targetCount);
    if (sequence !== state.matchSequence) return;
    const scrollPosition = scrollY;
    state.matches = append
      ? [
          ...state.matches,
          ...items.filter((m) => !state.matches.some((old) => old.id === m.id)),
        ]
      : items;
    state.cursor = cursor;
    renderMatches();
    if (!append) scrollTo({ top: scrollPosition, behavior: "instant" });
    $("#last-updated").textContent = "更新 " + time(result.server_time);
    const stale = state.matches.filter(
      (m) => m.markets?.length && !m.quote_fresh,
    ).length;
    $("#service-state").textContent =
      `赛事服务已响应 · ${state.matches.length} 场已载入${stale ? " · " + stale + " 场报价过期或时间未知" : ""}`;
    if (stale) {
      $("#matches-notice").hidden = false;
      $("#matches-notice").textContent =
        `${stale} 场比赛报价过期或时间未知，保留显示供查询；详情会标明报价状态。`;
    }
  } catch (error) {
    if (controller.signal.aborted || sequence !== state.matchSequence) return;
    $("#service-state").textContent = "赛事服务暂未响应";
    if (state.matches.length) {
      $("#matches-notice").hidden = false;
      $("#matches-notice").className = "notice error";
      $("#matches-notice").textContent =
        error.message + "。保留上次结果，30秒后重试。";
    } else displayError($("#matches-state"), error);
    if (state.page === "scores")
      state.retryTimer = setTimeout(() => loadMatches(), 30000);
  } finally {
    if (sequence === state.matchSequence) button.disabled = false;
  }
}
function leagueLabel() {
  $("#league-filter").textContent = state.leagueIds.length
    ? "联赛 (" + state.leagueIds.length + ") ▾"
    : "全部联赛 ▾";
}
function loadLeagues() {
  const sequence = ++state.leagueSequence;
  state.leagueError = null;
  state.leagueOptions = [];
  state.leagueRequest = (async () => {
   try {
    const data = await api(
      "/public/leagues?" +
        params({
          date: state.date,
          phase: state.phase,
          sport: state.sport,
          tz: "Asia/Shanghai",
        }),
    );
    if (sequence !== state.leagueSequence) return;
    state.leagueOptions = data.items;
    leagueLabel();
  } catch (error) {
    if (sequence !== state.leagueSequence) return;
    state.leagueError = error.message;
    $("#league-filter").title = error.message;
  }
  })();
  return state.leagueRequest;
}
async function chooseLeagues() {
  dialog('选择联赛', '<div class="empty-state" role="status">正在读取当前联赛目录…</div>');
  const sequence = state.dialogSequence;
  const leagueSequence = state.leagueSequence;
  await (state.leagueRequest || loadLeagues());
  if (sequence !== state.dialogSequence || !$("#content-dialog").open || leagueSequence !== state.leagueSequence) return;
  if (state.leagueError) {
    $("#dialog-content").innerHTML = '<div class="empty-state">' + esc(state.leagueError) +
      '<br><button class="small-button" id="retry-league-directory">重新读取联赛</button></div>';
    $("#retry-league-directory").onclick = () => { loadLeagues(); chooseLeagues(); };
    return;
  }
  dialog(
    "选择联赛",
    `<section class="detail-section"><p class="form-note">当前日期与状态下的完整联赛目录，可多选（最多100项）。</p><div class="league-choices">${state.leagueOptions.map((l) => `<label class="check-label"><input type="checkbox" data-league-id="${esc(l.competition_id)}" ${state.leagueIds.includes(l.competition_id) ? "checked" : ""} ${l.competition_id === null ? "disabled" : ""}><span>${esc(l.competition_name)} <small>(${esc(l.match_count)})</small></span></label>`).join("") || '<p class="muted">暂无可用联赛，请切换日期。</p>'}</div><div class="form-actions"><button class="small-button" data-clear-leagues>清除筛选</button><button class="primary-button" data-apply-leagues>应用筛选</button></div></section>`,
  );
}
export function signalCard(s, renderContext = "user") {
  const hasFixture = s.home_team && s.away_team;
  return `<article class="signal-card"><div class="signal-meta"><button data-expert="${esc(s.expert_id)}">${esc(s.display_nickname || "专家昵称未记录")}</button><span>${stamp(s.created_at)}</span><span class="badge ${esc(s.phase)}">${esc(phases[s.phase] || s.phase || "阶段未知")}</span><span class="badge ${esc(s.outcome)}">${esc(outcomes[s.outcome] || s.outcome || "待结算")}</span></div><div class="signal-match">${hasFixture ? esc(s.home_team) + ' <span class="muted">vs</span> ' + esc(s.away_team) : "赛事编号：" + esc(s.fixture_id)}</div><div class="muted">${esc(s.competition_name || "")} ${s.kickoff_at ? stamp(s.kickoff_at) : ""}</div><div class="signal-pick"><span>${esc(s.market_name)} · ${esc(period(s.period))} · ${esc(lineLabel(s))} · <strong>${esc(selection(s.selection))}</strong></span><span>系统赔率 <strong>${decimal(s.recommendation_price)}</strong></span></div><div class="signal-evidence">报价时间：${stamp(s.quote_time)}　报价编号：${esc(s.quote_id || "历史记录未提供")} ${s.score_baseline?.home !== null && s.score_baseline?.home !== undefined ? "　比分基线 " + esc(s.score_baseline.home) + ":" + esc(s.score_baseline.away) : ""}</div><div class="signal-actions">${s.fixture_id ? `<button class="text-link" data-match="${esc(s.fixture_id)}" data-quote="${esc(s.quote_id || "")}">系统比赛与盘口</button>` : ""}<button class="text-link" ${s.message_id ? `data-message="${esc(s.message_id)}" data-context="${renderContext}"` : `data-signal="${esc(s.id)}"`}>完整原文与审核</button><span class="${color(s.unit_net)}">${s.outcome !== "pending" && s.unit_net !== null ? "1单位净收益 " + signed(s.unit_net) : "等待赛果结算"}</span></div></article>`;
}
function renderExperts() {
  const experts = state.experts;
  $("#experts-list").innerHTML = experts
    .map(
      (e, i) =>
        `<article class="expert-row" data-expert-row="${esc(e.id)}"><div class="expert-identity"><button class="expert-name" data-expert="${esc(e.id)}"><span class="avatar">${esc((e.display_nickname || "?").slice(0, 1))}</span><span><strong>${esc(e.display_nickname)}</strong><small>${i + 1} 位 · 近${state.expertFilters.days}天 ${esc(e.total_signals ?? 0)} 条 · 证据不足排除 ${esc(e.excluded_signals ?? 0)}</small></span></button><button class="expert-follow" data-follow-expert="${esc(e.id)}" aria-label="关注 ${esc(e.display_nickname)}，仅当前浏览器" aria-pressed="${followedExperts.has(e.id)}" title="关注仅保存于当前浏览器，不启用自动投注">${followedExperts.has(e.id) ? "★" : "☆"}</button></div><span>${esc(e.settled_signals ?? 0)}</span><span>${percent(e.accuracy)}</span><span class="${color(e.unit_net)}">${e.settled_signals ? signed(e.unit_net) : "样本不足"}</span></article>`,
    )
    .join("");
  applyExpertFollowing();
  $("#top-experts").innerHTML =
    experts
      .slice(0, 5)
      .map(
        (e, i) =>
          `<button class="side-expert" data-expert="${esc(e.id)}"><span class="rank-number">${i + 1}</span><span class="rank-detail"><strong>${esc(e.display_nickname)}</strong><small>${esc(e.settled_signals ?? 0)} 条已结算</small></span><span class="${color(e.unit_net)}">${e.settled_signals ? signed(e.unit_net) : "—"}</span></button>`,
      )
      .join("") || '<p class="muted">暂无专家统计</p>';
  const select = $("#message-expert"),
    previous = select.value;
  select.innerHTML =
    '<option value="">选择专家查看完整发言</option>' +
    experts
      .map(
        (e) =>
          `<option value="${esc(e.id)}">${esc(e.display_nickname)}</option>`,
      )
      .join("");
  select.value = previous;
  $("#bet-experts").innerHTML = experts
    .map(
      (e) =>
        `<label><input type="checkbox" name="expert" value="${esc(e.id)}">${esc(e.display_nickname)}</label>`,
    )
    .join("");
}
function applyExpertFollowing() {
  $$("[data-expert-row]").forEach((row) => {
    row.hidden =
      state.followedExpertsOnly && !followedExperts.has(row.dataset.expertRow);
  });
  $("#experts-follow-empty").hidden =
    !state.followedExpertsOnly ||
    state.experts.some((expert) => followedExperts.has(expert.id));
}
async function loadExperts() {
  const sequence = ++state.expertSequence;
  if (!state.experts.length) stateBox($("#experts-state"), { loading: true });
  try {
    const data = await api("/public/experts?" + params(state.expertFilters));
    if (sequence !== state.expertSequence) return;
    if (data.source_options) {
      const select = $('#expert-stats-filter [name=source_id]');
      select.innerHTML = '<option value="">全部来源</option>' + data.source_options.map(s => `<option value="${esc(s.id)}">${esc(s.display_name)}</option>`).join('');
      select.value = state.expertFilters.source_id;
    }
    state.experts = data.items;
    renderExperts();
    $("#experts-state").hidden = !!data.items.length;
    if (!data.items.length)
      stateBox($("#experts-state"), {
        title: "暂无专家记录",
        description: "已启用的可信作者及其真实推荐会显示在这里。",
      });
    await loadSignals();
  } catch (error) {
    if (sequence !== state.expertSequence) return;
    if (!state.experts.length) displayError($("#experts-state"), error);
    else toast(error.message + "，保留上次专家记录");
  }
}
async function loadSignals(append = false) {
  try {
    const data = await api(
      "/public/signals?" +
        params({ page_size: 20, cursor: append ? state.signalCursor : null }),
    );
    state.signals = append ? [...state.signals, ...data.items] : data.items;
    state.signalCursor = data.next_cursor;
    $("#signals-list").innerHTML =
      state.signals.map((s) => signalCard(s)).join("") ||
      '<div class="empty-state">暂无已绑定比赛与盘口的推荐</div>';
    $("#signals-more").hidden = !data.next_cursor;
    $("#signals-limited").hidden = !data.limited;
  } catch (error) {
    $("#signals-list").innerHTML =
      `<div class="empty-state">${esc(error.message)}</div>`;
  }
}
async function loadMessages(append = false) {
  const expertId = $("#message-expert").value;
  const target = $("#messages-state");
  if (!expertId && state.messageContext !== "admin") {
    stateBox(target, {
      title: "选择一位专家",
      description:
        "按 Telegram 稳定作者查询全部原文，包括未解决和审核失败的消息。",
    });
    $("#messages-list").innerHTML = "";
    $("#messages-more").hidden = true;
    return;
  }
  stateBox(target, { loading: true });
  try {
    const path =
      state.messageContext === "admin"
        ? "/admin/telegram/messages?" +
          params({
            expert_id: expertId,
            limit: 30,
            cursor: append ? state.messageCursor : null,
          })
        : "/public/experts/" +
          encodeURIComponent(expertId) +
          "/messages?" +
          params({ limit: 30, cursor: append ? state.messageCursor : null });
    const data = await api(path, { context: state.messageContext });
    state.messages = append ? [...state.messages, ...data.items] : data.items;
    state.messageCursor = data.next_cursor;
    target.hidden = true;
    $("#messages-list").innerHTML = state.messages
      .map(
        (m) =>
          `<article class="message-card"><div class="message-meta"><strong>${esc(m.display_nickname)}</strong><span>${stamp(m.posted_at)}</span><span class="badge">${esc(analysisState(m.analysis_state || m.state))}</span>${m.deleted_at ? '<span class="badge">原消息已删除</span>' : ""}${m.edited_at ? "<span>已编辑 · 版本 " + esc(m.revision) + "</span>" : ""}</div><div class="message-preview">${esc(m.raw_text || "仅图片消息")}</div><div class="muted">${(m.media_objects || []).length} 张图片 ${m.media_state === "pending" ? "· 媒体待上传" : ""}</div><button class="text-link" data-message="${esc(m.id)}" data-context="${state.messageContext}">阅读原文、模型输出与对应买入</button></article>`,
      )
      .join("");
    $("#messages-more").hidden = !data.next_cursor;
    if (!state.messages.length)
      stateBox(target, {
        title: "该专家暂无发言记录",
        description: "新消息持久化后会出现在这里。",
      });
  } catch (error) {
    displayError(target, error);
  }
}
function analysisState(value) {
  return (
    {
      queued: "等待审核",
      running: "审核中",
      retry: "等待重试",
      accepted: "已接受",
      completed: "已完成",
      unresolved: "未解决",
      failed: "审核失败",
      rejected: "未接受",
      succeeded: "已完成",
    }[value] ||
    value ||
    "尚无审核结果"
  );
}
function lineLabel(q) {
  const value = q.line;
  if (value === null || value === undefined) return "";
  return (q.market_kind === "handicap" || /让球/.test(q.market_name || "")) &&
    Number(value) > 0
    ? "+" + value
    : String(value);
}
function marketMarkup(markets, filters = { period: "all", scope: "all" }) {
  const periods = [...new Set(markets.map((q) => q.period))];
  const controls = `<div class="filter-row"><label>期间 <select id="market-period"><option value="all">全部期间</option>${periods.map((p) => `<option value="${esc(p)}" ${p === filters.period ? "selected" : ""}>${esc(period(p))}</option>`).join("")}</select></label><label>盘口线 <select id="market-scope"><option value="all">全部盘口</option><option value="main" ${filters.scope === "main" ? "selected" : ""}>主线</option><option value="additional" ${filters.scope === "additional" ? "selected" : ""}>附加线</option></select></label></div>`;
  const groups = new Map();
  for (const quote of markets) {
    const key = quote.period + " / " + quote.market_name;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(quote);
  }
  return (
    controls +
    [...groups.entries()]
      .map(
        ([key, quotes]) =>
          `<section class="market-group" data-period="${esc(quotes[0].period)}"><h4>${esc(period(quotes[0].period))} · ${esc(quotes[0].market_name)}</h4><div class="table-scroll"><table class="market-table"><thead><tr><th>盘口线 / 方向</th><th>系统赔率</th><th>报价状态 / 时间</th></tr></thead><tbody>${quotes.map((q) => `<tr data-main="${q.is_main !== false}" data-quote="${esc(q.quote_id)}"><td>${esc(lineLabel(q))} · ${esc(selection(q.selection))} <small>${q.is_main === false ? "附加线" : "主线"}</small><br><small>${esc(q.quote_id || "无编号")}</small></td><td class="${q.change > 0 ? "price-up" : q.change < 0 ? "price-down" : ""}">${decimal(q.price)}${q.change > 0 ? " ↑" : q.change < 0 ? " ↓" : ""}</td><td>${q.status && !["active", "open"].includes(q.status) ? esc({ suspended: "停盘", closed: "关闭" }[q.status] || q.status) : q.fresh ? "有效" : "过期 / 时间未知"}<br><small>${stamp(q.source_updated_at)}</small></td></tr>`).join("")}</tbody></table></div></section>`,
      )
      .join("")
  );
}
function filterMarkets() {
  const filters = {
    period: $("#market-period").value,
    scope: $("#market-scope").value,
  };
  state.matchFilters = filters;
  $$(".market-group").forEach((group) => {
    group.hidden =
      filters.period !== "all" && group.dataset.period !== filters.period;
    $$("tbody tr", group).forEach((row) => {
      row.hidden =
        filters.scope === "main"
          ? row.dataset.main !== "true"
          : filters.scope === "additional"
            ? row.dataset.main === "true"
            : false;
    });
  });
}
async function openMatch(id, { refresh = false, quoteId = null } = {}) {
  setDetail("match", id);
  const same = state.activeMatch === id,
    scroll = $("#content-dialog").scrollTop;
  if (!same) state.matchFilters = { period: "all", scope: "all" };
  if (!same) {
    state.quotePrices = new Map();
    state.focusQuote = null;
  }
  if (quoteId) state.focusQuote = quoteId;
  state.activeMatch = id;
  if (!refresh)
    dialog("比赛详情", '<div class="empty-state">正在读取比赛与盘口…</div>');
  const sequence = ++state.dialogSequence;
  try {
    const data = await api("/public/matches/" + encodeURIComponent(id));
    const wanted = refresh && same ? (state.matchMarketCount || 0) : 0;
    while (data.next_market_cursor &&
      (data.markets.length < wanted || (quoteId && !data.markets.some((q) => q.quote_id === quoteId)))) {
      if (state.activeMatch !== id || sequence !== state.dialogSequence) return;
      const next = await api("/public/matches/" + encodeURIComponent(id) + "/markets?" +
        params({ cursor: data.next_market_cursor }));
      data.markets.push(...next.markets);
      data.next_market_cursor = next.next_market_cursor;
    }
    const f = data.fixture;
    if (state.activeMatch !== id || sequence !== state.dialogSequence) return;
    const previous = state.quotePrices || new Map();
    for (const q of data.markets) {
      const old = previous.get(q.quote_id);
      q.change = old === undefined ? null : Number(q.price) - Number(old);
    }
    state.quotePrices = new Map(data.markets.map((q) => [q.quote_id, q.price]));
    if (state.activeMatch !== id || sequence !== state.dialogSequence) return;
    $("#dialog-title").textContent = f.home_team + " vs " + f.away_team;
    $("#dialog-content").innerHTML =
      `<section class="detail-hero"><p>${esc(f.competition_name)} · ${stamp(f.kickoff_at)}</p><div class="detail-teams"><h3>${esc(f.home_team)}</h3><strong>${f.home_score === null || f.home_score === undefined ? "VS" : esc(f.home_score) + " : " + esc(f.away_score)}</strong><h3>${esc(f.away_team)}</h3></div><p>${esc(phases[f.phase] || f.match_state)} ${f.phase === "live" && f.clock_minute !== null ? esc(f.clock_minute ?? "") + "′" : ""} · 上游更新 ${stamp(f.source_updated_at)}</p><button class="small-button" id="refresh-detail">刷新本场数据</button></section><section class="detail-section"><h3>系统当前盘口</h3>${data.markets.length ? marketMarkup(data.markets, state.matchFilters) : '<div class="empty-state">暂无有效赔率。上游尚未提供盘口或当前已停盘。</div>'}</section><section class="detail-section"><h3>本场专家推荐</h3>${data.expert_signals.map((s) => signalCard({ ...s, home_team: f.home_team, away_team: f.away_team, competition_name: f.competition_name, fixture_id: f.id })).join("") || '<p class="muted">暂无已绑定本场的专家推荐</p>'}</section><p class="data-footnote">当前未提供阵容、直播事件及球队历史资料，不生成推测信息。</p>`;
    $("#refresh-detail").onclick = () => openMatch(id, { refresh: true });
    const marketSection = $("#dialog-content .detail-section");
    const renderMarkets = () => {
      state.matchMarketCount = data.markets.length;
      state.quotePrices = new Map(data.markets.map((q) => [q.quote_id, q.price]));
      marketSection.innerHTML = '<h3>系统当前盘口</h3>' + (data.markets.length
        ? marketMarkup(data.markets, state.matchFilters)
        : '<div class="empty-state">暂无有效赔率。上游尚未提供盘口或当前已停盘。</div>') +
        (data.next_market_cursor ? '<button class="small-button" id="match-markets-more">继续加载盘口</button>' : '');
      if (data.markets.length) {
        $("#market-period").onchange = filterMarkets;
        $("#market-scope").onchange = filterMarkets;
        filterMarkets();
      }
      const button = $("#match-markets-more");
      if (button) button.onclick = async () => {
        if (button.disabled) return;
        button.disabled = true;
        clearTimeout(state.detailTimer);
        try {
          const next = await api("/public/matches/" + encodeURIComponent(id) + "/markets?" +
            params({ cursor: data.next_market_cursor }));
          if (state.activeMatch !== id || sequence !== state.dialogSequence) return;
          const known = new Set(data.markets.map((q) => q.quote_id));
          data.markets.push(...next.markets.filter((q) => !known.has(q.quote_id)));
          data.next_market_cursor = next.next_market_cursor;
          renderMarkets();
        } catch (error) {
          if (sequence === state.dialogSequence) toast(error.message + '，保留已加载盘口，可重试');
        } finally {
          if (state.activeMatch === id && sequence === state.dialogSequence) {
            button.disabled = false;
            state.detailTimer = setTimeout(() => {
              if (!document.hidden) openMatch(id, { refresh: true });
            }, 20000);
          }
        }
      };
    };
    renderMarkets();
    if (data.markets.length) {
      $("#market-period").onchange = filterMarkets;
      $("#market-scope").onchange = filterMarkets;
      if (quoteId) {
        const target = data.markets.find((q) => q.quote_id === quoteId);
        if (target) {
          $("#market-period").value = target.period;
          $("#market-scope").value = "all";
        } else toast("推荐报价已不在当前盘口，请查阅推荐时的报价快照");
      }
      filterMarkets();
    }
    if (quoteId) {
      const row = $$("[data-quote]").find(
        (row) => row.dataset.quote === quoteId && row.tagName === "TR",
      );
      if (row) {
        row.classList.add("focused-quote");
        row.scrollIntoView({ block: "center" });
      }
    }
    if (refresh) $("#content-dialog").scrollTop = scroll;
  } catch (error) {
    if (sequence !== state.dialogSequence) return;
    if (refresh) toast(error.message + "，保留上次本场数据");
    else
      $("#dialog-content").innerHTML =
        `<div class="empty-state">${esc(error.message)}<br><button class="small-button" data-match="${esc(id)}">重新加载</button></div>`;
  } finally {
    if (sequence === state.dialogSequence) {
      clearTimeout(state.detailTimer);
      if ($("#content-dialog").open)
        state.detailTimer = setTimeout(() => {
          if (!document.hidden) openMatch(id, { refresh: true });
        }, 20000);
    }
  }
}
async function openExpert(id) {
  setDetail("expert", id);
  dialog("专家档案", '<div class="empty-state">正在读取专家统计…</div>');
  const sequence = state.dialogSequence;
  try {
    const [data, trend] = await Promise.all([
      api("/public/experts/" + encodeURIComponent(id) + "?" + params(state.expertFilters)),
      api("/public/experts/" + encodeURIComponent(id) + "/trend?" + params(state.expertFilters)),
    ]);
    if (sequence !== state.dialogSequence) return;
    const e = data.expert;
    const detailWindow = `近${state.expertFilters.days}天 · ${stamp(data.from_utc)} 至 ${stamp(data.to_utc)}`;
    const qualityNote = `证据不足排除 ${esc(e.excluded_signals ?? 0)} 条（缺报价 ${esc(e.missing_quote_evidence ?? 0)}，结算证据无效 ${esc(e.invalid_settlement_evidence ?? 0)}）。`;
    $("#dialog-title").textContent = e.display_nickname;
    $("#dialog-content").innerHTML =
      `<section class="detail-section"><div class="expert-name"><span class="avatar">${esc(e.display_nickname.slice(0, 1))}</span><h2>${esc(e.display_nickname)}</h2></div><p class="muted">${detailWindow}</p><div class="metrics-grid"><div><small>已结算推荐</small><strong>${esc(e.settled_signals)}</strong></div><div><small>加权准确率</small><strong>${percent(e.accuracy)}</strong></div><div><small>1单位净收益</small><strong>${e.settled_signals ? signed(e.unit_net) : "样本不足"}</strong></div><div><small>有效本金 ROI</small><strong>${percent(e.unit_roi)}</strong></div></div><p class="muted">全赢 ${e.win ?? 0} · 半赢 ${e.half_win ?? 0} · 全输 ${e.loss ?? 0} · 半输 ${e.half_loss ?? 0} · 走盘 ${e.push ?? 0} · 作废 ${e.void ?? 0} · 待结算 ${e.pending ?? 0}</p><p class="form-note">平均每条净收益 ${e.settled_signals ? signed(e.average_unit_net) : "样本不足"}；准确率按赢/输有效份额加权，走盘与作废排除。统计基于不可变系统报价及最新赛果结算证据。${qualityNote}</p></section><section class="detail-section"><h3>累计一单位净收益走势</h3>${chart(trend.items)}<p class="form-note">与所选统计窗口一致，按UTC结算日累计；无结算日不补收益。</p></section><section class="detail-section"><button class="primary-button" data-expert-messages="${esc(id)}">查看该专家全部发言与审核原文</button></section>`;
  } catch (error) {
    if (sequence !== state.dialogSequence) return;
    $("#dialog-content").innerHTML =
      `<div class="empty-state">${esc(error.message)}</div>`;
  }
}
function chart(items) {
  if (!items.length)
    return '<div class="empty-state">暂无已结算样本，无法绘制收益走势</div>';
  const values = items.map((i) => Number(i.cumulative_unit_net));
  const low = Math.min(0, ...values),
    high = Math.max(0, ...values),
    range = high - low || 1;
  const points = values
    .map(
      (v, i) =>
        `${45 + i * (610 / Math.max(1, values.length - 1))},${140 - ((v - low) / range) * 110}`,
    )
    .join(" ");
  return `<svg class="yield-chart" viewBox="0 0 680 170" role="img" aria-label="累计一单位净收益走势，起始 ${esc(signed(values[0]))}，最新 ${esc(signed(values.at(-1)))}"><line x1="45" x2="655" y1="${140 - ((0 - low) / range) * 110}" y2="${140 - ((0 - low) / range) * 110}" stroke="#d9d2e6" stroke-dasharray="4 4"/><polyline points="${points}" fill="none" stroke="#8064b1" stroke-width="2.5"/><text x="4" y="35" fill="#999" font-size="10">${high.toFixed(2)}</text><text x="4" y="140" fill="#999" font-size="10">${low.toFixed(2)}</text><text x="45" y="160" fill="#999" font-size="10">${esc(String(items[0].day).slice(0, 10))}</text><text x="580" y="160" fill="#999" font-size="10">${esc(String(items.at(-1).day).slice(0, 10))}</text></svg>`;
}
async function openSignal(id) {
  dialog("推荐原文", '<div class="empty-state">正在读取推荐…</div>');
  const sequence = state.dialogSequence;
  try {
    const s = await api("/public/signals/" + encodeURIComponent(id), {
      context: "user",
    });
    if (sequence !== state.dialogSequence) return;
    if (s.message_id) return openMessage(s.message_id, "user");
    $("#dialog-content").innerHTML =
      `<section class="detail-section"><h3>消息原文</h3><pre class="raw-text">${esc(s.raw_text)}</pre></section>${signalCard(s)}${traceMarkup(s.analysis_trace || [])}`;
  } catch (error) {
    if (sequence !== state.dialogSequence) return;
    $("#dialog-content").innerHTML =
      `<div class="empty-state">${esc(error.message)}${error.status === 403 ? '<br><button data-page="membership" class="small-button">查看会员权益</button>' : ""}</div>`;
  }
}
function traceBlocks(traces) {
  return traces.filter((t) => t.trace_kind !== "system_review_result").map((t) => `<details class="trace-block" ${/output|final|result/.test(t.trace_kind) ? "open" : ""}><summary>${esc(t.trace_kind || analysisState(t.state))} · ${stamp(t.created_at)}${t.last_error ? " · " + esc(t.last_error) : ""}</summary><pre>${jsonTrace(t.payload ?? t.last_error ?? "暂无轨迹内容")}</pre>${t.tool_response_url ? `<button type="button" class="small-button" data-tool-response="${esc(t.tool_response_url)}">读取完整工具返回</button><div class="tool-response-content"></div>` : ""}</details>`).join("");
}
function traceMarkup(traces = [], systemSummary = null) {
  const latest = systemSummary || traces.filter((t) => t.trace_kind === "system_review_result" && typeof t.payload?.content === "string").at(-1);
  const receipt = latest ? `<section class="detail-section system-review-result"><h3>系统确认的审核结果</h3><pre class="raw-text">${esc(latest.payload.content)}</pre><p class="form-note">此处只汇总系统已接受记录及工具拒绝原因；模型原始输出保留在下方。</p></section>` : "";
  return receipt + `<section class="detail-section"><h3>Hermes 模型输入、原始输出与工具轨迹</h3><p class="form-note">原文按审核顺序分页保留；图片请查看授权原图，工具分片通过对应记录读取完整返回。</p><div id="message-traces">${traces.length ? traceBlocks(traces) : '<p class="muted">尚无审核轨迹。消息仍保留供排查。</p>'}</div><button type="button" class="small-button" id="message-traces-more" hidden>继续读取模型原文与轨迹</button><p id="message-traces-status" class="form-note" role="status"></p></section>`;
}
function bindMessageEvidence(data, id, context, sequence) {
  const content = $("#dialog-content");
  const root = (context === "admin" ? "/admin/telegram/messages/" : "/public/messages/") + encodeURIComponent(id);
  let traceCursor = data.analysis_next_cursor || null;
  let signalCursor = data.signals_next_cursor || null;
  let signalCount = (data.signals || []).length;
  const traceMore = $("#message-traces-more");
  const signalMore = $("#message-signals-more");
  traceMore.hidden = !traceCursor;
  signalMore.hidden = !signalCursor;
  $("#message-traces-status").textContent = traceCursor ? "还有审核记录，请继续读取；尚未加载的内容没有被省略或删除。" : "当前已保存的审核轨迹已全部加载。";
  traceMore.onclick = async () => {
    traceMore.disabled = true;
    try {
      const page = await api(root + "/traces?cursor=" + encodeURIComponent(traceCursor), { context });
      if (sequence !== state.dialogSequence) return;
      $("#message-traces").insertAdjacentHTML("beforeend", traceBlocks(page.items || []));
      traceCursor = page.next_cursor || null;
      traceMore.hidden = !traceCursor;
      $("#message-traces-status").textContent = traceCursor ? "还有审核记录，可继续读取。" : "当前已保存的审核轨迹已全部加载。";
    } catch (error) {
      if (sequence === state.dialogSequence) $("#message-traces-status").textContent = error.message + "；原文保留，可重试或重新打开。";
    } finally { traceMore.disabled = false; }
  };
  signalMore.onclick = async () => {
    signalMore.disabled = true;
    try {
      const page = await api(root + "/signals?cursor=" + encodeURIComponent(signalCursor), { context });
      if (sequence !== state.dialogSequence) return;
      $("#message-signals").insertAdjacentHTML("beforeend", (page.items || []).map((s) => signalCard(s, context)).join(""));
      signalCount += (page.items || []).length;
      signalCursor = page.next_cursor || null;
      signalMore.hidden = !signalCursor;
      $("#message-signals-status").textContent = `已加载 ${signalCount} 条系统盘口绑定。${signalCursor ? "还有推荐，请继续读取。" : "当前推荐已全部加载。"}`;
    } catch (error) {
      if (sequence === state.dialogSequence) $("#message-signals-status").textContent = error.message + "；已加载推荐保留。";
    } finally { signalMore.disabled = false; }
  };
  content.onclick = async (event) => {
    if (sequence !== state.dialogSequence) return;
    const button = event.target.closest("[data-tool-response]");
    if (!button || button.disabled) return;
    const url = button.dataset.toolResponse;
    if (!url?.startsWith("/api/v1" + root + "/tools/")) return;
    button.disabled = true;
    const target = button.nextElementSibling;
    try {
      const response = await api(url.slice(7), { context });
      if (sequence !== state.dialogSequence) return;
      target.innerHTML = `<p class="form-note">完整返回已校验 · ${esc(response.snapshot_sha256)}</p><pre>${jsonTrace(response.result)}</pre>`;
      button.textContent = "完整工具返回已加载";
    } catch (error) {
      if (sequence === state.dialogSequence) { target.textContent = error.message; button.disabled = false; }
    }
  };
}
async function openMessage(id, context = "user") {
  state.messageContext = context;
  setDetail("message", id);
  dialog("消息原文与审核", '<div class="empty-state">正在读取完整消息…</div>');
  const sequence = state.dialogSequence;
  state.mediaUrls.forEach(URL.revokeObjectURL);
  state.mediaUrls = [];
  try {
    const data = await api(
      (context === "admin"
        ? "/admin/telegram/messages/"
        : "/public/messages/") + encodeURIComponent(id),
      { context },
    );
    if (sequence !== state.dialogSequence) return;
    const m = data.message;
    $("#dialog-title").textContent =
      (m.display_nickname || "专家") + " · 完整消息";
    $("#dialog-content").innerHTML =
      `<section class="detail-section"><div class="message-meta"><strong>${esc(m.display_nickname)}</strong><span>${stamp(m.posted_at)}</span><span>消息版本 ${esc(m.revision ?? 1)}</span><span>${context === "admin" ? "管理员读取" : "用户权益读取"}</span>${m.deleted_at ? '<span class="badge">原消息已删除</span>' : ""}</div><h3>Telegram 消息原文</h3><pre class="raw-text">${esc(m.raw_text || "（仅图片消息）")}</pre>${m.reply_context ? `<details class="trace-block"><summary>回复上下文</summary><pre>${jsonTrace(m.reply_context)}</pre></details>` : ""}<div class="media-gallery" id="message-media"></div>${m.media_state === "pending" ? '<p class="notice">图片仍待上传；审核可能尚未完成。</p>' : ""}</section><section class="detail-section"><h3>对应系统比赛、盘口与推荐买入</h3><p class="form-note" id="message-signals-status">已加载 ${data.signals.length} 条系统盘口绑定。${data.signals_next_cursor ? "还有推荐，请继续读取。" : "当前推荐已全部加载。"}其他未解决意图及原因保留在下方审核结果中，不能作为有效推荐。</p><div id="message-signals">${data.signals.map((s) => signalCard(s, context)).join("") || '<p class="muted">尚未形成已接受的盘口绑定；这条消息不会计入收益或自动投注。</p>'}</div><button type="button" class="small-button" id="message-signals-more" hidden>继续读取推荐买入</button></section>${traceMarkup(data.analysis, data.system_summary)}`;
    bindMessageEvidence(data, id, context, sequence);
    for (const [index, media] of (m.media_objects || []).entries()) {
      const gallery = $("#message-media");
      const url =
        media.url ||
        `/api/v1/${context === "admin" ? "admin/telegram" : "public"}/messages/${encodeURIComponent(id)}/media/${index}`;
      const allowedRoot = `/api/v1/${context === "admin" ? "admin/telegram" : "public"}/messages/${encodeURIComponent(id)}/media/`;
      if (!url.startsWith(allowedRoot) && !(context === "admin" && url.startsWith(`/api/v1/public/messages/${encodeURIComponent(id)}/media/`))) continue;
      const status = document.createElement("span");
      status.className = "muted";
      status.textContent = "图片加载中…";
      gallery.append(status);
      fetch(url, {
        credentials: "same-origin",
        headers: { "X-Session-Context": context },
        signal: AbortSignal.timeout(15000),
      })
        .then(async (response) => {
          if (!response.ok) throw new Error("图片暂不可读");
          if (
            !/^image\/(png|jpeg|webp|gif)$/.test(
              response.headers.get("Content-Type")?.split(";")[0] || "",
            )
          )
            throw new Error("图片类型不受支持");
          const blob = await response.blob();
          if (blob.size > 12 * 1024 * 1024) throw new Error("图片过大");
          if (sequence !== state.dialogSequence || !$("#content-dialog").open)
            return;
          const objectUrl = URL.createObjectURL(blob);
          state.mediaUrls.push(objectUrl);
          const img = document.createElement("img");
          img.src = objectUrl;
          img.alt = "Telegram 原消息授权图片 " + (index + 1);
          status.replaceWith(img);
        })
        .catch(() => (status.textContent = "图片暂不可读，请稍后重新打开消息"));
    }
  } catch (error) {
    if (sequence !== state.dialogSequence) return;
    if (error.status === 403) state.pendingDetail = { id, context };
    $("#dialog-content").innerHTML =
      `<div class="empty-state">${esc(error.message)}${error.status === 403 ? '<br><button class="small-button" data-login="login">登录后继续阅读</button> <button class="small-button" data-page="membership">查看会员权益</button>' : ""}</div>`;
  }
}
async function loadPlans() {
  try {
    if (state.page === "membership") await loadIdentities();
    const data = await api("/public/membership/plans");
    state.plans = data.items;
    $("#plans-list").innerHTML =
      data.items
        .map(
          (p) =>
            `<article class="plan-card"><h3>${esc(p.name)}</h3>${(state.user?.membership?.active_plans || [{ code: state.user?.membership?.code }]).some((plan) => plan.code === p.code) ? '<span class="badge">当前有效方案</span>' : ""}<div class="plan-price">${decimal(p.price, Number(p.price) % 1 ? 2 : 0)}<small> ${esc(p.currency)}</small></div><p>${esc(p.duration_days)} 天</p><ul>${
              Object.entries(p.entitlements)
                .filter(([, v]) => v === true)
                .map(([k]) => `<li>${esc(entitlementName(k))}</li>`)
                .join("") || "<li>无额外权益</li>"
            }</ul><button disabled>在线支付未配置</button></article>`,
        )
        .join("") || '<div class="empty-state">暂无可用会员方案</div>';
    $("#membership-current").textContent = state.user?.membership
      ? `${state.user.membership.name} · ${state.user.membership.ends_at ? "有效至 " + stamp(state.user.membership.ends_at) : "长期有效"}`
      : "登录后查看我的权益";
  } catch (error) {
    $("#plans-list").innerHTML =
      `<div class="empty-state">${esc(error.message)}</div>`;
  }
}
const entitlementName = (k) =>
  ({
    expert_full_history: "完整专家记录",
    expert_live_signals: "滚球专家推荐",
    automatic_betting: "本人账户自动投注",
  })[k] || k;
async function loadIdentities() {
  const results = await Promise.allSettled([
    api("/auth/me"),
    api("/admin/me", { context: "admin" }),
  ]);
  for (const [index, r] of results.entries()) {
    const key = index ? "admin" : "user";
    if (r.status === "fulfilled") state[key] = r.value;
    else if (r.reason.status === 401) state[key] = null;
    else if (state.page === (index ? "admin" : "account"))
      toast(r.reason.message);
  }
  $("#account-entry").textContent =
    state.user?.user.display_name || "登录 / 注册";
  $("#user-logout").hidden = !state.user;
  $("#admin-logout").hidden = !state.admin;
}
async function loadAccount() {
  const sequence = ++state.accountSequence;
  $$("input,button", $("#leyu-form")).forEach(
    (input) => (input.disabled = true),
  );
  $("#leyu-form").setAttribute("aria-busy", "true");
  $("#betting-form").setAttribute("aria-busy", "true");
  configureBetControls();
  try {
    await loadIdentities();
    if (sequence !== state.accountSequence) return;
    const user = state.user;
    $("#account-content").hidden = !user;
    $("#user-logout").hidden = !user;
    if (!user) {
      $("#identity-panel").innerHTML =
        '<div class="identity-card"><h2>欢迎来到球讯</h2><p>登录后管理会员权益、绑定本人乐鱼账户及查看订单。</p><button class="primary-button" data-login="login">用户登录 / 注册</button></div>';
      return;
    }
    const m = user.membership;
    $("#identity-panel").innerHTML =
      `<div class="identity-card"><h2>${esc(user.user.display_name)}</h2><p>${esc(user.user.email)} · 普通用户</p><p>${m ? esc(m.name) + " · " + (m.ends_at ? "有效至 " + stamp(m.ends_at) : "长期有效") : "暂无有效会员权益"}</p><p>${
        Object.entries(m?.entitlements || {})
          .filter(([, v]) => v)
          .map(([k]) => esc(entitlementName(k)))
          .join(" / ") || "可浏览公开赛事与受限专家摘要"
      }</p></div>`;
    if (user.closure_requested_at)
      $("#identity-panel").insertAdjacentHTML(
        "beforeend",
        `<p class="notice">注销申请提交于 ${stamp(user.closure_requested_at)}，当前仅保留对账与停止投注等操作。核对完成后请在注销分区再次确认；取消注销不会恢复自动投注。</p>`,
      );
    $("#profile-form").elements.display_name.value = user.user.display_name;
    const data = await api("/accounts/leyu");
    if (sequence !== state.accountSequence) return;
    $("#account-health").textContent = data.account
      ? { healthy: "已通过校验", unverified: "未校验", expired: "授权已失效" }[
          data.account.health_state
        ] || data.account.health_state
      : "未绑定";
    $("#leyu-form").elements.account_label.value =
      data.account?.account_label || "";
    state.accountBound = !!data.account;
    $("#account-verification").innerHTML = data.account
      ? `<p>${esc(data.account.account_label)} · 最近校验 ${stamp(data.account.last_verified_at)}</p><p>可用余额 ${data.account.available_balance === null || data.account.available_balance === undefined ? "尚未核验" : decimal(data.account.available_balance)} · 余额时间 ${stamp(data.account.balance_observed_at)}</p>${(data.tasks || []).map((task) => `<p class="form-note">${esc({ verify: "账户校验", refresh: "账户刷新" }[task.kind] || task.kind)} · ${esc({ queued: "已排队", running: "校验中", succeeded: "校验成功", failed: "校验失败", unknown: "结果未知", rejected: "授权被拒绝" }[task.state] || task.state)}${task.last_error ? " · " + esc(task.last_error) : ""}</p>`).join("")}`
      : '<p class="muted">尚未绑定本人乐鱼账户。</p>';
    $("#verify-account").disabled = !data.account;
    const b = data.betting || {};
    $("#betting-status").textContent =
      b.auto_enabled && !b.emergency_stopped
        ? "自动投注已启用；执行前仍会核验信号与敞口。"
        : "自动投注已关闭。紧急停止始终可用。";
    if (data.execution_capability?.live_enabled === false)
      $("#betting-status").textContent += " 当前实盘执行未开放。";
    if (data.execution_capability?.prematch === "unsupported")
      $("#betting-status").textContent +=
        " 赛前自动投注暂未支持，赛前专家推荐仍可浏览。";
    for (const name of [
      "stake_per_order",
      "max_per_order",
      "daily_limit",
      "open_exposure_limit",
    ])
      $("#betting-form").elements[name].value = b[name] ?? "";
    const timezone = $("#betting-form").elements.daily_timezone;
    const timezoneValue = b.daily_timezone || "Asia/Shanghai";
    for (const option of [...timezone.options])
      if (option.dataset.accountTimezone) option.remove();
    if (
      ![...timezone.options].some((option) => option.value === timezoneValue)
    ) {
      const option = new Option(timezoneValue, timezoneValue);
      option.dataset.accountTimezone = "true";
      timezone.add(option);
    }
    timezone.value = timezoneValue;
    $("#betting-form").elements.enabled.checked =
      !!b.auto_enabled && !b.emergency_stopped;
    $$("#bet-experts input").forEach(
      (x) => (x.checked = (b.allowed_experts || []).includes(x.value)),
    );
  } catch (error) {
    if (sequence !== state.accountSequence) return;
    $("#account-health").textContent = "暂不可读";
    $("#betting-status").textContent =
      error.message + "；账户状态未被替换为未绑定。";
  } finally {
    if (sequence === state.accountSequence) {
      $$("input,button", $("#leyu-form")).forEach(
        (input) => (input.disabled = false),
      );
      $("#leyu-form").setAttribute("aria-busy", "false");
      $("#betting-form").setAttribute("aria-busy", "false");
      configureBetControls();
    }
  }
  if (sequence !== state.accountSequence) return;
  loadOrders();
}
function configureBetControls() {
  const form = $("#betting-form"),
    enabled = form.elements.enabled.checked,
    loading = form.getAttribute("aria-busy") === "true";
  for (const input of $$("input,select", form)) {
    input.disabled = loading || (input.name !== "enabled" && !enabled);
    if (input.type === "number") input.required = enabled;
  }
  $("button", form).disabled = loading;
  form.elements.confirmed.required = enabled;
}
async function loadOrders(append = false) {
  try {
    const result = await api(
      "/accounts/orders?" +
        params({ limit: 30, before: append ? state.orderCursor : null }),
    );
    state.orders = append ? [...state.orders, ...result.items] : result.items;
    state.orderCursor = result.next_cursor;
    $("#orders-more").hidden = !state.orderCursor;
    $("#orders-list").innerHTML =
      state.orders
        .map(
          (order) =>
            `<div class="admin-record"><strong>${esc({ pending: "待执行", submitting: "提交中", accepted: "已接单", rejected: "已拒绝", unknown: "接单结果未知，等待核对", settled: "已结算", cancelled: "已取消" }[order.state] || order.state)}</strong> · ${esc(order.home_team || order.fixture_id || "")}<br>${esc(order.display_nickname || "")} · ${esc(order.market_name || "")} ${esc(lineLabel(order))} ${esc(selection(order.selection))}<br>预留本金 ${decimal(order.stake)} · 预留赔率 ${decimal(order.price)}<br>官方成交本金 ${order.provider_stake == null ? "未知" : decimal(order.provider_stake)} · 成交赔率 ${order.provider_price == null ? "未知" : decimal(order.provider_price)} · 币种 ${esc(order.provider_currency || "未知")}<br>实际净收益 ${order.settled_net == null ? (order.state === "settled" ? "官方回执未提供" : "待结算") : signed(order.settled_net)} · 手续费 ${order.provider_fees == null ? "未知" : decimal(order.provider_fees)}<br><span class="muted">官方下单 ${order.provider_placed_at ? stamp(order.provider_placed_at) : "未知"} · 官方结算 ${order.settled_at ? stamp(order.settled_at) : "未知"}<br>${stamp(order.created_at)} · 订单 ${esc(order.provider_order_no || "未取得上游订单号")}<br>平台记录 ${esc(order.id)}</span></div>`,
        )
        .join("") || '<div class="empty-state">暂无真实订单</div>';
  } catch (error) {
    $("#orders-list").innerHTML =
      `<div class="notice">${error.status === 404 ? "订单服务尚未开放" : esc(error.message)}，保留已读取记录，不补入模拟订单。</div>`;
  }
}
function auth(tab = "login") {
  const isAdmin = tab === "admin" || tab === "admin-recovery";
  state.authTab = tab;
  $$("[data-auth-tab]").forEach((b) => {
    b.classList.toggle(
      "active",
      b.dataset.authTab === (isAdmin ? "admin" : tab),
    );
    b.setAttribute("aria-pressed", String(b.classList.contains("active")));
  });
  $("#auth-title").textContent = isAdmin
    ? "管理员独立登录"
    : tab === "register"
      ? "注册用户账号"
      : "用户登录";
  $("#display-name-field").hidden = tab !== "register";
  $("#display-name-field input").required = tab === "register";
  $("#totp-field").hidden = tab !== "admin";
  $("#totp-field input").required = false;
  $("#admin-recovery-field").hidden = tab !== "admin-recovery";
  $("#admin-recovery-field input").required = tab === "admin-recovery";
  $("#admin-recovery-toggle").hidden = !isAdmin;
  $("#auth-identity-label span").textContent = tab === "admin" || tab === "admin-recovery" ? "管理员账号" : "邮箱";
  $("#auth-form").elements.email.type = tab === "admin" || tab === "admin-recovery" ? "text" : "email";
  $("#auth-form").elements.password.minLength = tab === "register" ? 12 : tab === "admin" ? 6 : 1;
  $("#auth-form button").textContent = tab === "register" ? "注册" : "登录";
  $("#auth-message").textContent = "";
  showDialog($("#auth-dialog"), $("#auth-form").elements.email);
}
function events() {
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button,a");
    if (!button) return;
    if (button.dataset.page) {
      event.preventDefault();
      const pageChanged = state.page !== button.dataset.page;
      $$("dialog[open]").forEach((d) => d.close());
      route(button.dataset.page, { phase: button.dataset.phase });
      if (pageChanged) {
        const heading = $(".view.active h1");
        if (heading) {
          heading.tabIndex = -1;
          heading.focus();
        }
      }
      return;
    }
    if (button.dataset.closeDialog !== undefined) {
      button.closest("dialog").close();
      return;
    }
    if (button.dataset.login) {
      auth(button.dataset.login);
      return;
    }
    if (button.dataset.authTab) {
      auth(button.dataset.authTab);
      return;
    }
    if (button.dataset.date) {
      setDate(button.dataset.date);
      return;
    }
    if (button.dataset.phase) {
      state.phase = button.dataset.phase;
      state.matches = [];
      route("scores", { replace: true });
      return;
    }
    if (button.dataset.sport) {
      state.sport = button.dataset.sport;
      state.matches = [];
      route("scores", { replace: true });
      return;
    }
    if (button.dataset.favorite) {
      const id = button.dataset.favorite;
      if (favorites.has(id)) favorites.delete(id);
      else favorites.add(id);
      try {
        localStorage.setItem(
          "qiuxun-favorites",
          JSON.stringify([...favorites].slice(-500)),
        );
      } catch {
        toast("浏览器未允许保存关注记录");
      }
      renderMatches();
      return;
    }
    if (button.dataset.followExpert) {
      const id = button.dataset.followExpert;
      if (followedExperts.has(id)) followedExperts.delete(id);
      else followedExperts.add(id);
      button.setAttribute("aria-pressed", String(followedExperts.has(id)));
      button.textContent = followedExperts.has(id) ? "★" : "☆";
      try {
        localStorage.setItem(
          "qiuxun-followed-experts",
          JSON.stringify([...followedExperts].slice(-500)),
        );
      } catch {
        toast("浏览器未允许保存专家关注记录，本次会话仍可使用。");
      }
      applyExpertFollowing();
      return;
    }
    if (button.dataset.match) {
      openMatch(button.dataset.match, { quoteId: button.dataset.quote });
      return;
    }
    if (button.dataset.expert) {
      openExpert(button.dataset.expert);
      return;
    }
    if (button.dataset.message) {
      openMessage(
        button.dataset.message,
        button.dataset.context || state.messageContext,
      );
      return;
    }
    if (button.dataset.signal) {
      openSignal(button.dataset.signal);
      return;
    }
    if (button.dataset.expertMessages) {
      $("#content-dialog").close();
      $("#message-expert").value = button.dataset.expertMessages;
      route("messages");
      return;
    }
    if (button.dataset.expertTab) {
      $$("[data-expert-tab]").forEach((b) => {
        b.classList.toggle("active", b === button);
        b.setAttribute("aria-pressed", String(b === button));
      });
      $("#expert-ranking").hidden = button.dataset.expertTab !== "ranking";
      $("#expert-signals").hidden = button.dataset.expertTab !== "signals";
    }
    if (button.hasAttribute("data-retry")) {
      if (state.page === "scores") loadMatches();
      else if (state.page === "messages") loadMessages();
      else loadExperts();
    }
  });
  $('#expert-stats-filter').onsubmit = (event) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    state.expertFilters = {days: Number(form.get('days')), phase: form.get('phase'), market_kind: form.get('market_kind'), source_id: form.get('source_id')};
    state.experts = [];
    renderExperts();
    loadExperts();
  };
  $("#account-entry").onclick = () => (state.user ? route("account") : auth());
  $("#date-prev").onclick = () => setDate(shiftDay(state.date, -1));
  $("#date-next").onclick = () => setDate(shiftDay(state.date, 1));
  $("#date-picker").onchange = (e) => setDate(e.target.value);
  $("#league-filter").onclick = chooseLeagues;
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (
      button.hasAttribute("data-apply-leagues") ||
      button.hasAttribute("data-clear-leagues")
    ) {
      const values = button.hasAttribute("data-clear-leagues")
        ? []
        : $$("[data-league-id]:checked").map((input) => input.dataset.leagueId);
      if (values.length > 100) {
        toast("最多选择100个联赛");
        return;
      }
      state.leagueIds = values;
      leagueLabel();
      $("#content-dialog").close();
      state.matches = [];
      route("scores", { replace: true });
    }
  });
  let debounce;
  $("#match-search").oninput = (e) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      state.q = e.target.value.trim();
      state.matches = [];
      loadMatches();
    }, 350);
  };
  $("#favorites-filter").onclick = (e) => {
    state.favoritesOnly = !state.favoritesOnly;
    e.currentTarget.setAttribute("aria-pressed", String(state.favoritesOnly));
    e.currentTarget.textContent = state.favoritesOnly ? "★ 关注" : "☆ 关注";
    renderMatches();
  };
  $("#refresh-matches").onclick = () => loadMatches();
  $("#load-more").onclick = () => loadMatches(true);
  $("#refresh-experts").onclick = () => loadExperts();
  $("#followed-experts").onclick = (event) => {
    state.followedExpertsOnly = !state.followedExpertsOnly;
    event.currentTarget.setAttribute(
      "aria-pressed",
      String(state.followedExpertsOnly),
    );
    applyExpertFollowing();
  };
  $("#signals-more").onclick = () => loadSignals(true);
  $("#message-expert").onchange = () => loadMessages();
  $("#refresh-messages").onclick = () => loadMessages();
  $("#messages-more").onclick = () => loadMessages(true);
  $("#refresh-orders").onclick = () => loadOrders();
  $("#orders-more").onclick = () => loadOrders(true);
  $("#refresh-account").onclick = () => loadAccount();
  $("#verify-account").onclick = async () => {
    try {
      await api("/accounts/leyu/verify", { method: "POST" });
      toast("已排队无资金账户校验");
      loadAccount();
    } catch (error) {
      toast(error.message);
    }
  };
  $("#stats-help").onclick = () =>
    dialog(
      "专家统计口径",
      '<section class="detail-section"><p class="raw-text">准确率 = (全赢 + 半赢 × 0.5) ÷ (全赢 + 全输 + 半赢 × 0.5 + 半输 × 0.5)。走盘、作废和待结算不计准确率分母。\n\n1单位净收益使用推荐时系统赔率：全赢为赔率减1，全输为-1，半赢为一半净赢，半输为-0.5，走盘与作废为0。\n\n平均每条净收益 = 已结算净收益 ÷ 已结算推荐数。有效本金 ROI = 已结算净收益 ÷ 有效风险本金。\n\n上述均为专家推荐台账指标，实际账户收益以真实成交与结算回执为准。无已结算样本时显示样本不足。</p></section>',
    );
  $("#auth-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    submit(form, async () => {
      const fields = Object.fromEntries(new FormData(form));
      const body = { email: fields.email, password: fields.password };
      if (state.authTab === "register") body.display_name = fields.display_name;
      if (state.authTab === "admin" && fields.totp_code) body.totp_code = fields.totp_code;
      if (state.authTab === "admin-recovery")
        body.recovery_code = fields.recovery_code;
      try {
        await api(
          state.authTab === "admin-recovery"
            ? "/admin/recovery-login"
            : state.authTab === "admin"
              ? "/admin/login"
              : state.authTab === "register"
                ? "/auth/register"
                : "/auth/login",
          {
            method: "POST",
            body,
            context: state.authTab.startsWith("admin") ? "admin" : "user",
          },
        );
        form.reset();
        $("#auth-dialog").close();
        await loadIdentities();
        toast("登录成功");
        if (state.authTab.startsWith("admin")) route("admin");
        else if (state.pendingDetail) {
          const pending = state.pendingDetail;
          state.pendingDetail = null;
          openMessage(pending.id, pending.context);
        } else route("account");
        loadExperts();
      } catch (error) {
        $("#auth-message").textContent = error.message;
        form.elements.password.value = "";
        form.elements.totp_code.value = "";
        form.elements.recovery_code.value = "";
      }
    });
  };
  $("#user-logout").onclick = async () => {
    try {
      await api("/auth/logout", { method: "POST" });
      state.user = null;
      loadAccount();
      toast("已退出用户会话");
    } catch (error) {
      toast(error.message);
    }
  };
  $("#admin-logout").onclick = async () => {
    try {
      await api("/admin/logout", { method: "POST", context: "admin" });
      state.admin = null;
      loadAdmin();
      toast("已退出管理员会话");
    } catch (error) {
      toast(error.message);
    }
  };
  $("#profile-form").onsubmit = (e) => {
    e.preventDefault();
    submit(e.currentTarget, async () => {
      await api("/auth/profile", {
        method: "PATCH",
        body: Object.fromEntries(new FormData(e.currentTarget)),
      });
      toast("资料已保存");
      await loadAccount();
    });
  };
  $("#password-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    submit(form, async () => {
      await api("/auth/password", {
        method: "PUT",
        body: Object.fromEntries(new FormData(form)),
      });
      form.reset();
      toast("密码已更新");
      await loadAccount();
    });
  };
  $("#leyu-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    submit(form, async () => {
      const data = await api("/accounts/leyu", {
        method: "PUT",
        body: Object.fromEntries(new FormData(form)),
      });
      form.elements.password.value = "";
      form.elements.username.value = "";
      toast(data.message || "账户已加密保存，等待校验");
      loadAccount();
    });
  };
  $("#unbind-account").onclick = async () => {
    if (!(await confirmAction("解绑本人乐鱼账户并停止自动投注？"))) return;
    try {
      await api("/accounts/leyu", { method: "DELETE" });
      toast("已解绑并关闭自动投注");
      loadAccount();
    } catch (error) {
      toast(error.message);
    }
  };
  $("#betting-form").elements.enabled.onchange = configureBetControls;
  $("#betting-form").onsubmit = (e) => {
    e.preventDefault();
    const form = e.currentTarget;
    submit(form, async () => {
      const fd = new FormData(form);
      const enabled = fd.has("enabled");
      if (enabled && !fd.getAll("expert").length)
        throw new Error("至少选择一位允许跟随的专家");
      if (enabled && !fd.has("confirmed"))
        throw new Error("请先确认本人账户资金与投注风险");
      const body = {
        enabled,
        ...(enabled ? { daily_timezone: fd.get("daily_timezone") } : {}),
        confirmation_phrase: enabled ? "I_UNDERSTAND" : "",
        allowed_experts: fd.getAll("expert"),
      };
      for (const key of [
        "stake_per_order",
        "max_per_order",
        "daily_limit",
        "open_exposure_limit",
      ])
        body[key] = fd.get(key) || "0";
      await api("/accounts/leyu/auto-betting", { method: "PUT", body });
      toast(enabled ? "投注设置已保存" : "自动投注已关闭");
      loadAccount();
    });
  };
  $("#emergency-stop").onclick = async () => {
    try {
      await api("/accounts/leyu/emergency-stop", { method: "POST" });
      toast("已紧急停止全部自动投注");
      loadAccount();
    } catch (error) {
      toast(error.message);
    }
  };
  window.addEventListener("popstate", () => readRoute());
  window.addEventListener("offline", () => {
    if (state.page === "scores") {
      $("#matches-notice").hidden = false;
      $("#matches-notice").textContent = "当前网络已断开，保留上次赛事。";
    }
  });
  window.addEventListener("online", () => {
    if (state.page === "scores") loadMatches();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) clearTimeout(state.retryTimer);
    else if (state.page === "scores") loadMatches();
  });
}
function setDetail(type, id) {
  const query = params({
    ...(state.page === "scores"
      ? { date: state.date, phase: state.phase }
      : {}),
    [type]: id,
    ...(type === "message" && state.messageContext === "admin"
      ? { asadmin: "1" }
      : {}),
  });
  const hash = "#" + state.page + "?" + query;
  if (location.hash !== hash) history.pushState({ detail: true }, "", hash);
}
function readRoute() {
  const [page, query] = location.hash.slice(1).split("?");
  const p = new URLSearchParams(query || "");
  if (validDate(p.get("date"))) state.date = p.get("date");
  if (
    ["all", "live", "scheduled", "finished", "cancelled"].includes(
      p.get("phase"),
    )
  )
    state.phase = p.get("phase");
  state.leagueIds = p
    .getAll("league_ids")
    .filter((value) => value && value.length <= 100)
    .slice(0, 100);
  const type = ["match", "expert", "message"].find((key) => p.has(key));
  route(page || "scores", { replace: true, preserveHash: !!type });
  if (type === "match") openMatch(p.get(type));
  else if (type === "expert") openExpert(p.get(type));
  else if (type === "message")
    openMessage(p.get(type), p.get("asadmin") === "1" ? "admin" : "user");
  else if ($("#content-dialog").open) $("#content-dialog").close();
}
$("#content-dialog").addEventListener("close", () => {
  clearTimeout(state.detailTimer);
  state.dialogSequence++;
  state.mediaUrls.forEach(URL.revokeObjectURL);
  state.mediaUrls = [];
  const query = new URLSearchParams(location.hash.split("?")[1] || "");
  if (["match", "expert", "message"].some((key) => query.has(key))) {
    if (history.state?.detail) history.back();
    else
      history.replaceState(
        {},
        "",
        "#" +
          state.page +
          (state.page === "scores"
            ? "?" +
              params({
                date: state.date,
                phase: state.phase,
                league_ids: state.leagueIds,
              })
            : ""),
      );
  }
});
initAdmin({ state, loadIdentities, auth, route, openMessage, loadPlans });
initIdentity({ state, dialog, auth, loadAccount });
initMfa({ state, dialog });
events();
renderDates();
loadIdentities().then(() => {
  if (state.page === "admin") loadAdmin();
});
loadExperts();
readRoute();
$("#header-clock").textContent = dayKey() + " 北京时间";
setInterval(() => {
  if (!document.hidden && state.page === "scores") loadMatches();
}, 30000);
