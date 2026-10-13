"use strict";
const $ = id => document.getElementById(id);
const S = {view: 'live', type: 'real', league: '', search: '', matches: [], selected: null,
  request: 0, detailRequest: 0, historyRequest: 0, recommendationsRequest: 0, historyOffset: 0, historyNext: null, busy: false, loaded: false, timer: null, chartKey: '', marketScope: 'all', expandedMatches: new Set(), settingsVersion: 0, settingsBusy: false, accountBusy: false, accountAt: 0, betStatusUnsupported: false};
const API = '/api/v1';
const POLL_MS = 5000;
const algorithmLabel = a => ({economic_ensemble:'经济学汇总', economics_control:'纯经济学', economics_llm:'经济学 + LLM', poisson_market: '盘口 Poisson', poisson_time_decay: '衰减 Poisson',
  devig_consensus: '去水共识', economics_risk_adjusted: '经济学风控',
  microstructure_adjusted: '盘口微观结构', legacy: '旧算法'}[a] || a || '—');
const pct = n => Number.isFinite(Number(n)) && n !== null ? (Number(n) * 100).toFixed(1) + '%' : '—';
const fixed = (n, digits = 2) => n !== null && n !== undefined && Number.isFinite(Number(n)) ? Number(n).toFixed(digits) : '—';
function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'text') node.textContent = value;
    else if (key === 'class') node.className = value;
    else if (key === 'onclick') node.addEventListener('click', value);
    else node.setAttribute(key, value);
  }
  for (const child of children) node.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
  return node;
}
function notice(id, message = '', error = false) {
  $(id).hidden = !message;
  $(id).textContent = message;
  $(id).classList.toggle('error', error);
}
async function get(path) {
  try {
    const response = await fetch(API + path, {signal: AbortSignal.timeout(8000), cache: 'no-store'});
    if (!response.ok) {
      const error = new Error('读取失败（' + response.status + '）');
      error.status = response.status;
      throw error;
    }
    return await response.json();
  } catch (error) {
    if (error.name === 'TimeoutError') {
      const timeout = new Error('读取超时，服务暂未响应');
      timeout.name = 'TimeoutError';
      throw timeout;
    }
    throw error;
  }
}
const temporaryReadError = error => error.name === 'TimeoutError' || error.name === 'TypeError' || [502,503,504].includes(error.status);
function accountPnl(data) {
  const pnl = data?.today_pnl;
  const available = pnl?.available === true && typeof pnl.amount === 'number' && Number.isFinite(pnl.amount);
  const amount = available ? pnl.amount : null;
  const value = available ? (amount > 0 ? '+' : '') + fixed(amount) : '—';
  const basis = '北京时间 · 按结算时间统计 · 不含未结算订单';
  const note = available ? `${pnl.date} · 今日已结 ${pnl.settled_count} 笔` : (pnl?.reason || '今日结算盈亏暂不可用');
  return {value, note, basis, className:'account-pnl' + (amount > 0 ? ' positive' : amount < 0 ? ' negative' : '')};
}
function accountPnlNode(data) {
  const pnl = accountPnl(data);
  return el('span', {class:pnl.className, text:'今日盈亏 ' + pnl.value, title:pnl.note + ' · ' + pnl.basis});
}
function renderAccount(data) {
  const node = $('account-summary');
  if (!node) return;
  if (!data || !data.available) {
    node.textContent = '账户数据不可用';
    node.title = data?.error || '未配置乐鱼 App 会话';
    renderAccountDetails(data);
    return;
  }
  const balance = data.sports_balance == null
    ? (data.balance == null ? '—' : fixed(data.balance)) : fixed(data.sports_balance);
  const pendingCount = data.unsettled?.count == null ? '—' : String(data.unsettled.count);
  const pendingAmount = data.unsettled?.amount == null ? '' : ' · ' + fixed(data.unsettled.amount);
  const pending = pendingCount + pendingAmount;
  const settled = data.settled?.count == null ? '—' : String(data.settled.count);
  node.replaceChildren(el('span', {class:'account-balance', text:'体育余额 ' + balance}), accountPnlNode(data),
    ...[['未结', pending], ['已结', settled]].map(([label, value]) =>
      el('span', {class:'account-record-stats', text: label + ' ' + value})));
  node.title = data.error || ('中心钱包 ' + (data.center_balance == null ? '—' : fixed(data.center_balance)) + ' · 乐鱼体育场馆 YBTY' +
    (data.stale || data.refresh_error ? ' · 账户数据待更新' : '') +
    (data.updated_at_ms ? ' · 更新于 ' + new Date(data.updated_at_ms).toLocaleTimeString() : ''));
  renderAccountDetails(data);
}
function renderAccountDetails(data) {
  const root = $('account-details');
  if (!root) return;
  if (!data || !data.available) {
    root.replaceChildren(el('p', {class:'account-error', text:data?.error || '账户数据不可用'}));
    return;
  }
  const summary = el('div', {class:'account-detail-stats'}, [
    ...(data.stale || data.refresh_error ? [el('span', {class:'account-error',text:data.refresh_error || '正在更新，当前展示上次账户数据'})] : []),
    el('span', {text:'体育余额 ' + fixed(data.sports_balance ?? data.balance)}),
    accountPnlNode(data),
    el('span', {text:'未结 ' + String(data.unsettled?.count ?? '—') + ' · ' + fixed(data.unsettled?.amount)}),
    el('span', {text:'已结 ' + String(data.settled?.count ?? '—') + ' · ' + fixed(data.settled?.amount)})
  ]);
  const states = [['未结算', data.unsettled?.items || []], ['已结算', data.settled?.items || []]];
  const sections = states.map(([title, items]) => {
    const rows = items.length ? items.slice(0, 20).map(item => el('div', {class:'account-order'}, [
      el('strong', {text:item.match || item.order_no || '投注'}),
      el('span', {text:(item.market || '盘口') + ' · ' + (item.option || '—') + ' @' + fixed(item.odds)}),
      el('span', {text:'金额 ' + fixed(item.amount) + (title === '已结算' ? ' · 收益 ' + fixed(item.profit) : '')})
    ])) : [el('span', {class:'account-muted', text:'暂无记录'})];
    return el('section', {class:'account-orders'}, [el('h4', {text:title}), ...rows]);
  });
  const pnl = accountPnl(data);
  root.replaceChildren(summary, el('p', {class:'account-muted account-pnl-note', text:pnl.note + ' · ' + pnl.basis}), ...sections);
}
async function loadAccount() {
  const now = Date.now();
  if (S.accountBusy || now - S.accountAt < 30000) return;
  S.accountBusy = true;
  S.accountAt = now;
  try { S.accountData = await get('/account'); renderAccount(S.accountData); }
  catch (error) { renderAccount(S.accountData ? {...S.accountData, stale:true, refresh_error:error.message, error:error.message} : {available: false, error: error.message}); }
  finally { S.accountBusy = false; }
}
function switchView(view) {
  S.view = view;
  for (const key of ['live', 'recommendations', 'history', 'data-model', 'settings']) {
    $(key + '-view').hidden = key !== view;
    $('nav-' + key).classList.toggle('active', key === view);
    if (key === view) $('nav-' + key).setAttribute('aria-current', 'page');
    else $('nav-' + key).removeAttribute('aria-current');
  }
  location.hash = view;
  if (view === 'recommendations') loadRecommendations();
  else if (view === 'history') loadHistory();
  else if (view === 'settings' && !S.settingsVersion) loadSettings();
  else if (view === 'settings') return;
  else if (view === 'data-model') loadDataModel();
  else loadLive();
}
function emptyMessage(title, description, retry) {
  return el('div', {class: 'empty'}, [el('h3', {text: title}), el('p', {text: description}),
    el('button', {class: 'button', text: retry ? '重新读取' : '清除筛选', onclick: () => {
      if (retry) loadLive();
      else {S.search = ''; S.league = ''; $('search').value = ''; renderMatches(); renderLeagues();}
    }})]);
}
function marketRow(market, match) {
  const family = market.market.replace('_1H', '');
  const name = {HAD: '独赢', AH: '让球', OU: '大小球'}[family] || market.market;
  const prefix = market.market.includes('_1H') ? '上半场 ' : '';
  const coverage = market.market.startsWith('RAW_') ? ' · 原始' : market.complete === false ? ' · 缺项' : market.fresh === false ? ' · 过期' : '';
  const row = el('div', {class: 'market-row' + (market.quotes.length === 2 ? ' two' : '') + (market.market.startsWith('RAW_') ? ' raw' : '')},
    [el('span', {class: 'market-name', text: prefix + (market.market.startsWith('RAW_') ? market.name || market.market : name) + (market.line ? '\n' + market.line : '') + coverage})]);
  for (const quote of market.quotes) {
    const trend = Number(quote.trend_pct || 0);
    row.appendChild(el('button', {class: 'odd', title: quote.label + ' · 赔率 ' + quote.odds,
      'aria-label': '查看' + quote.label + '的分析', 'data-focus': match.match_id + '|' + market.market + '|' + market.line + '|' + quote.outcome,
      onclick: () => selectMatch(match.match_id)}, [
      el('span', {class: 'odd-label', text: {home: '主', away: '客', draw: '平', over: '大', under: '小'}[quote.outcome] || quote.outcome}),
      el('span', {class: 'odd-number' + (trend > .01 ? ' up' : trend < -.01 ? ' down' : ''),
        text: fixed(quote.odds) + (trend > .01 ? ' ↑' : trend < -.01 ? ' ↓' : '')})]));
  }
  return row;
}
function probabilityRows(match, compact = false) {
  const rows = el('div', {class: compact ? 'match-probabilities' : ''});
  for (const oc of ['home', 'draw', 'away']) {
    const value = match.probabilities?.[oc];
    if (value === null || value === undefined || !Number.isFinite(Number(value))) continue;
    const bar = el('span'); bar.style.width = Math.max(0, Math.min(100, Number(value) * 100)) + '%';
    rows.appendChild(el('div', {class: 'probability-row'}, [el('span', {text: {home: '主胜', draw: '平', away: '客胜'}[oc]}),
      el('div', {class: 'probability-bar'}, [bar]), el('span', {text: pct(value)})]));
  }
  return rows;
}
function evidenceSummary(item) {
  const decisions = fixed(item.decision_count ?? item.decision_occurrences, 1);
  const hit = fixed(item.settled_hit_count ?? item.hit_count, 1);
  const miss = fixed(item.miss_count, 1);
  const pending = fixed(item.pending_decisions ?? item.pending_count, 1);
  return '决策次数 ' + decisions + ' · 历史命中 ' + hit + ' · 历史未中 ' + miss + ' · 待结算 ' + pending;
}
function renderRecommendations(rows) {
  const picks = rows.flatMap(match => match.suspended ? [] : (match.picks || []).map(pick => ({match,pick})))
    .sort((a,b) => Number(b.pick.composite_confidence ?? b.match.recommendation_confidence ?? b.pick.confidence ?? 0) -
      Number(a.pick.composite_confidence ?? a.match.recommendation_confidence ?? a.pick.confidence ?? 0));
  return picks.slice(0, 100).map(({match,pick}) => el('button', {class:'buy-card', onclick:()=>selectMatch(match.match_id)}, [
    el('span',{class:'buy-match',text:match.home + ' · ' + match.away}),
    el('strong',{text:pick.label + ' @' + fixed(pick.odds) + (pick.odds_live === false ? ' · 等待新赔率' : '')}),
    el('div',{class:'buy-metrics'},[
      el('span',{text:'概率 ' + pct(pick.p_model)}), el('span',{text:'置信度 ' + pct(pick.confidence)}),
      el('span',{text:'净EV ' + pct(pick.effective_ev_live ?? pick.effective_ev)}),
      el('span',{text:evidenceSummary(pick)}),
      el('span',{text:'综合 ' + pct(pick.composite_confidence ?? match.recommendation_confidence)}),
      el('span',{text:'仓位 ' + pct(pick.kelly)})])
  ]));
}

function renderRecommendationCard(item) {
  const match = item.match || item;
  const pick = item.pick || {};
  const betting = item.betting;
  const executionLabels = {accepted:'已接受',pending:'等待场馆确认',queued:'等待检查',rejected:'场馆拒单',unknown:'待核对回执',blocked:'未提交',disabled:'投注已关闭',not_submitted:'尚未提交'};
  return el('button', {class:'buy-card', onclick:()=> {
    if (match.match_id) { S.view = 'live'; switchView('live'); selectMatch(match.match_id); }
  }}, [
    el('span',{class:'buy-match',text:(match.home || '主队') + ' · ' + (match.away || '客队')}),
    el('strong',{text:(pick.label || item.label || '盘口') + ' @' + fixed(pick.odds ?? item.odds) + (pick.odds_live === false ? ' · 等待新赔率' : '')}),
    el('div',{class:'buy-metrics'},[
      el('span',{text:'概率 ' + pct(pick.p_model ?? item.p_fused)}),
      el('span',{text:'置信度 ' + pct(pick.confidence ?? item.confidence)}),
      el('span',{text:evidenceSummary({hit_count: pick.hit_count ?? item.hit_count,
        miss_count: pick.miss_count ?? item.miss_count,
        pending_count: pick.pending_count ?? item.pending_count})}),
      el('span',{text:'综合 ' + pct(pick.composite_confidence ?? item.composite_confidence)}),
      el('span',{text:'赔率状态 ' + (item.market_open ? '开启' : '已关闭')})]),
    ...(betting ? [el('small',{class:'buy-execution',text:'投注：' + (executionLabels[betting.status] || betting.status) +
      (betting.quote_to_submit_ms !== undefined ? ' · 盘口至提交 ' + fixed(betting.quote_to_submit_ms, 0) + 'ms' : '') +
      (betting.quote_to_submit_ms > 2000 ? ' · 超过2秒性能目标，继续执行' : '') +
      (betting.order_no ? ' · 注单 ' + betting.order_no : '') + (betting.reason ? ' · ' + betting.reason : '') +
      (betting.attempt ? ' · 第' + betting.attempt + '次检查' : '') +
      (betting.awaiting_new_quote ? ' · 等待新盘口' : betting.retry_cancelled ? ' · 重试已取消' : betting.retry_scheduled ? ' · ' + betting.retry_after_s + '秒后重新决策' :
        betting.awaiting_new_decision ? ' · 等待更新决策' : betting.retry_exhausted ? ' · 本轮暂停30秒' : '')})] : [])
  ]);
}

function renderRecommendationsPage(data) {
  const open = data.open || [];
  const closed = data.closed || [];
  const summary = data.summary || {};
  $('recommendations-stats').replaceChildren(...[
    ['开启盘口', open.length], ['关闭盘口', closed.length], ['正确', summary.won || 0],
    ['错误', summary.lost || 0], ['待结算', summary.pending || 0]
  ].map(([label, value]) => el('div', {}, [el('span', {text: label}), el('strong', {text: String(value)})])));
  $('open-recommendation-count').textContent = open.length + ' 个';
  $('open-recommendations').replaceChildren(...(open.length ? open.map(renderRecommendationCard) : [el('div',{class:'buy-empty',text:'暂无开启盘口推荐'})]));
  $('closed-recommendation-count').textContent = closed.length + ' 个';
  const statusLabel = {won:'正确', lost:'错误', half_won:'赢半', half_lost:'输半', push:'走水', pending:'待结算', void:'无法结算'};
  const rows = closed.map(item => {
    const date = new Date(item.at);
    const time = Number.isNaN(date.getTime()) ? item.at : date.toLocaleString('zh-CN', {hour12:false});
    const status = statusLabel[item.status] || item.status || '待结算';
    const kind = ['won','half_won'].includes(item.status) ? 'history-win' : ['lost','half_lost'].includes(item.status) ? 'history-loss' : '';
    return el('tr', {}, [
      el('td', {text: time}),
      el('td', {text: (item.home || '主队') + ' vs ' + (item.away || '客队')}, [el('small',{text:item.label || item.market || '盘口'})]),
      el('td', {text: fixed(item.odds)}),
      el('td', {class:kind, text:status}),
      el('td', {class:kind, text:['pending','void'].includes(item.status) ? '—' : (item.pnl > 0 ? '+' : '') + fixed(item.pnl)})
    ]);
  });
  $('closed-recommendations').replaceChildren(...rows);
  $('closed-recommendations-empty').hidden = !!rows.length;
}

async function loadRecommendations(force = false) {
  if (S.recommendationsBusy || (force !== true && Date.now() < (S.recommendationsNextAt || 0))) return;
  S.recommendationsBusy = true;
  const request = ++S.recommendationsRequest;
  $('recommendations-refresh').disabled = true;
  try {
    const data = await get('/recommendations?type=' + encodeURIComponent(S.type));
    if (request !== S.recommendationsRequest || S.view !== 'recommendations') return;
    S.recommendationsNextAt = 0;
    renderRecommendationsPage(data);
    notice('recommendations-notice', data.source_age_s != null ? '实时盘口更新于 ' + fixed(data.source_age_s, 1) + 's 前' : '');
  } catch (error) {
    if (temporaryReadError(error)) S.recommendationsNextAt = Date.now() + 30000;
    if (request === S.recommendationsRequest) notice('recommendations-notice', temporaryReadError(error) ? '后端服务暂不可用，保留上次结果，30秒后自动重试，也可点击刷新。' : error.message + '，可点击刷新重试。', true);
  } finally {
    S.recommendationsBusy = false;
    if (request === S.recommendationsRequest) $('recommendations-refresh').disabled = false;
  }
}
function renderMatches() {
  const query = S.search.toLocaleLowerCase().trim();
  const rows = S.matches.filter(m => (!S.league || m.league === S.league) &&
    (!query || [m.league, m.home, m.away, m.match_id].join(' ').toLocaleLowerCase().includes(query)));
  $('list-count').textContent = rows.length + ' 场';
  $('list-title').textContent = S.league || '实时赛事';
  const focus = document.activeElement?.getAttribute('data-focus');
  const nodes = rows.map(match => {
    const card = el('article', {class: 'match-card' + (S.selected === match.match_id ? ' selected' : ''), 'data-mid': match.match_id});
    card.appendChild(el('div', {class: 'match-meta'}, [el('span', {class: 'league-name', text: match.league || '赛事信息待补齐', title: match.league || ''}),
      el('span', {class: 'live-label' + (match.stale ? ' saved' : ''), text: match.stale ? '已保存数据' : '● LIVE'})]));
    const score = el('span', {class: 'score', text: match.score ? match.score.join(' : ') : '— : —'}, [el('small', {text: match.clock || '时间未核验'})]);
    card.appendChild(el('button', {class: 'match-title', 'data-focus': match.match_id, 'aria-label': '分析' + match.home + '对' + match.away,
      onclick: () => selectMatch(match.match_id)}, [el('span', {class: 'team', text: match.home || '队名待补齐', title: match.home || ''}), score,
      el('span', {class: 'team away', text: match.away || '队名待补齐', title: match.away || ''})]));
    const full = (match.markets || []).filter(m => ['HAD', 'AH', 'OU'].includes(m.market));
    const shown = S.expandedMatches.has(match.match_id) ? match.markets || [] : ['HAD', 'AH', 'OU'].map(family =>
      full.filter(m => m.market === family).sort((a,b) => Number(b.complete !== false) - Number(a.complete !== false) ||
        Math.abs((a.quotes[0]?.p_market ?? .5) - .5) - Math.abs((b.quotes[0]?.p_market ?? .5) - .5))[0]).filter(Boolean);
    for (const market of shown) card.appendChild(marketRow(market, match));
    if ((match.markets || []).length > 3) card.appendChild(el('button', {class: 'market-expand',
      text: (S.expandedMatches.has(match.match_id) ? '收起' : '全部盘口') + ' · ' + match.markets.length,
      onclick: () => {if (S.expandedMatches.has(match.match_id)) S.expandedMatches.delete(match.match_id);
        else S.expandedMatches.add(match.match_id); renderMatches();}}));
    const forecast = match.picks?.[0] || match.forecasts?.[0];
    if (forecast && !match.stale) card.appendChild(el('div', {class: 'signal-row'}, [
      el('strong', {text: forecast.label}), el('span', {text: 'P ' + pct(forecast.p_model) + ' · EV ' + pct(forecast.ev)})]));
    if (Object.keys(match.probabilities || {}).length) card.appendChild(probabilityRows(match, true));
  card.appendChild(el('div', {class: 'match-bottom'}, [el('span', {class: 'observation', text: match.suspended ? '暂停' : (match.has_buy ? '研究建议' : match.decision === 'forecast' && !match.stale ? '研究判断' : '观察')}),
      el('button', {class: 'analysis-link', text: '详情', title: '查看比赛分析', onclick: () => selectMatch(match.match_id)})]));
    return card;
  });
  $('matches').replaceChildren(...nodes);
  if (!nodes.length) $('matches').appendChild(emptyMessage(S.matches.length ? '没有符合筛选的赛事' : '等待这一组赛事的实时行情',
    S.matches.length ? '试试其他球队、联赛，或清除当前筛选。' : '采集连接和算法状态见上方。赛事出现后会自动更新。', !S.matches.length));
  if (focus) {
    const node = Array.from($('matches').querySelectorAll('[data-focus]')).find(n => n.getAttribute('data-focus') === focus);
    node?.focus({preventScroll: true});
  }
}
function renderLeagues() {
  const counts = new Map();
  for (const match of S.matches) counts.set(match.league || '未分类', (counts.get(match.league || '未分类') || 0) + 1);
  $('league-total').textContent = S.matches.length;
  $('all-leagues').classList.toggle('active', !S.league);
  $('leagues').replaceChildren(...Array.from(counts).sort((a, b) => a[0].localeCompare(b[0])).map(([league, count]) =>
    el('button', {class: 'league' + (S.league === league ? ' active' : ''), onclick: () => {S.league = league; renderLeagues(); renderMatches();}},
      [el('span', {text: league}), el('span', {text: String(count)})])));
}
async function loadLive() {
  if (S.busy || S.view !== 'live') return;
  S.busy = true;
  $('refresh').disabled = true;
  const request = ++S.request;
  const type = S.type;
  try {
    const data = await get('/workbench?type=' + encodeURIComponent(type));
    if (request !== S.request || type !== S.type) return;
    S.loaded = true;
    S.matches = data.matches || [];
    const online = Number(data.realtime?.connected || 0) > 0;
    $('connection').classList.toggle('online', online);
    $('connection').querySelector('span').textContent = online ? '实时连接正常' : '采集暂未连接';
    $('match-count').textContent = S.matches.length;
    $('match-count').previousElementSibling.textContent = '已分析赛事';
    const coverage = data.coverage || {};
    const sourceCount = coverage.source_current ?? coverage.source_upstream ?? coverage.source_derived;
    $('source-match-count').textContent = sourceCount !== null && sourceCount !== undefined && Number.isFinite(Number(sourceCount)) ? sourceCount : '—';
    $('subscribed-count').textContent = coverage.subscribed !== null && coverage.subscribed !== undefined && Number.isFinite(Number(coverage.subscribed)) ? coverage.subscribed : '—';
    $('source-match-count').title = coverage.source_age_s !== null && coverage.source_age_s !== undefined ? '赛程缓存 ' + fixed(coverage.source_age_s, 1) + 's' : '赛程未确认';
    const quoteAges = S.matches.map(m => m.quote_age_s).filter(Number.isFinite);
    $('quote-freshness').textContent = quoteAges.length ? fixed(Math.min(...quoteAges), 1) + 's 前' : '—';
    const p95 = (data.performance?.by_type_input_to_result_ms?.[type] || data.performance?.input_to_result_ms)?.p95;
    $('algorithm-latency').title = '最新输入至决策 P95；投注接收至提交耗时见执行状态';
    $('algorithm-latency').textContent = p95 !== null && p95 !== undefined ? fixed(p95, 0) + ' ms' : '—';
    notice('notice', data.projection?.loading ? '正在生成数据视图' : data.projection?.stale ? '后台视图更新延迟 · 显示缓存' : online ? (S.matches.some(m => m.stale) ? '部分报价过期' : '') : '采集断开 · 显示缓存');
    renderMatches(); renderLeagues();
    if (S.selected) await loadDetail(S.selected);
  } catch (error) {
    notice('notice', error.name === 'TimeoutError' ? '读取超时，请稍后刷新。已读内容暂时保留。' : error.message + '，可点击刷新重试。', true);
    S.matches = S.matches.map(m => ({...m, stale: true}));
    renderMatches();
    $('connection').classList.remove('online');
    $('connection').querySelector('span').textContent = '连接异常';
    if (!S.loaded) $('matches').replaceChildren(emptyMessage('暂时无法读取赛事', '请检查连接并重试。', true));
  } finally {
    S.busy = false;
    $('refresh').disabled = false;
  }
}
async function selectMatch(mid) {
  if (S.selected !== mid) S.chartKey = '';
  S.selected = mid;
  $('detail').classList.add('has-detail');
  $('detail').replaceChildren(el('div', {class: 'detail-placeholder'}, [el('h2', {text: '正在读取赛事分析…'})]));
  renderMatches();
  await loadDetail(mid);
}
async function loadDetail(mid) {
  const request = ++S.detailRequest;
  try {
    const data = await get('/workbench/' + encodeURIComponent(mid));
    if (request !== S.detailRequest || S.selected !== mid) return;
    renderDetail(data);
  } catch (error) {
    if (S.selected !== mid || request !== S.detailRequest) return;
    $('detail').replaceChildren(el('div', {class: 'detail-placeholder'}, [el('h2', {text: '该场分析暂不可用'}),
      el('p', {text: error.message}), el('button', {class: 'button', text: '返回赛事', onclick: closeDetail})]));
  }
}
function closeDetail() {
  S.selected = null; S.detailRequest++;
  $('detail').classList.remove('has-detail');
  $('detail').replaceChildren(el('div', {class: 'detail-placeholder'}, [el('span', {class: 'placeholder-icon', text: '◎'}),
    el('h2', {text: '选择比赛'})]));
  renderMatches();
}
function detailSection(title) {
  return el('section', {class: 'detail-section'}, [el('h3', {text: title})]);
}
function priceChart(points) {
  points = points.filter(p => Array.isArray(p) && p.length >= 2 && p.every(v => Number.isFinite(Number(v))));
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 320 130'); svg.setAttribute('class', 'chart');
  svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', '已接收报价的时间走势');
  if (!points.length) return svg;
  const add = (tag, attrs, label) => {
    const node = document.createElementNS(svg.namespaceURI, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (label !== undefined) node.textContent = label;
    svg.appendChild(node); return node;
  };
  const vals = points.map(p => Number(p[1]));
  const low = Math.min(...vals), high = Math.max(...vals);
  const bottom = low === high ? low - .01 : low;
  const top = low === high ? high + .01 : high;
  const start = Number(points[0][0]), end = Number(points.at(-1)[0]);
  for (const [value, y] of [[top, 14], [(top + bottom) / 2, 54], [bottom, 94]]) {
    add('line', {x1: 40, x2: 310, y1: y, y2: y, stroke: '#e4e6ed'});
    add('text', {x: 34, y: y + 4, 'text-anchor': 'end', fill: '#7c8899', 'font-size': 10}, fixed(value));
  }
  const coordinates = points.map(p => (40 + 270 * (Number(p[0]) - start) / Math.max(1, end - start)).toFixed(1) + ',' +
    (94 - 80 * (Number(p[1]) - bottom) / (top - bottom)).toFixed(1));
  add('polyline', {points: coordinates.join(' '), fill: 'none', stroke: '#1c88ff', 'stroke-width': 2});
  const last = coordinates.at(-1).split(',');
  add('circle', {cx: last[0], cy: last[1], r: 3, fill: '#1c88ff'});
  const stamp = ts => new Date(ts).toLocaleTimeString('zh-CN', {hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit'});
  add('text', {x: 40, y: 117, fill: '#7c8899', 'font-size': 10}, stamp(start));
  add('text', {x: 310, y: 117, 'text-anchor': 'end', fill: '#7c8899', 'font-size': 10}, stamp(end));
  return svg;
}
function renderDetail(match) {
  const panel = $('detail');
  const scroll = panel.scrollTop;
  const header = el('div', {class: 'detail-header'}, [el('div', {class: 'detail-top'}, [el('span', {text: match.league || '比赛详情'}),
    el('button', {class: 'close-detail', text: '关闭 ×', onclick: closeDetail})]),
    el('h2', {text: (match.home || '主队') + ' vs ' + (match.away || '客队'), title: (match.home || '') + ' vs ' + (match.away || '')}),
    el('span', {class: 'observation', text: (match.score ? match.score.join(' : ') : '— : —') + ' · ' + match.clock})]);
  const decisions = detailSection('汇总判断');
  decisions.appendChild(el('div', {class: 'config-version', text: algorithmLabel(match.algorithm) + ' · v' + (match.config_version || '—') +
    ' · ' + ({recorded: '首判已记录', queued: '首判写入中', none: '暂无首判'}[match.recording_status] || '')}));
  for (const f of match.forecasts || []) {
    const row = el('div', {class:'decision-row'}, [el('span',{},[el('strong',{text:f.label}),
      el('small',{text:'置信度 ' + pct(f.confidence) + ' · 分歧 ' + pct(f.disagreement)})]),
      el('span',{text:'@' + fixed(f.odds) + ' · P ' + pct(f.p_model)},[el('small',{text:'净EV ' + pct(f.effective_ev)})])]);
    if ((f.members || []).length) row.appendChild(el('details',{class:'member-breakdown'},[el('summary',{text:f.members.length + ' 算法'}),
      ...f.members.map(m=>el('div',{text:algorithmLabel(m.algorithm) + ' · P ' + pct(m.p_model) + ' · 权重 ' + pct(m.weight)}))]));
    decisions.appendChild(row);
  }
  if (!(match.forecasts || []).length) decisions.appendChild(el('p', {class: 'chart-note', text: '数据未满足判断条件'}));
  for (const pick of match.picks || []) decisions.appendChild(el('div', {class: 'recommendation-row'}, [
    el('strong', {text: '研究建议 · ' + pick.label}), el('span', {text: '@' + fixed(pick.odds) + ' · P ' + pct(pick.p_model) + ' · EV ' + pct(pick.ev)})]));
  const review = match.llm_review || {status: 'disabled'};
  if (review.status !== 'disabled') decisions.appendChild(el('p', {class: 'chart-note', text: 'LLM · ' +
    ({ready: {confirm:'认可', watch:'观察', reject:'不认可'}[review.verdict] + ' ' + pct(review.confidence),
      error:'失败', pending:'审核中', expired:'已过时'}[review.status] || review.status), title: review.reason || ''}));
  const evidence = el('details', {class: 'detail-section evidence-toggle'}, [el('summary', {text: '模型基准 · 数据状态'})]);
  if (match.stale) evidence.appendChild(el('p', {class: 'evidence', text: '报价已过期'}));
  for (const reason of match.reasons || []) evidence.appendChild(el('p', {class: 'evidence', text: reason}));
  const probability = detailSection('胜平负概率');
  probability.title = '市场拟合模型基准，独立预测优势尚未验证';
  probability.appendChild(probabilityRows(match));
  if (!Object.keys(match.probabilities || {}).length) probability.appendChild(el('p', {class: 'chart-note', text: '暂无概率'}));
  const intensity = detailSection('剩余进球强度');
  const rates = Array.isArray(match.remaining_goals) ? match.remaining_goals : [];
  if (rates.length >= 2) {
    const values = el('div', {class: 'intensity-grid'});
    for (const [label, value] of [['主队', rates[0]], ['客队', rates[1]]]) {
      values.appendChild(el('div', {title: '模型预计的剩余进球数'}, [el('span', {text: label + ' λ'}), el('strong', {text: fixed(value, 2)})]));
    }
    intensity.appendChild(values);
  } else intensity.appendChild(el('p', {class: 'chart-note', text: '暂无强度'}));
  const trend = detailSection('盘口走势');
  const seriesList = Object.entries(match.price_history || {}).filter(([, points]) => points.length > 1);
  const series = seriesList.find(([key]) => key === S.chartKey) || seriesList.find(([key]) => key.startsWith('OU|')) || seriesList[0];
  if (series) {
    S.chartKey = series[0];
    const select = el('select', {'aria-label': '走势图盘口', class: 'chart-select'});
    for (const [key] of seriesList) {
      const [market, line, outcome] = key.split('|');
      const option = el('option', {value: key, text: ({HAD: '独赢', OU: '大小球', AH: '让球'}[market.replace('_1H', '')] || market) +
        (market.includes('_1H') ? ' 上半场' : '') + ' ' + line + ' ' + ({home: '主', away: '客', draw: '平', over: '大', under: '小'}[outcome] || outcome)});
      option.selected = key === series[0]; select.appendChild(option);
    }
    select.addEventListener('change', () => {S.chartKey = select.value; renderDetail(match);});
    trend.appendChild(select);
    trend.appendChild(priceChart(series[1]));
  } else trend.appendChild(el('p', {class: 'chart-note', text: '暂无走势'}));
  const candidates = detailSection('盘口估值 · 模型基准');
  const estimates = el('table', {class: 'ev-table'}, [el('thead', {}, [el('tr', {}, ['方向', '赔率', '模型', 'EV', '有效EV', '状态'].map(text => el('th', {text})))])]);
  const estimatesBody = el('tbody');
  for (const candidate of (match.candidates || []).slice(0, 8)) estimatesBody.appendChild(el('tr', {}, [
    el('td', {text: candidate.label}), el('td', {text: fixed(candidate.odds)}),
    el('td', {text: pct(candidate.p_model)}), el('td', {class: Number(candidate.ev) > 0 ? 'history-win' : 'history-loss', text: pct(candidate.ev)}),
    el('td', {class: Number(candidate.effective_ev) > 0 ? 'history-win' : 'history-loss', text: pct(candidate.effective_ev)}),
    el('td', {text: candidate.research_only ? '研究判断' : candidate.decision_status === 'settleable' ? '可结算' : '展示'})]));
  estimates.appendChild(estimatesBody);
  if ((match.candidates || []).length) candidates.appendChild(estimates);
  if (!(match.candidates || []).length) candidates.appendChild(el('p', {class: 'chart-note', text: '暂无 EV'}));
  const coverage = match.market_coverage || {};
  const markets = detailSection('盘口 · ' + (coverage.total ?? match.markets?.length ?? 0)); markets.classList.add('detail-markets');
  const marketTabs = el('div', {class: 'market-tabs'});
  for (const [scope,label] of [['all','全部'],['full','全场'],['half','半场'],['other','其他']]) marketTabs.appendChild(el('button', {
    class: S.marketScope === scope ? 'active' : '', text: label, onclick: () => {S.marketScope = scope; renderDetail(match);}}));
  markets.appendChild(marketTabs);
  markets.appendChild(el('p', {class: 'chart-note', text: '完整 ' + (coverage.complete ?? '—') + ' · 新鲜 ' + (coverage.fresh ?? '—') + ' · 估值 ' + (coverage.valued ?? '—')}));
  const marketList = (match.markets || []).filter(m => S.marketScope === 'all' ||
    (S.marketScope === 'half' && m.market.includes('_1H')) || (S.marketScope === 'other' && m.market.startsWith('RAW_')) ||
    (S.marketScope === 'full' && ['HAD','AH','OU'].includes(m.market)));
  for (const market of marketList) markets.appendChild(marketRow(market, match));
  if (!marketList.length) markets.appendChild(el('p', {class: 'chart-note', text: '暂无盘口'}));
  const context = detailSection('赛况与数据时间');
  context.appendChild(el('p', {class: 'chart-note', text: '报价 ' + fixed(match.quote_age_s, 1) + 's · 结果 ' + fixed(match.result_age_s, 1) + 's · 计算 ' + fixed(match.compute_ms, 1) + 'ms'}));
  const events = match.events || [];
  context.appendChild(el('p', {class: 'chart-note', text: events.length ? events.length + ' 条状态事件' : '暂无状态事件'}));
  panel.replaceChildren(header, decisions, probability, intensity, trend, candidates, markets, context, evidence);
  panel.scrollTop = scroll;
}
const HISTORY_LOADING_RETRY_MS = 2000;
const HISTORY_LOADING_MAX_RETRIES = 15;
async function loadHistory(reset = true, automatic = false) {
  clearTimeout(S.historyRetryTimer);
  if (!automatic) S.historyLoadingRetries = 0;
  if (reset !== false) S.historyOffset = 0;
  const request = ++S.historyRequest;
  $('history-refresh').disabled = true;
  try {
    const data = await get('/ledger/history?limit=100&offset=' + S.historyOffset + '&days=' + encodeURIComponent($('history-days').value) + '&type=' + encodeURIComponent($('history-type').value) + '&algorithm=' + encodeURIComponent($('history-algorithm').value) + '&cohort=' + encodeURIComponent($('history-cohort').value));
    if (request !== S.historyRequest) return;
    const statisticsLoading = data.statistics_status === 'loading' || data.overall === null;
    const entriesLoading = data.entries_status === 'loading';
    const summary = data.overall || {};
    const pendingEntries = statisticsLoading ? '—' : Number(summary.pending || 0);
    const metrics = [['全盘口正确率', pct(summary.accuracy ?? summary.hit_rate)], ['有效样本 / 场次', statisticsLoading ? '— / —' : String(summary.accuracy_samples ?? ((summary.won || 0) + (summary.lost || 0))) + ' / ' + (data.settled?.matches || 0)], ['等额 ROI', pct(summary.roi)], ['置信度 ROI', pct(summary.confidence_roi)], ['仓位 ROI', pct(summary.allocated_roi)], ['待结算盘口', String(pendingEntries)]];
    $('history-stats').replaceChildren(...metrics.map(([label, value]) => el('div', {}, [el('span', {text: label}), el('strong', {text: value})])));
    const portfolio = data.portfolio_summary || {};
    $('history-portfolio').replaceChildren(...[['正确率',pct(portfolio.accuracy)],['有效样本',statisticsLoading ? '—' : String(portfolio.accuracy_samples || 0)],['等额 ROI',pct(portfolio.roi)],['置信度 ROI',pct(portfolio.confidence_roi)],['仓位 ROI',pct(portfolio.allocated_roi)],['净收益',fixed(portfolio.profit_units) + 'u']].map(([label,value])=>el('div',{},[el('span',{text:label}),el('strong',{text:value})])));
    const weights = data.algorithm_weights?.algorithms || {};
    $('weight-at').textContent = data.algorithm_weights?.at ? '证据截至 ' + new Date(data.algorithm_weights.at).toLocaleTimeString('zh-CN',{hour12:false}) : '';
    $('history-algorithms').replaceChildren(...Object.entries(data.by_algorithm || {}).map(([algorithm, stats]) => {
      const w = weights[algorithm] || {};
      return el('tr',{},[el('td',{text:algorithmLabel(algorithm)}),
        el('td',{text:pct(stats.accuracy ?? stats.hit_rate) + ' / ' + (stats.accuracy_samples ?? (stats.won+stats.lost) ?? 0)}),
        el('td',{text:pct(stats.roi)}), el('td',{text:pct(stats.confidence_roi)}), el('td',{text:pct(stats.allocated_roi)}),
        el('td',{text:pct(w.weight)}),el('td',{text:(w.matches ?? '—') + ' / ' + fixed(w.brier,3)})]);
    }));
    const alerts = (data.algorithm_alerts || []).filter(a => a.status === 'warning');
    $('algorithm-alerts').replaceChildren(...(alerts.length ? alerts.map(a => el('div', {class: 'notice error', text: algorithmLabel(a.algorithm) + '：' + a.message})) : []));
    $('confidence-returns').replaceChildren(...Object.entries(data.by_confidence || {}).map(([band,stats])=>el('div',{class:'algorithm-card'},[
      el('strong',{text:band}),el('span',{text:'正确率 ' + pct(stats.accuracy) + ' · ' + stats.accuracy_samples + ' 注 · ROI ' + pct(stats.roi)})])));
    const timeline = data.timeline || [];
    const points = timeline.map((v,i)=>[i,v.cum_profit]);
    $('profit-chart').replaceChildren();
    if (points.length) {
      const max = Math.max(1,...points.map(p=>Math.abs(p[1])));
      const svg=document.createElementNS('http://www.w3.org/2000/svg','svg'); svg.setAttribute('viewBox','0 0 640 100'); svg.setAttribute('role','img'); svg.setAttribute('aria-label','累计研究净收益');
      const path=document.createElementNS(svg.namespaceURI,'polyline');path.setAttribute('points',points.map(([x,y])=>(20+x*600/Math.max(1,points.length-1))+','+(50-y/max*40)).join(' '));
      path.setAttribute('fill','none');path.setAttribute('stroke','#d89a35');path.setAttribute('stroke-width','2');svg.appendChild(path);$('profit-chart').appendChild(svg);
      $('profit-chart').appendChild(el('small',{text:timeline[0].date + ' → ' + timeline.at(-1).date + ' · 累计净收益 ' + fixed(timeline.at(-1).cum_profit) + 'u'}));
    }
    const experiment = data.experiments || {};
    const paired = experiment.paired || {};
    $('experiment-status').textContent = '配对 ' + (experiment.paired_opportunities || 0) + ' · ' + (experiment.conclusion || '等待实验样本');
    $('experiment-results').replaceChildren(...Object.entries(paired).map(([arm,stats])=>el('div',{class:'algorithm-card'},[
      el('strong',{text:algorithmLabel(arm)}),el('span',{text:'正确率 ' + pct(stats.accuracy) + ' · ROI ' + pct(stats.roi) + ' · 本金 ' + fixed(stats.stake_units) + 'u'})])));
    $('experiment-results').appendChild(el('div',{class:'algorithm-card'},[el('strong',{text:'配对差异'}),el('span',{text:'正确率 Δ ' + pct(experiment.accuracy_difference) + ' · 净收益 Δ ' + fixed(experiment.profit_difference) + 'u · 覆盖 ' + pct(experiment.coverage)})]));
    $('experiment-results').appendChild(el('div',{class:'algorithm-card'},[el('strong',{text:'审核状态'}),el('span',{text:Object.entries(experiment.review_status || {}).map(([k,v])=>({ready:'已审核',error:'失败',expired:'迟到',pending:'等待'}[k] || k) + ' ' + v).join(' · ') || '未开始；在设置启用 LLM'})]));
    const experimentErrors = Object.entries(experiment.errors || {}).filter(([code])=>code && code!=='null');
    if (experimentErrors.length) $('experiment-results').appendChild(el('div',{class:'algorithm-card'},[el('strong',{text:'接口诊断'}),el('span',{text:experimentErrors.map(([code,n])=>code+' ×'+n).join(' · ')})]));
    S.historyNext = data.next_offset ?? null;
    $('history-prev').disabled = S.historyOffset === 0;
    $('history-next').disabled = S.historyNext === null;
    $('history-page').textContent = '第 ' + (Math.floor(S.historyOffset/100)+1) + ' 页 · 查询 ' + fixed(data.query_ms,0) + 'ms';
    $('history-count').textContent = (data.entries_total ?? '—') + ' 条 · ' + (statisticsLoading ? '—' : summary.matches || 0) + ' 场 · 待结算 ' + pendingEntries + ' 个盘口';
    const historyMessages = [];
    if (data.statistics_refreshing || statisticsLoading) historyMessages.push('战绩统计正在后台汇总，完成后刷新可查看最新统计。');
    if (entriesLoading) historyMessages.push('当前筛选的记录页正在后台读取，自动等待更新。');
    if (data.entries_total == null && !entriesLoading) historyMessages.push('记录已加载，总条数仍在后台统计；可继续翻页。');
    if (data.entries_refreshing) historyMessages.push('后台正在更新当前记录页。');
    if (data.entries_stale) historyMessages.push('当前记录页为上次缓存，可能暂未包含新记录。');
    if (data.entries_at) historyMessages.push('记录页时点：' + (timeLabel(data.entries_at) === '—' ? '暂不可用' : timeLabel(data.entries_at)) + '。');
    if (data.entries_error) historyMessages.push('记录页读取失败：' + data.entries_error + '，可刷新重试。');
    if (data.statistics_error) historyMessages.push('战绩汇总暂不可用：' + data.statistics_error + '。');
    if (data.statistics_stale) historyMessages.push('当前显示上次统计缓存，可能暂未包含最新结算。');
    if (data.statistics_at) {
      const at = new Date(data.statistics_at);
      historyMessages.push(Number.isNaN(at.getTime()) ? '统计时点暂不可用。' : '统计时点：' + at.toLocaleString('zh-CN', {hour12:false}) + '。');
    }
    if (data.legacy_identity_rows) historyMessages.push('旧记录 ' + data.legacy_identity_rows + ' 条，身份不完整。');
    if (!summary.graded && !data.statistics_refreshing && !statisticsLoading) historyMessages.push('待确认终场赛果 · ' + (data.settle?.evidence_missing ?? summary.pending ?? 0) + ' 场未取得证据');
    notice('history-notice', historyMessages.join(' '),data.entries_status === 'error' || data.statistics_status === 'error' || !!data.statistics_error);
    const statusLabel = {won: '正确', lost: '错误', half_won: '赢半', half_lost: '输半', push: '走水', pending: '待结算', void: '无法结算'};
    const rows = (data.entries || []).map(entry => {
      const date = new Date(entry.at);
      const time = Number.isNaN(date.getTime()) ? entry.at : date.toLocaleString('zh-CN', {hour12: false});
      const kind = entry.pnl > 0 ? 'history-win' : entry.pnl < 0 ? 'history-loss' : '';
      return el('tr', {}, [el('td', {text: time}, [el('small', {text: entry.competition_type === 'virtual' ? '虚拟比赛' : entry.competition_type === 'real' ? '真实足球' : '旧记录 / 类型未核验'})]),
        el('td', {text: (entry.home || '主队') + ' vs ' + (entry.away || '客队')}, [el('small', {text: entry.label || entry.market})]),
        el('td', {text: algorithmLabel(entry.algorithm)}, [el('small', {text: 'v' + (entry.config_version || 0) + ' · P ' + pct(entry.p_fused)})]),
        el('td', {text: fixed(entry.odds)}, [el('small', {text: entry.entry_clock_s != null ? '入场 ' + fixed(entry.entry_clock_s, 0) + 's' : ''})]),
        el('td', {text: entry.ft_score ? entry.ft_score.join(' : ') : '—'}, [el('small', {text: entry.status === 'pending' ? '比赛进行中 / 等待终场' : ''})]),
        el('td', {class: kind, text: statusLabel[entry.status] || entry.status, title: entry.settle_note || ''}),
        el('td', {class: kind, text: ['pending', 'void'].includes(entry.status) ? '—' : (entry.pnl > 0 ? '+' : '') + fixed(entry.pnl)})]);
    });
    $('history-rows').replaceChildren(...rows);
    $('history-empty').hidden = !!rows.length;
    $('history-empty').textContent = entriesLoading ? '记录页正在加载，请稍候。' : data.entries_status === 'error' ? '记录页暂不可读取，请刷新重试。' : '暂无符合筛选条件的记录。';
    if ((entriesLoading || statisticsLoading) && (S.historyLoadingRetries || 0) < HISTORY_LOADING_MAX_RETRIES) {
      S.historyLoadingRetries = (S.historyLoadingRetries || 0) + 1;
      S.historyRetryTimer = setTimeout(() => {if (S.view === 'history' && S.historyRequest === request) loadHistory(false,true);},HISTORY_LOADING_RETRY_MS);
    }
  } catch (error) {
    if (request === S.historyRequest) notice('history-notice', error.message + '，可刷新重试。', true);
  } finally {
    if (request === S.historyRequest) $('history-refresh').disabled = false;
  }
}
const ORIGINAL_SETTING_FIELDS = [
  ['算法与门槛', [
    ['decision_mode', '决策模式', 'select', {algorithm: '纯算法', model: '纯决策模型', hybrid: '算法 + 决策模型'}],
    ['model_blend_weight', '混合模型权重', 'number', [0,1,.01]],
    ['primary_algorithm', '强度参考算法', 'select', {poisson_market: '当前盘口 Poisson', poisson_time_decay: '时间衰减 Poisson', devig_consensus: '去水共识', economics_risk_adjusted: '经济学风控', microstructure_adjusted: '盘口微观结构'}],
    ['devig_method', '去水方法', 'select', {proportional: '比例法', power: 'Power 法'}],
    ['min_probability', '建议最低概率', 'number', [0,1,.01]],
    ['min_ev', '建议最低 EV', 'number', [0,1,.01]],
    ['anchor_max_age_s', '衰减锚点重置 / 秒', 'number', [5,600,1]],
    ['score_change_cooldown_s', '比分变化冷却 / 秒', 'number', [0,600,1]],
    ['devig_spread_warn_pp', '去水方法分歧阈值 / pp', 'number', [.1,20,.1]],
    ['fractional_kelly', '分数 Kelly', 'number', [0,1,.05]],
    ['weight_prior_matches', '权重收缩先验 / 场', 'number', [2,1000,1]],
    ['max_algorithm_weight', '单算法最大权重', 'number', [.2,1,.05]],
    ['algorithm_alert_enabled', '低正确率预警', 'checkbox'],
    ['algorithm_alert_min_samples', '预警最小有效样本', 'number', [1,100000,1]],
    ['algorithm_alert_threshold', '预警正确率阈值', 'number', [0,1,.01]],
    ['betting_enabled', '自动真实投注', 'checkbox'],
    ['betting_stake_mode', '投注额度模式', 'select', {fixed: '固定值', confidence_multiplier: '综合置信度 × 固定值'}],
    ['betting_fixed_stake', '固定投注额度 / CNY', 'number', [.01,100000,.01]],
    ['betting_min_hit_count', '最小决策出现次数', 'number', [1,100000,1]],
    ['betting_min_evidence_matches', '最小独立结算比赛数', 'number', [1,100000,1]],
    ['betting_min_edge', '真实下注最低优势', 'number', [0,1,.01]],
    ['betting_retry_max_attempts', '每轮最多尝试次数', 'number', [1,20,1]],
    ['betting_retry_base_delay_s', '重试基础间隔 / 秒', 'number', [.5,30,.5]],
    ['max_total_exposure', '同场最大敞口', 'number', [0,1,.01]],
    ['risk_correlation', '同场风险相关性', 'number', [0,.99,.01]],
    ['execution_cost', '执行成本', 'number', [0,.1,.001]],
  ]],
  ['数据时效', [
    ['quote_max_age_s', '报价有效期 / 秒', 'number', [1,300,1]],
    ['state_max_age_s', '比分与时钟有效期 / 秒', 'number', [5,600,1]],
  ]],
  ['LLM 审核', [
    ['llm_enabled', '启用后台审核', 'checkbox'],
    ['llm_experiment_enabled', '记录经济学 / LLM 配对实验', 'checkbox'],
    ['llm_base_url', 'API 地址', 'url'],
    ['llm_model', '模型', 'text'],
    ['llm_fallback_models', '备用模型（逗号分隔）', 'text'],
    ['llm_api_key', 'API Key', 'password'],
    ['llm_timeout_s', '超时 / 秒', 'number', [1,120,1]],
    ['llm_temperature', 'Temperature', 'number', [0,2,.1]],
    ['llm_max_tokens', '输出 Token 上限', 'number', [256,8192,1]],
    ['llm_interval_s', '单场审核间隔 / 秒', 'number', [10,3600,1]],
    ['llm_review_max_age_s', '审核输入有效期 / 秒', 'number', [30,600,1]],
  ]],
];
const TRAINING_FIELDS = [
  ['model_shadow_enabled', '记录模型影子预测', 'checkbox'],
  ['model_training_enabled', '自动更新训练模型', 'checkbox'],
  ['model_training_matches', '更新周期 / 新增已结算比赛数', 'number', [1,100000,1]],
  ['model_training_cpu', '训练 CPU / 核', 'number', [1,2,1]],
  ['model_training_memory_mb', '训练内存上限 / MiB', 'number', [256,4096,1]],
];
const FIELD_MAP = Object.fromEntries([...ORIGINAL_SETTING_FIELDS.flatMap(([,fields]) => fields), ...TRAINING_FIELDS].map(f => [f[0],f]));
const SETTING_TREE = [
  ['决策', [
    ['模型与信号', [['算法与概率', ['decision_mode','model_blend_weight','primary_algorithm','devig_method','min_probability','min_ev']], ['时间与共识', ['anchor_max_age_s','score_change_cooldown_s','devig_spread_warn_pp']], ['算法融合', ['weight_prior_matches','max_algorithm_weight']]]],
    ['风险与预警', [['仓位控制', ['fractional_kelly','max_total_exposure','risk_correlation','execution_cost']], ['表现预警', ['algorithm_alert_enabled','algorithm_alert_min_samples','algorithm_alert_threshold']]]],
  ]],
  ['执行', [['真实投注', [['功能开关', ['betting_enabled']], ['额度与条件', ['betting_stake_mode','betting_fixed_stake','betting_min_hit_count','betting_min_evidence_matches','betting_min_edge']], ['提交前重试', ['betting_retry_max_attempts','betting_retry_base_delay_s']]]]]],
  ['数据与模型', [
    ['模型训练', [['决策模式', ['model_shadow_enabled']], ['更新周期', ['model_training_enabled','model_training_matches']], ['资源限制', ['model_training_cpu','model_training_memory_mb']]]],
    ['实时数据', [['新鲜度', ['quote_max_age_s','state_max_age_s']]]],
  ]],
  ['LLM', [
    ['审核服务', [['服务与密钥', ['llm_enabled','llm_base_url','llm_model','llm_fallback_models','llm_api_key']], ['调用参数', ['llm_timeout_s','llm_temperature','llm_max_tokens']]]],
    ['审核实验', [['周期与记录', ['llm_experiment_enabled','llm_interval_s','llm_review_max_age_s']]]],
  ]],
];
const SETTING_FIELDS = SETTING_TREE.flatMap(([,menus]) => menus.flatMap(([,groups]) => groups.map(([name,keys]) => [name,keys.map(k => FIELD_MAP[k])])));
const ALGORITHM_HELP = {
  poisson_market: '由当前大小球和独赢盘口拟合主客队剩余进球强度 λ。公式：P(X=k)=e^(−λ)·λ^k/k!；主客队独立时 P(H=h,A=a)=P(H=h)·P(A=a)。在当前比分上累加剩余进球，按盘口结算规则求胜、负和走盘权重。',
  poisson_time_decay: '使用盘口锚点的剩余进球强度随比赛时间衰减。公式：λ_i(t)=λ_i(anchor)·max(0,5400−t)/max(1,5400−t_anchor)；P(X=k)=e^(−λ_i(t))·λ_i(t)^k/k!。比分变化或锚点过期时重新拟合。t 的单位为秒。',
  devig_consensus: '计算多种去水方法及其分歧，当前结论优先采用 Shin，失败时回退比例法。公式：q_i=1/o_i，B=Σq_i；Shin p_i=(√(z²+4(1−z)q_i²/B)−z)/(2(1−z))，求 z 使 Σp_i=1；比例法 p_i=q_i/B。',
  economics_risk_adjusted: '将衰减 Poisson 概率向当前市场概率收缩，并折扣相关风险。公式：p=(p_Poisson+2·p_market)/3；f=max(0,λ_Kelly·(o·p−1)/(o−1))；成员仓位 f_risk=f·(1−ρ)，有效 EV 另扣 min(0.5,ρ·f)。',
  microstructure_adjusted: '按已接收盘口走势对衰减 Poisson 概率作调整。公式：r=min(0.5,|trend_pct|/100)；p=clip(p_Poisson−r·s,0,1)，trend_pct>0 时 s=1，否则 s=−1。调整后按当前赔率重新计算 EV。',
};
const SETTING_HELP = {
  decision_mode: '纯算法采用现有算法融合概率；纯决策模型采用通过版本和晋级校验的模型概率，无有效模型时不产生买入；混合模式结合二者。公式：p_hybrid=(1−w)·p_algorithm+w·p_model，再按同盘口约束校正并重新计算 EV 与仓位。',
  model_blend_weight: '混合模式中决策模型的权重 w，取值 0~1。公式：p_hybrid=(1−w)·p_algorithm+w·p_model。0 完全使用算法，1 完全使用模型；纯算法和纯模型模式不使用此值。',
  primary_algorithm: '选择页面展示主客队剩余进球强度时的参考算法，必须属于已启用算法；综合推荐仍由启用的算法融合生成。Poisson 公式：P(X=k)=e^(−λ)·λ^k/k!。各算法的具体公式见对应算法名称后的问号。',
  devig_method: '从盘口赔率中扣除水钱，生成市场公平概率。令 q_i=1/o_i；比例法 p_i=q_i/Σq_j；Power 法 p_i=q_i^k/Σq_j^k，数值求 k 使 Σq_i^k=1。用于当前市场概率及 Poisson 强度拟合；去水共识算法使用自己的 AUTO 方法。',
  min_probability: '显示买入建议的最低有效胜率，要求 p_model≥此值。含走盘盘口使用 p=P(win)/(P(win)+P(loss))，走盘份额单独计入期望收益；概率高仍须满足 EV、仓位等条件。',
  min_ev: '显示买入建议的最低有效期望收益率。公式：EV=(p·o−1)·(P(win)+P(loss))；EV_eff=q_fill·EV−cost_exec。此处比较 EV_eff；0.02 表示每单位注额的有效期望为 2%。',
  score_change_cooldown_s: '比分变化后的冷却秒数。场馆在比分推送后仍会发布数秒进球前的旧价格；冷却期内该场全部盘口视为不新鲜，不产生推荐，执行器也拒绝下单。0 表示关闭（不建议）。',
  anchor_max_age_s: '时间衰减 Poisson 的盘口锚点最长复用时间。超过此秒数、比分变化或时钟回退时重置锚点。公式：λ(t)=λ_anchor·max(0,5400−t)/max(1,5400−t_anchor)。',
  devig_spread_warn_pp: '不同去水方法概率分歧的告警阈值，单位为百分点 pp。公式：spread=100·max_(a,b) Σ_i |p_(a,i)−p_(b,i)|。超过阈值标记去水差异；1 pp 对应概率差 0.01。',
  fractional_kelly: '建议仓位的 Kelly 折扣系数 λ。公式：f=max(0,λ·(o·p−1)/(o−1))；o 为十进制赔率，p 为非走盘部分的胜率。随后按同场最大敞口缩放；真实固定额度模式独立计算金额。',
  weight_prior_matches: '算法融合权重的收缩先验场次 k。后验正确率 a=(wins+k/2)/(wins+losses+k)；相对市场技能 s=(Brier_market−Brier_model)·n/(n+k)；评分 score=clip((a/0.5)·exp(3s),0.25,2)，按评分归一化权重。k 越大，少量历史证据影响越小。',
  max_algorithm_weight: '限制融合中单算法权重并将剩余权重重新分配。实际 cap=max(设置值,1/启用算法数)，确保权重总和为 1；概率融合 p=Σ_i w_i·p_i。仅启用一个算法时权重为 1。',
  algorithm_alert_enabled: '开启后，已结算有效样本达到门槛且算法正确率低于阈值时，页面显示预警；此开关不会直接关闭算法。',
  algorithm_alert_min_samples: '触发表现预警所需的最小有效已结算样本数。样本不足时显示证据不足，不据少量结果判定算法失效。',
  algorithm_alert_threshold: '算法表现预警正确率阈值。公式：accuracy=wins/(wins+losses)；有效样本达到门槛且 accuracy<此值时告警。走盘和未结算不计入正确率分母。',
  betting_enabled: '允许后台执行满足决策出现次数、同版本独立结算证据、当前优势、盘口和余额校验的真实综合推荐。关闭后取消尚未提交的重试；已提交注单不会撤销。',
  betting_stake_mode: '真实投注金额计算方式。固定值：stake=base；综合置信度模式：stake=base·clip(composite_confidence,0,1)。最终按场馆限额和可用余额校验，金额精确到分。',
  betting_fixed_stake: '真实投注的基础金额，单位 CNY。固定模式直接使用此金额；置信度模式以此金额乘综合置信度。它与研究推荐的 Kelly 本金比例分开计算。',
  betting_min_hit_count: '同一比赛、盘口线和方向在获取新盘口后计算得到的决策最少出现次数。每次符合决策条件计一次；赔率变化不会改变已经提交注单的去重身份。此次数不代表赛果正确次数。',
  betting_min_evidence_matches: '真实投注所需的同版本独立已结算比赛数量，避免同场重复决策充当多个独立样本；同时还要求历史 ROI 为正。此项与决策出现次数分别校验。',
  betting_min_edge: '真实提交前要求的当前报价最低优势。优先使用最新报价的 effective_ev_live，否则使用有效 EV；优势低于此值时等待新决策。研究推荐可采用较低的建议 EV 门槛。',
  betting_retry_max_attempts: '可重试的会话、网络或盘口变化错误，每轮最多尝试次数，包含首次尝试。提交结果未知时核对场馆订单，不自动重发；达到上限后等待重新检查。',
  betting_retry_base_delay_s: '可重试错误的基础退避间隔。公式：delay=min(后台复查上限,base·2^(attempt−1)) 秒。重试前重新校验当前配置、决策和盘口；关闭投注会取消尚未提交的重试。',
  max_total_exposure: '单场研究推荐的 Kelly 总仓位上限，以本金比例表示。公式：scale=min(1,cap/Σf_i)，各推荐仓位 f_scaled=f_i·scale；0.25 表示同场最多 25%。此项不替代真实固定额度或场馆限额。',
  risk_correlation: '同场算法分歧与相关风险参数 ρ。经济学风控成员采用 f_risk=f·(1−ρ)、EV_penalty=min(0.5,ρ·f)；融合前分歧惩罚为 ρ·σ_p·odds。综合候选按一致性校正后的概率重新计算 EV 和 Kelly。',
  execution_cost: '每单位注额的执行成本比例。公式：EV_eff=q_fill·EV−cost_exec。当前 q_fill=clip(0.95·exp(−2·max(EV,0)),0.01,1)，为估算参数；0.001 表示扣除 0.1% 成本。',
  quote_max_age_s: '完整盘口报价允许参与估值的最长年龄。报价年龄超过此秒数或盘口暂停时，不用于生成新买入建议。',
  state_max_age_s: '比分和比赛时钟允许使用的最长年龄。超过此秒数时暂停基于该状态计算新决策，防止使用陈旧赛况。',
  llm_enabled: '启用独立后台 LLM 审核。经济学计算先完成，再异步审核同场盘口；此开关需要同时配置服务地址和模型。',
  llm_experiment_enabled: '记录纯经济学与经济学加 LLM 的配对实验，供战绩页比较同一机会上的结果与收益；不以未审核输出充当实验结论。',
  llm_base_url: 'OpenAI 兼容服务的基础 HTTP/HTTPS 地址。使用无用户名、密码、查询参数的服务地址；后台调用 chat/completions 接口。',
  llm_model: '后台 LLM 审核使用的服务模型标识，需要与配置的 API 服务支持的名称一致。',
  llm_fallback_models: '主模型请求失败时按顺序尝试的备用模型名称，使用英文逗号分隔；留空表示不配置备用模型。',
  llm_api_key: 'LLM 服务密钥仅写入后端，不在读取设置时回显。已有密钥时留空保留；需要删除时勾选下方“清除已保存 API Key”。',
  llm_timeout_s: '单次 LLM 请求的超时秒数。请求超过时限后记录审核错误，并按备用模型配置处理。',
  llm_temperature: 'LLM 输出采样温度。较低值通常使审核结果更稳定；实际支持范围与行为取决于服务提供方。',
  llm_max_tokens: '单次 LLM 审核允许生成的最大输出 token 数。较小值减少输出长度，也可能截断结构化结果。',
  llm_interval_s: '同一比赛两次后台审核之间的最小间隔秒数，避免每次盘口推送都触发远程请求。',
  llm_review_max_age_s: '审核输入的最大有效年龄。超过时限的审核结果标记为迟到，避免旧盘口审核覆盖新的决策。',
  model_shadow_enabled: '由独立 model-worker 记录模型影子预测和前瞻验证表现。此开关控制观察记录，与决策模式分别配置；纯算法、纯模型或混合结论由“决策模式”选择，模型参与时仍需有效模型与晋级校验。',
  model_training_enabled: '允许独立 model-worker 自动检查训练周期并生成候选模型。训练与 API 服务解耦，候选通过时间隔离验证和晋级门槛后才可用于决策。',
  model_training_matches: '上次训练后新增的有效已结算独立比赛数达到此值时触发更新。每场保留多盘口、多决策输入；仅使用决策时点前可见的走势。首次训练仍须达到最低有效比赛门槛。',
  model_training_cpu: '独立模型训练进程允许使用的 CPU 核数上限，降低值可减少训练对采集与 API 响应的竞争。此项不设置系统服务 CPU 限制。',
  model_training_memory_mb: '独立训练进程的内存上限，单位 MiB。候选训练超过限制可能被终止；失败状态会显示原因并保留已有可用模型。',
  clear_llm_key: '保存时删除后端已配置的 LLM API Key；不勾选且密钥输入留空时保留已有密钥。',
};
const PARAMETER_RESEARCH_HELP = {
  devig_method: '研究建议：保留当前方法，离线比较 proportional 与 power；不按短期正确率切换。按同比赛时间折的 Brier、Log loss 和可靠性选择，Power 的候选优势需要独立前瞻证据。',
  fractional_kelly: '研究建议范围 0.10~0.25，仅用于仓位控制；概率尚未证明优于市场时不提高折扣系数。参数不确定性会使全 Kelly 过度下注；真实固定金额模式不受此系数保护。',
  betting_min_evidence_matches: '研究建议：30场只适合最低运行门槛，100~300场独立前瞻比赛才适合初步复核。30场在正确率约50%时，95%误差约±18百分点；不因决策条目多就降低独立比赛门槛。',
  model_training_matches: '研究建议：保留100场更新周期，离线评估100~300场新增独立有效已结比赛的周期；每个周期保留独立时间测试，训练频繁不等于预测更准确。',
  min_probability: '研究建议：先保留0.52作为现有初值，仅离线比较0.50~0.60，并同时看覆盖率、赔率和净EV。不同盘口不能靠提高胜率阈值直接保证收益。',
  min_ev: '研究建议：保留0.02作为现有初值，仅离线比较0.02~0.05；成本、改价和成交偏差必须纳入净EV，不用提高历史正确率替代独立收益检验。',
  betting_min_edge: '研究建议：保留现有0.05提交门槛，研究区间0.03~0.08仅供冻结时间验证；没有净成本与成交证据前不放宽真实提交门槛。',
  weight_prior_matches: '研究建议：小样本时收缩先验20~100场，比只靠近期正确率分配权重稳健；采用按场Brier和独立时间验证后再调整。',
};
for (const [key, help] of Object.entries(PARAMETER_RESEARCH_HELP)) SETTING_HELP[key] += '\n' + help;
function settingControl(key, label, input, type, help) {
  const descriptionId = 'setting-help-' + key;
  const description = el('p', {id:descriptionId, class:'setting-description', role:'tooltip', text:help || SETTING_HELP[key] || '此设置的说明暂不可用。', hidden:''});
  const button = el('button', {type:'button', class:'setting-help', text:'?', title:description.textContent,
    'aria-label':label + '的设置说明', 'aria-expanded':'false', 'aria-controls':descriptionId, 'aria-describedby':descriptionId});
  const show = visible => {description.hidden = !visible; button.setAttribute('aria-expanded', String(visible));};
  button.addEventListener('click', () => show(description.hidden));
  button.addEventListener('keydown', event => {if (event.key === 'Escape') {show(false); event.stopPropagation();}});
  input.setAttribute('aria-describedby', [input.getAttribute('aria-describedby'), descriptionId].filter(Boolean).join(' '));
  const name = el('span', {class:'setting-label'}, [el('label', {for:input.id, text:label}), button]);
  return el('div', {class:type === 'algorithm' ? 'setting-algorithm' : type === 'checkbox' ? 'setting-toggle' : 'setting-field'},
    type === 'algorithm' ? [input, name, description] : [name, input, description]);
}
S.settingsPath = [0,0,0];
function selectSetting(category, menu, group) {
  S.settingsPath = [category,menu,group];
  const [title,menus] = SETTING_TREE[category];
  const [subtitle,groups] = menus[menu];
  const buttons = (items, active, callback) => items.map(([name], i) => el('button', {type:'button',class:'setting-nav-button' + (i === active ? ' active' : ''),text:name,'aria-pressed':String(i===active),onclick:()=>callback(i)}));
  $('settings-categories').replaceChildren(...buttons(SETTING_TREE,category,i=>selectSetting(i,0,0)));
  $('settings-submenus').replaceChildren(...buttons(menus,menu,i=>selectSetting(category,i,0)));
  $('settings-groups').replaceChildren(...buttons(groups,group,i=>selectSetting(category,menu,i)));
  $('settings-breadcrumb').textContent = title + ' / ' + subtitle + ' / ' + groups[group][0];
  for (const section of $('settings-fields').children) section.hidden = section.dataset.group !== groups[group][0];
}
let dataModelBusy = false;
const bytes = value => Number.isFinite(value) ? (value / 1024**3).toFixed(2) + ' GiB' : '—';
const timeLabel = value => value && Number.isFinite(new Date(value).getTime()) ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '—';
const STORAGE_STALE_MS = 5 * 60 * 1000;
const decisionModeLabel = mode => ({algorithm:'纯算法',model:'纯决策模型',hybrid:'算法 + 决策模型'}[mode] || '待后端更新');
function decisionModelStatus(data) {
  const mode = data.decision_mode || data.settings?.decision_mode;
  if (mode === 'algorithm') return {mode, state:'算法服务决策', reason:''};
  if (!['model','hybrid'].includes(mode)) return {mode, state:'模式信息暂不可用', reason:''};
  const unavailable = data.decision_mode_status === 'unavailable' || data.decision_model_available === false || !data.model?.version || data.model?.promotion?.approved !== true;
  const reason = unavailable ? data.decision_mode_reason || '暂无通过校验的已晋级模型，此模式暂不产生买入推荐。' : '';
  return {mode, state:unavailable ? '不可用' : '已加载 · 实际结果见实时决策', reason};
}
const unknownMetric = (value, label = '尚未审查') => value === null || value === undefined || value === '' ? label : value;
function shadowStatus(data) {
  const shadow = data.shadow || {};
  if (shadow.available === false) return {available:false, text:shadow.reason || '影子预测尚未可用'};
  if (!shadow.version) return {available:false, text:shadow.reason || '尚无影子模型版本'};
  const state = shadow.approved === true ? '已晋级影子模型' : shadow.artifact_kind === 'candidate' ? '候选影子模型（未晋级）' : '影子模型';
  return {available:true, text:state + ' · ' + shadow.version};
}
function prospectiveMetricRows(data) {
  const prospective = data.prospective || {};
  const model = prospective.model || {}, baseline = prospective.baseline || {};
  const status = prospective.status || (Object.keys(model).length ? 'ready' : 'unavailable');
  const reason = prospective.reason || (status === 'waiting_for_settlement' ? '已生成预测正在等待赛果结算。' : status === 'no_predictions' ? '尚未记录影子预测。' : status === 'unavailable' ? '当前没有可用的前瞻统计。' : '');
  const metric = value => value == null ? '暂无适用样本' : pct(value);
  const rows = [
    ['前瞻模型', status === 'ready' ? model : null], ['前瞻市场基准', status === 'ready' ? baseline : null],
  ];
  return {prospective,status,reason,model,baseline,rows,metric};
}
function metricCards(items) {
  return items.map(([label,value]) => el('div', {class:'model-metric'}, [el('span',{text:label}),el('strong',{text:String(value ?? '—')})]));
}
function renderDataModel(data) {
  const model = data.model || {}, training = data.training || {}, storage = data.storage || {};
  const candidate = training.candidate || {}, offlineModel = model.version ? model : candidate;
  const quality = training.data_quality || {};
  const sampledQuality = quality.sampled === true || quality.counts_scope === 'sampled_qualified_inputs';
  const qualityCards = sampledQuality ? [
    ['全量训练输入',quality.input_records ?? '尚未统计'], ['全量输入比赛',quality.input_matches ?? '尚未统计'],
    ['全量有效训练决策',training.eligible_decisions ?? '尚未审查'], ['全量无效 / 隔离数据',quality.invalid_training_records ?? '尚未审查'],
    ['等待赛果标签（全量）',quality.waiting_for_label ?? '尚未统计'],
    ...(quality.estimated_input_records != null ? [['训练输入（估计）',quality.estimated_input_records]] : []),
    ...(quality.estimated_input_matches != null ? [['输入比赛（估计）',quality.estimated_input_matches]] : []),
    ['已核验样本数',quality.sample_examined_records ?? '尚未审查'], ['已核验样本比赛',quality.sample_examined_matches ?? '尚未审查'],
    ['已核验样本有效决策',quality.sample_valid_training_decisions ?? training.sampled_eligible_decisions ?? '尚未审查'],
    ['已核验样本无效（不可结算）',quality.sample_ungraded_inputs ?? '尚未审查'],
  ] : [
    ['有效训练决策',training.eligible_decisions ?? '尚未审查'], ['已有输入比赛',quality.input_matches ?? '尚未统计'],
    ['持久化训练输入',quality.input_records ?? '尚未统计'], ['无效 / 隔离训练数据',quality.invalid_training_records ?? '尚未审查'],
    ['等待赛果标签',quality.waiting_for_label ?? '尚未统计'],
  ];
  const decisionStatus = decisionModelStatus(data);
  const labels = {not_started:'尚未启动',restoring:'历史数据恢复中',checking:'检查训练数据',waiting:'等待新增已结算比赛',training:'训练中',ready:'已更新',candidate:'候选已训练 · 未晋级',failed:'训练失败',disabled:'自动训练已关闭'};
  $('model-state').textContent = labels[training.state] || '未训练';
  $('model-summary').replaceChildren(...metricCards([
    ['决策模式',decisionModeLabel(decisionStatus.mode)], ['模式状态',decisionStatus.state],
    ['模型影子记录',data.settings?.shadow_enabled === false ? '已关闭' : '已开启 · 独立观察'],
    ...(decisionStatus.mode === 'hybrid' ? [['混合模型权重',pct(data.settings?.model_blend_weight)]] : []),
    ['最新更新时间',timeLabel(offlineModel.updated_at || training.checked_at)], ['训练参数量',offlineModel.parameter_count ?? (candidate.version ? '候选参数暂未提供' : '未训练')],
    ['生产模型版本',model.version || '尚无已晋级模型'],
    ...(candidate.version ? [['候选模型版本',candidate.version],['候选晋级状态',candidate.promotion?.approved === true ? '已通过 · 等待加载' : '未晋级']] : []),
    [sampledQuality ? '样本有效已结算比赛' : '有效已结算比赛',unknownMetric(training.eligible_matches ?? model.eligible_matches)],
    ['本轮训练样本',unknownMetric(training.training_decisions)], ['每场样本上限',unknownMetric(quality.training_max_rows_per_match,'未配置')],
    ...qualityCards,
    ['距更新周期', (training.new_matches ?? '—') + ' / ' + (data.settings?.cycle_matches ?? '—') + ' 场'],
    ['当前实际训练限制',training.limits ? training.limits.cpu_cores + ' 核 / ' + training.limits.memory_mb + ' MiB' : '尚未执行'],
    ['训练资源上限',(data.settings?.cpu_cores ?? '—') + ' 核 / ' + (data.settings?.memory_mb ?? '—') + ' MiB'],
  ]));
  const promotion = model.promotion || data.training?.candidate?.promotion || {};
  const calibration = model.calibration || data.training?.candidate?.calibration;
  const promotionText = promotion.approved === true ? (model.version ? ' 当前模型已通过晋级门槛。' : ' 当前候选已通过晋级校验，尚未加载生产模型。') :
    promotion.approved === false ? ' 当前候选未晋级：' + ((promotion.reasons || []).join('；') || '独立验证尚未满足晋级要求。') :
      model.version ? ' 当前模型的晋级证据暂未提供。' : training.candidate ? ' 当前候选等待晋级校验。' : ' 尚无已晋级模型。';
  $('model-description').textContent = (offlineModel.architecture || (candidate.version ? '候选已训练：L2 Logistic 多盘口概率校准' : '待训练：L2 Logistic 多盘口概率校准')) + '。' + (data.note || '') +
    (decisionStatus.reason ? ' 决策模式不可用：' + decisionStatus.reason : '') +
    ' 首次训练至少需要100场有效已结算比赛。验证按比赛和时间隔离，训练与评价按比赛等权；走盘排除，赢半/输半按非走盘方向判定。' +
    (sampledQuality ? ' 数据资格口径：已核验有效/无效数量仅覆盖本轮按比赛限量抽取的样本，不代表全库资格；全量尚未审查的数量单独标明。标注“估计”的输入量来自数据库统计信息，可能滞后；等待赛果标签不计为无效。' :
      quality.invalid_training_records !== undefined ? ' 数据资格口径：有效数为通过版本、双时间和结算校验的训练决策；无效/隔离数包含旧版本、缺失或越界时间、不可结算输入及不符合新契约的旧台账；等待标签的数据单独统计，不计为无效。' : '') +
    (calibration ? ' 校准：' + (calibration.method === 'logistic' ? 'Logistic slope/intercept' : '恒等映射') + '（' + (calibration.samples || 0) + ' 条样本）。' : ' 尚无独立校准制品。') +
    promotionText;
  const prospective = prospectiveMetricRows(data), shadow = data.shadow || {}, shadowView = shadowStatus(data);
  const livePerformance = shadow.performance || {};
  const latencyP95 = field => livePerformance[field]?.p95 == null ? '尚未统计' : fixed(livePerformance[field].p95,0) + ' ms';
  const prospectiveLabels = {ready:'已有前瞻结算样本',waiting_for_settlement:'等待赛果结算',no_predictions:'等待首条影子预测',unavailable:'前瞻统计尚不可用',error:'前瞻统计读取失败'};
  const performanceCards = [
    ['影子模型',shadowView.text], ['影子观察状态',prospectiveLabels[prospective.status] || '等待统计'],
    ['影子预测已记录',unknownMetric(prospective.prospective.counts?.predictions,'尚未统计')],
    ['前瞻已结样本',unknownMetric(prospective.prospective.counts?.settled_predictions,'尚未统计')],
    ['前瞻待结样本',unknownMetric(prospective.prospective.counts?.pending_predictions,'尚未统计')],
    ['前瞻已结比赛',unknownMetric(prospective.prospective.counts?.settled_matches,'尚未统计')],
    ...(prospective.prospective.sampling ? [
      ['有效已结决策',unknownMetric(prospective.model.decisions,'尚未审查')],
      ['已结窗口上限',String(prospective.prospective.sampling.max_matches ?? '—') + ' 场'],
      ['每场完整合约上限',String(prospective.prospective.sampling.max_contracts_per_match ?? '—') + ' 个'],
    ] : []),
    ['前瞻方向二分类（非推荐命中）',prospective.status === 'ready' ? prospective.metric(prospective.model.binary_accuracy ?? prospective.model.accuracy) : '等待前瞻样本'],
    ['胜平负 Top-1 正确率',prospective.status === 'ready' ? prospective.metric(prospective.model.had_top1_accuracy) : '等待前瞻样本'],
    ['胜平负市场 Top-1',prospective.status === 'ready' ? prospective.metric(prospective.baseline.had_top1_accuracy) : '等待前瞻样本'],
    ['胜平负完整组比赛',unknownMetric(prospective.model.had_top1_matches,'尚未审查')],
    ['亚洲盘选中方向获利率',prospective.status === 'ready' ? prospective.metric(prospective.model.asian_payment_accuracy) : '等待前瞻样本'],
    ['亚洲盘市场方向获利率',prospective.status === 'ready' ? prospective.metric(prospective.baseline.asian_payment_accuracy) : '等待前瞻样本'],
    ['亚洲盘选中合约',unknownMetric(prospective.model.asian_selected_contracts,'尚未审查')],
    ['不可结算影子样本',unknownMetric(prospective.prospective.counts?.ungraded_predictions,'尚未审查')],
    ['前瞻排除样本',unknownMetric(prospective.prospective.counts?.excluded_predictions,'尚未审查')],
    ['本进程跳过已结回放',unknownMetric(shadow.skipped_settled_replay,'尚未统计')],
    ['历史游标隔离回放（非本窗口）',unknownMetric(prospective.prospective.counts?.excluded_replay_predictions,'尚未统计')],
    ['影子计算 P95',latencyP95('compute_ms')],
    ['实时排队 P95',latencyP95('queue_ms')],
    ['决策至影子输出 P95',latencyP95('decision_to_prediction_ms')],
    ['决策至本地可靠保存 P95',latencyP95('decision_to_durable_ms')],
    ['决策至 PG 确认 P95',latencyP95('decision_to_pg_confirmed_ms')],
    ['影子持久化 P95',latencyP95('prediction_persist_ms')],
    ['待同步 PG 批次',unknownMetric(shadow.live?.outbox_batches,'尚未统计')],
    ['待同步 PG 预测',unknownMetric(shadow.live?.outbox_predictions,'尚未统计')],
    ['最旧待同步等待',shadow.live?.outbox_oldest_wait_ms == null ? '尚未统计' : fixed(shadow.live.outbox_oldest_wait_ms,0) + ' ms'],
    ['PG 同步状态',({idle:'已同步',following:'同步正常',pending:'等待同步',persisting:'同步中',error:'同步失败，保留待重试数据'})[shadow.live?.persist_state] || shadow.live?.persist_state || '尚未统计'],
    ['实时延迟样本',unknownMetric(livePerformance.samples,'尚未统计')],
    ['已存历史预测端到端 P95',prospective.prospective.latency?.p95_ms == null ? '尚未统计' : fixed(prospective.prospective.latency.p95_ms,0) + ' ms'],
  ];
  const rows = [[model.version ? '生产模型离线时间验证' : '候选离线时间验证',offlineModel.validation],['离线同组市场基准',offlineModel.baseline],['当前版本前瞻验证',prospective.status === 'ready' ? prospective.model : null],['前瞻市场基准',prospective.status === 'ready' ? prospective.baseline : null]];
  const shadowPanel = el('section',{class:'shadow-performance', 'aria-label':'影子模型前瞻表现'},[
    el('h3',{text:'影子模型前瞻表现'}),el('div',{class:'model-metrics'},metricCards(performanceCards)),
    el('p',{text:(prospective.reason || shadow.reason || (shadowView.available ? '当前影子预测只供观察，不代表候选已晋级。' : shadowView.text)) +
      ' 统计生成于 ' + (timeLabel(prospective.prospective.updated_at) === '—' ? '尚未提供' : timeLabel(prospective.prospective.updated_at)) +
      '；最近预测 ' + (timeLabel(shadow.last_prediction_at) === '—' ? '尚未生成' : timeLabel(shadow.last_prediction_at)) + '。' +
      (prospective.prospective.windows ? ' 统计范围：已结窗口与待结窗口分别维护；已结比赛最多 ' + unknownMetric(prospective.prospective.windows.settled?.max_matches,'未提供上限') + ' 场；待结窗口最多 ' + unknownMetric(prospective.prospective.windows.pending?.max_predictions,'未提供上限') + ' 条预测。' : prospective.prospective.window ? ' 统计范围：当前版本最近 ' + unknownMetric(prospective.prospective.window.max_predictions,'未提供上限') + ' 条预测；' + timeLabel(prospective.prospective.window.from) + ' 至 ' + timeLabel(prospective.prospective.window.to) + '。' : '')}),
    el('p',{text:'方向二分类按每个候选 p≥0.5 判断事件发生，不能当作买入推荐命中率。胜平负 Top-1：在同一时点完整的主胜、平、客胜组中选择概率最高方向；亚洲盘：在完整双向组中选择最高概率方向，以实际支付判定，赢半/输半计半注权重，走盘排除输赢分母，各比赛等权。前瞻只使用模型可用后、结果揭晓前记录的预测；离线验证与前瞻统计分开展示。市场基准使用相同前瞻样本，窗口统计不代表全库正确率。'}),
    el('p',{text:'实时延迟按本进程最近 ' + unknownMetric(livePerformance.sample_limit,'尚未提供上限') + ' 个有效样本计算：计算包含特征构建与模型推理；排队从算法决策生成计时；输出包含排队与计算。本地可靠保存与 PG 确认分别计时，等待同步的数据不会删除，PG 写入失败会重试。已存历史预测端到端延迟保留过去的积压记录。重启恢复记录不参与计算、排队和输出延迟统计；PG 确认延迟包含保留下来的积压与恢复记录；持久化耗时按包含实时预测的合批事务统计，批中也可能包含恢复预测。历史训练输入仍在独立后台完整保存。' +
      ' 延迟统计于 ' + timeLabel(livePerformance.updated_at) + '。'}),
    ...(shadow.live?.persist_error ? [el('p',{class:'warning',text:'PG 同步错误：' + shadow.live.persist_error})] : []),
    ...(prospective.prospective.sampling ? [el('p',{text:prospective.prospective.sampling.note || '已结表现按比赛及完整合约有界抽样，原始预测永久保留；该窗口不代表全量正确率。'})] : []),
  ]);
  $('model-performance').replaceChildren(shadowPanel,el('table',{},[
    el('thead',{},[el('tr',{},['表现口径','比赛 / 决策','二分类准确率','HAD Top-1','Brier ↓','Log loss ↓','ECE ↓'].map(text=>el('th',{text})))]),
    el('tbody',{},rows.map(([label,m])=>el('tr',{},[label,m && Number(m.decisions)>0 ? unknownMetric(m.matches,'尚未统计') + ' / ' + m.decisions : '暂无有效样本',pct(m?.accuracy),pct(m?.had_top1_accuracy),fixed(m?.brier,4),fixed(m?.log_loss,4),fixed(m?.ece,4)].map(text=>el('td',{text}))))),
  ]));
  const folds = model.rolling_validation || data.training?.candidate?.rolling_validation || [];
  const foldPanel = folds.length ? el('div',{class:'model-folds'},[
    el('h3',{text:'滚动时间折'}),
    el('table',{},[el('thead',{},[el('tr',{},['折','训练场次','验证场次','模型 Brier','盘口 Brier','差值'].map(text=>el('th',{text})))]),
      el('tbody',{},folds.map(f=>el('tr',{},[f.fold,f.training_matches,f.validation_matches,f.model?.brier,f.baseline?.brier,f.delta_brier].map(value=>el('td',{text:typeof value === 'number' ? fixed(value,4) : String(value ?? '—')})))))]),
  ]) : null;
  if (foldPanel) $('model-performance').appendChild(foldPanel);
  const storageTimestamp = storage.summary_at || storage.at;
  const storageAt = storageTimestamp && Number.isFinite(new Date(storageTimestamp).getTime()) ? new Date(storageTimestamp) : null;
  const storageStale = storage.stale === true || storage.summary_status === 'stale' || (!storage.summary_status && storageAt && Date.now() - storageAt.getTime() > STORAGE_STALE_MS);
  $('storage-at').textContent = storageAt ? (storageStale ? '上次统计 · ' : '统计于 ') + timeLabel(storageTimestamp) : '尚未生成存储统计';
  $('storage-summary').replaceChildren(...metricCards([
    ['台账决策 / 比赛',unknownMetric(storage.records?.decisions,'尚未统计') + ' / ' + unknownMetric(storage.records?.matches,'尚未统计')], ['已结算决策',unknownMetric(storage.records?.settled_decisions,'尚未统计')], ['本地文件',unknownMetric(storage.files,'尚未统计')],['本地数据',storage.bytes == null ? '尚未统计' : bytes(storage.bytes)],['硬盘容量',bytes(storage.disk?.total)],
    ['硬盘已用',bytes(storage.disk?.used)],['硬盘可用',bytes(storage.disk?.free)],
    ['S3 归档文件', storage.archive?.backend === 'local' ? '未启用' : unknownMetric(storage.archive?.files,'尚未统计')],
    ['S3 原始数据', storage.archive?.bytes == null ? '尚未统计' : bytes(storage.archive.bytes)], ['S3 对象占用', storage.archive?.object_bytes == null ? '尚未统计' : bytes(storage.archive.object_bytes)],
  ]));
  $('storage-categories').replaceChildren(el('table',{},[
    el('thead',{},[el('tr',{},['数据类别','文件数','存储量','实际占用'].map(text=>el('th',{text})))]),
    el('tbody',{},(storage.categories || []).length ? storage.categories.map(c=>el('tr',{},[c.name,String(unknownMetric(c.files,'尚未统计')),c.bytes == null ? '尚未统计' : bytes(c.bytes),c.allocated_bytes == null ? '尚未统计' : bytes(c.allocated_bytes)].map(text=>el('td',{text})))) : [el('tr',{},[el('td',{colspan:'4',text:'分类统计尚未生成，独立后台服务更新后显示。'})])]),
  ]));
  $('storage-description').textContent = storage.retention || '统计由后台更新，刷新页面不会扫描归档或启动训练。';
  if (!storageAt) $('storage-description').textContent += ' 尚未取得存储汇总；请等待后台统计，不代表持久化数据为零。';
  if (storageStale) $('storage-description').textContent += ' 当前统计已过期，保留上次结果；后台服务更新后自动刷新。';
  if (storage.refreshing) $('storage-description').textContent += ' 后台正在更新存储统计。';
  if (storage.reason) $('storage-description').textContent += ' ' + storage.reason;
  if (storage.summary_reason && storage.summary_reason !== storage.reason) $('storage-description').textContent += ' ' + storage.summary_reason;
  if (data.history_repair?.totals) $('storage-description').textContent += ' 历史恢复：已处理 ' + data.history_repair.files + ' 个归档文件，剩余 ' + data.history_repair.remaining_files + ' 个；已重建 ' + (data.history_repair.totals.rebuilt_inputs || 0).toLocaleString() + ' 条训练输入。';
  const error = training.error || storage.archive?.error || (storage.errors || []).join('；');
  notice('data-model-notice',error || (!data.running ? '独立模型服务尚未启动，请启动 model-worker。' : ''),!!error);
}
async function loadDataModel(force = false) {
  if (dataModelBusy || (S.dataModelUnsupported && force !== true)) return;
  dataModelBusy = true;
  $('data-model-refresh').disabled = true;
  try {renderDataModel(await get('/data-model')); S.dataModelUnsupported = false;}
  catch (error) {
    if (error.status === 404) S.dataModelUnsupported = true;
    if ($('model-summary').querySelector('.skeleton')) {
      $('model-state').textContent = error.status === 404 ? '接口待更新' : '读取失败';
      $('model-summary').replaceChildren(el('p',{text:'等待后端返回实际模型信息。'}));
    }
    notice('data-model-notice',error.status === 404 ? '当前后端版本尚未提供数据与模型接口，请构建并更新 analytics-api 后手动刷新。' : error.message + '，请刷新重试。',true);
  }
  finally {dataModelBusy=false; $('data-model-refresh').disabled=false;}
}
function renderBettingStatus(betting) {
  const statusLabels = {accepted:'已接受',pending:'等待场馆确认',rejected:'场馆拒单',unknown:'提交结果未知',blocked:'未提交',duplicate:'重复推荐已跳过'};
  const last = betting?.last_result;
  const order = last?.status ? last : betting?.orders?.[0];
  notice('betting-capability-notice', '真实投注：' + (betting?.reason || '无法读取投注执行状态，请重新读取设置。') +
    (order?.status ? '；最近执行：' + (statusLabels[order.status] || order.status) +
      (order.order_no ? ' · 注单 ' + order.order_no : '') + (order.reason ? ' · ' + order.reason : '') +
      (order.retry_cancelled ? ' · 重试已取消' : '') : '') +
    (betting?.latency?.quote_to_submit_ms?.p95 !== null && betting?.latency?.quote_to_submit_ms?.p95 !== undefined ? '；盘口至提交 P95 ' + fixed(betting.latency.quote_to_submit_ms.p95, 0) + 'ms' : '') +
    (betting?.latency?.target_misses ? '；超过2秒性能目标 ' + betting.latency.target_misses + ' 次（未阻止下单）' : '') +
    (betting?.session_warmup ? '；会话 ' + ({ready:'已预热',warming:'预热中',retrying:'刷新中',disabled:'未启用',not_started:'未启动',unconfigured:'未配置'}[betting.session_warmup] || betting.session_warmup) : '') +
    (betting?.retry_pending ? '；待重试 ' + betting.retry_pending + ' 项' : '') +
    '。仅对满足决策出现次数、最新盘口、限额与余额条件的综合推荐下单；已发出的订单不会因关闭而撤销。');
}
function renderSettings(data) {
  S.settingsVersion = data.version;
  $('settings-version').textContent = 'v' + data.version + ' · ' + (data.persistence_backend === 'postgres' ? '已保存至 PostgreSQL · 重启保留' : data.persistent ? '已持久保存' : '内存配置');
  const cfg = data.settings;
  const betting = data.capabilities?.betting;
  renderBettingStatus(betting);
  const alerts = (data.algorithm_alerts || []).filter(a => a.status === 'warning');
  notice('settings-notice', alerts.length ? alerts.map(a => a.message).join('；') : data.restore_error || '', !!alerts.length);
  const sections = SETTING_FIELDS.map(([title,fields], index) => {
    const section = el('fieldset', {class: 'settings-section', 'data-group':title}, [el('legend', {text: title})]);
    if (index === 0) {
      const algorithms = el('div', {class: 'enabled-algorithms'});
      for (const [key,name] of Object.entries(data.algorithms)) {
        const input = el('input', {type: 'checkbox', name: 'algorithm', value: key, id:'setting-algorithm-' + key});
        input.checked = cfg.algorithms.includes(key);
        algorithms.appendChild(settingControl('algorithm-' + key, name, input, 'algorithm', ALGORITHM_HELP[key]));
      }
      section.appendChild(algorithms);
    }
    for (const [key,label,type,options] of fields) {
      const attrs = {id: 'setting-' + key, name: key};
      let input;
      if (type === 'select') {
        input = el('select', attrs, Object.entries(options).map(([value,text]) => el('option', {value,text})));
        input.value = cfg[key] ?? (key === 'decision_mode' ? 'algorithm' : '');
        if (key === 'decision_mode' && cfg[key] === undefined) input.disabled = true;
      } else {
        input = el('input', {...attrs, type});
        if (type === 'checkbox') input.checked = cfg[key];
        else if (type === 'password') {
          input.autocomplete = 'new-password';
          input.placeholder = cfg.has_llm_key ? '已配置，留空保留' : '未配置';
          input.value = '';
        } else input.value = cfg[key] ?? '';
        if ((key.startsWith('model_training_') || key === 'model_shadow_enabled' || key === 'model_blend_weight') && cfg[key] === undefined) input.disabled = true;
        if (type === 'number') {input.required=true; input.min=options[0]; input.max=options[1]; input.step=options[2];}
      }
      if (key === 'betting_enabled') {
        input.disabled = betting?.execution_supported !== true;
        input.setAttribute('aria-describedby', 'betting-capability-notice');
      }
      section.appendChild(settingControl(key, label, input, type));
    }
    if (fields.some(f => f[0].startsWith('model_training_')) && cfg.model_training_matches === undefined) section.appendChild(el('p',{class:'config-version',text:'当前后端版本没有训练配置，请更新 analytics-api 后重新读取设置。'}));
    if (fields.some(f => f[0] === 'llm_api_key')) section.appendChild(settingControl('clear_llm_key', '清除已保存 API Key', el('input', {type:'checkbox', id:'clear-llm-key'}), 'checkbox'));
    return section;
  });
  $('settings-fields').replaceChildren(...sections);
  selectSetting(...S.settingsPath);
  notice('settings-notice', alerts.length ? alerts.map(a => algorithmLabel(a.algorithm) + '：' + a.message).join('；') : data.restore_error || '', !!alerts.length);
}
async function loadSettings() {
  if (S.settingsBusy) return;
  S.settingsBusy = true;
  $('settings-save').disabled = true; $('settings-reload').disabled = true;
  try {renderSettings(await get('/settings'));}
  catch (error) {notice('settings-notice', error.message, true);}
  finally {S.settingsBusy = false; $('settings-save').disabled = !S.settingsVersion; $('settings-reload').disabled = false;}
}
async function saveSettings(event) {
  event.preventDefault();
  if (S.settingsBusy || !S.settingsVersion) return;
  const invalid = Array.from($('settings-form').querySelectorAll('input,select')).find(n => !n.disabled && !n.checkValidity());
  if (invalid) {
    const name = invalid.closest('fieldset').dataset.group;
    SETTING_TREE.forEach(([,menus],c)=>menus.forEach(([,groups],m)=>groups.forEach(([label],g)=>{if(label===name) selectSetting(c,m,g);})));
    invalid.reportValidity(); return;
  }
  const settings = {algorithms: Array.from(document.querySelectorAll('input[name="algorithm"]:checked')).map(n => n.value)};
  for (const [,fields] of SETTING_FIELDS) for (const [key,,type] of fields) {
    const input = $('setting-' + key);
    if (input.disabled) continue;
    if (type === 'password') {if (input.value) settings[key] = input.value;}
    else settings[key] = type === 'checkbox' ? input.checked : type === 'number' ? Number(input.value) : input.value;
  }
  if ($('clear-llm-key').checked) settings.llm_api_key = '';
  S.settingsBusy = true;
  $('settings-save').disabled = true;
  try {
    const response = await fetch(API + '/settings', {method:'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({version: S.settingsVersion, settings}), signal: AbortSignal.timeout(8000)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '保存失败（' + response.status + '）');
    renderSettings(data);
    notice('settings-notice', 'v' + data.version + ' 已生效');
  } catch (error) {notice('settings-notice', error.name === 'TimeoutError' ? '保存请求超时，结果尚未确认，请重新读取设置确认是否生效。' : error.message, true);}
  finally {S.settingsBusy = false; $('settings-save').disabled = false;}
}
async function loadBettingStatus() {
  if (S.betStatusUnsupported) return;
  try {
    renderBettingStatus(await get('/bet/status'));
  } catch (error) {
    if (error.status === 404) {
      // Older API containers expose the same capability data through /settings.
      S.betStatusUnsupported = true;
      try {
        const settings = await get('/settings');
        renderBettingStatus(settings.capabilities?.betting);
        return;
      } catch (fallbackError) {
        error = fallbackError;
      }
    }
    notice('betting-capability-notice', '无法读取投注执行状态：' + error.message, true);
  }
}
$('settings-form').addEventListener('submit', saveSettings);
$('settings-reload').addEventListener('click', loadSettings);
$('account-summary').addEventListener('click', () => {
  const panel = $('account-popover');
  const open = panel.hidden;
  panel.hidden = !open;
  $('account-summary').setAttribute('aria-expanded', String(open));
});
$('account-close').addEventListener('click', () => {
  $('account-popover').hidden = true;
  $('account-summary').setAttribute('aria-expanded', 'false');
});
$('nav-live').addEventListener('click', () => switchView('live'));
$('nav-history').addEventListener('click', () => switchView('history'));
$('nav-data-model').addEventListener('click', () => switchView('data-model'));
$('data-model-refresh').addEventListener('click', () => loadDataModel(true));
$('nav-settings').addEventListener('click', () => switchView('settings'));
$('refresh').addEventListener('click', loadLive);
$('history-prev').addEventListener('click',()=>{S.historyOffset=Math.max(0,S.historyOffset-100);loadHistory(false);});
$('history-next').addEventListener('click',()=>{if(S.historyNext!==null){S.historyOffset=S.historyNext;loadHistory(false);}});
$('history-refresh').addEventListener('click', loadHistory);
$('history-days').addEventListener('change', loadHistory);
$('history-type').addEventListener('change', loadHistory);
$('history-algorithm').addEventListener('change', loadHistory);
$('history-cohort').addEventListener('change', loadHistory);
$('all-leagues').addEventListener('click', () => {S.league = ''; renderLeagues(); renderMatches();});
$('search').addEventListener('input', event => {S.search = event.target.value; renderMatches();});
for (const button of document.querySelectorAll('[data-type]')) button.addEventListener('click', () => {
  S.type = button.getAttribute('data-type'); S.league = ''; S.request++;
  for (const tab of document.querySelectorAll('[data-type]')) tab.classList.toggle('active', tab === button);
  loadLive();
});
document.addEventListener('keydown', event => {if (event.key === 'Escape') closeDetail();});
window.addEventListener('hashchange', () => {const view = ['recommendations', 'history', 'data-model', 'settings'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'live'; if (S.view !== view) switchView(view);});
async function poll() {
  loadAccount();
  if (S.view === 'recommendations') await loadRecommendations();
  else if (S.view === 'live') await loadLive();
  else if (S.view === 'data-model') await loadDataModel();
  else if (S.view === 'settings') {
    await loadBettingStatus();
  }
  S.timer = setTimeout(poll, POLL_MS);
}
$('nav-recommendations').addEventListener('click', () => switchView('recommendations'));
$('recommendations-refresh').addEventListener('click', () => loadRecommendations(true));
if (['recommendations', 'history', 'data-model', 'settings'].includes(location.hash.slice(1))) switchView(location.hash.slice(1));
poll();
