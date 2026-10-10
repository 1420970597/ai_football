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
  const response = await fetch(API + path, {signal: AbortSignal.timeout(8000), cache: 'no-store'});
  if (!response.ok) {
    const error = new Error('读取失败（' + response.status + '）');
    error.status = response.status;
    throw error;
  }
  return response.json();
}
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
  node.title = data.error || ('中心钱包 ' + (data.center_balance == null ? '—' : fixed(data.center_balance)) + ' · 乐鱼体育场馆 YBTY');
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
  try { renderAccount(await get('/account')); }
  catch (error) { renderAccount({available: false, error: error.message}); }
  finally { S.accountBusy = false; }
}
function switchView(view) {
  S.view = view;
  for (const key of ['live', 'recommendations', 'history', 'settings']) {
    $(key + '-view').hidden = key !== view;
    $('nav-' + key).classList.toggle('active', key === view);
    if (key === view) $('nav-' + key).setAttribute('aria-current', 'page');
    else $('nav-' + key).removeAttribute('aria-current');
  }
  location.hash = view;
  if (view === 'recommendations') loadRecommendations();
  else if (view === 'history') loadHistory();
  else if (view === 'settings') loadSettings();
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
      el('span',{text:'命中 ' + fixed(pick.hit_count, 1) + ' · 未中 ' + fixed(pick.miss_count, 1)}),
      el('span',{text:'综合 ' + pct(pick.composite_confidence ?? match.recommendation_confidence)}),
      el('span',{text:'仓位 ' + pct(pick.kelly)})])
  ]));
}

function renderRecommendationCard(item) {
  const match = item.match || item;
  const pick = item.pick || {};
  return el('button', {class:'buy-card', onclick:()=> {
    if (match.match_id) { S.view = 'live'; switchView('live'); selectMatch(match.match_id); }
  }}, [
    el('span',{class:'buy-match',text:(match.home || '主队') + ' · ' + (match.away || '客队')}),
    el('strong',{text:(pick.label || item.label || '盘口') + ' @' + fixed(pick.odds ?? item.odds) + (pick.odds_live === false ? ' · 等待新赔率' : '')}),
    el('div',{class:'buy-metrics'},[
      el('span',{text:'概率 ' + pct(pick.p_model ?? item.p_fused)}),
      el('span',{text:'置信度 ' + pct(pick.confidence ?? item.confidence)}),
      el('span',{text:'命中 ' + fixed(pick.hit_count ?? item.hit_count, 1) + ' · 未中 ' + fixed(pick.miss_count ?? item.miss_count, 1)}),
      el('span',{text:'综合 ' + pct(pick.composite_confidence ?? item.composite_confidence)}),
      el('span',{text:'赔率状态 ' + (item.market_open ? '开启' : '已关闭')})])
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

async function loadRecommendations() {
  const request = ++S.recommendationsRequest;
  $('recommendations-refresh').disabled = true;
  try {
    const data = await get('/recommendations?type=' + encodeURIComponent(S.type));
    if (request !== S.recommendationsRequest || S.view !== 'recommendations') return;
    renderRecommendationsPage(data);
    notice('recommendations-notice', data.source_age_s != null ? '实时盘口更新于 ' + fixed(data.source_age_s, 1) + 's 前' : '');
  } catch (error) {
    if (request === S.recommendationsRequest) notice('recommendations-notice', error.message + '，可点击刷新重试。', true);
  } finally {
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
    const p95 = (data.performance?.by_type_event_to_result_ms?.[type] || data.performance?.event_to_result_ms)?.p95;
    $('algorithm-latency').textContent = p95 !== null && p95 !== undefined ? fixed(p95, 0) + ' ms' : '—';
    notice('notice', online ? (S.matches.some(m => m.stale) ? '部分报价过期' : '') : '采集断开 · 显示缓存');
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
async function loadHistory(reset = true) {
  if (reset !== false) S.historyOffset = 0;
  const request = ++S.historyRequest;
  $('history-refresh').disabled = true;
  try {
    const data = await get('/ledger/history?limit=100&offset=' + S.historyOffset + '&days=' + encodeURIComponent($('history-days').value) + '&type=' + encodeURIComponent($('history-type').value) + '&algorithm=' + encodeURIComponent($('history-algorithm').value) + '&cohort=' + encodeURIComponent($('history-cohort').value));
    if (request !== S.historyRequest) return;
    const summary = data.overall || {};
    const pendingEntries = Number(summary.pending || 0);
    const metrics = [['全盘口正确率', pct(summary.accuracy ?? summary.hit_rate)], ['有效样本 / 场次', String(summary.accuracy_samples ?? ((summary.won || 0) + (summary.lost || 0))) + ' / ' + (data.settled?.matches || 0)], ['等额 ROI', pct(summary.roi)], ['置信度 ROI', pct(summary.confidence_roi)], ['仓位 ROI', pct(summary.allocated_roi)], ['待结算盘口', String(pendingEntries)]];
    $('history-stats').replaceChildren(...metrics.map(([label, value]) => el('div', {}, [el('span', {text: label}), el('strong', {text: value})])));
    const portfolio = data.portfolio_summary || {};
    $('history-portfolio').replaceChildren(...[['正确率',pct(portfolio.accuracy)],['有效样本',String(portfolio.accuracy_samples || 0)],['等额 ROI',pct(portfolio.roi)],['置信度 ROI',pct(portfolio.confidence_roi)],['仓位 ROI',pct(portfolio.allocated_roi)],['净收益',fixed(portfolio.profit_units) + 'u']].map(([label,value])=>el('div',{},[el('span',{text:label}),el('strong',{text:value})])));
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
    $('history-count').textContent = (data.entries_total || 0) + ' 条 · ' + (summary.matches || 0) + ' 场 · 待结算 ' + pendingEntries + ' 个盘口';
    const warning = data.legacy_identity_rows ? '旧记录 ' + data.legacy_identity_rows + ' 条，身份不完整。' : '';
    notice('history-notice', warning + (summary.graded ? '' : '待确认终场赛果 · ' + (data.settle?.evidence_missing ?? summary.pending ?? 0) + ' 场未取得证据'));
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
  } catch (error) {
    if (request === S.historyRequest) notice('history-notice', error.message + '，可刷新重试。', true);
  } finally {
    if (request === S.historyRequest) $('history-refresh').disabled = false;
  }
}
const SETTING_FIELDS = [
  ['算法与门槛', [
    ['primary_algorithm', '强度参考算法', 'select', {poisson_market: '当前盘口 Poisson', poisson_time_decay: '时间衰减 Poisson', devig_consensus: '去水共识', economics_risk_adjusted: '经济学风控', microstructure_adjusted: '盘口微观结构'}],
    ['devig_method', '去水方法', 'select', {proportional: '比例法', power: 'Power 法'}],
    ['min_probability', '建议最低概率', 'number', [0,1,.01]],
    ['min_ev', '建议最低 EV', 'number', [0,1,.01]],
    ['anchor_max_age_s', '衰减锚点重置 / 秒', 'number', [5,600,1]],
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
    ['betting_min_hit_count', '最小历史盘口命中次数', 'number', [1,100000,1]],
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
function renderBettingStatus(betting) {
  const statusLabels = {accepted:'已接受',pending:'等待场馆确认',rejected:'场馆拒单',unknown:'提交结果未知',blocked:'未提交',duplicate:'重复推荐已跳过'};
  const last = betting?.last_result;
  const order = last?.status ? last : betting?.orders?.[0];
  notice('betting-capability-notice', '真实投注：' + (betting?.reason || '无法读取投注执行状态，请重新读取设置。') +
    (order?.status ? '；最近执行：' + (statusLabels[order.status] || order.status) +
      (order.order_no ? ' · 注单 ' + order.order_no : '') + (order.reason ? ' · ' + order.reason : '') : '') +
    '。仅对满足命中次数、最新盘口、限额与余额条件的综合推荐下单；已发出的订单不会因关闭而撤销。');
}
function renderSettings(data) {
  S.settingsVersion = data.version;
  $('settings-version').textContent = 'v' + data.version + ' · ' + (data.persistent ? '已持久保存' : '内存配置');
  const cfg = data.settings;
  const betting = data.capabilities?.betting;
  renderBettingStatus(betting);
  const alerts = (data.algorithm_alerts || []).filter(a => a.status === 'warning');
  notice('settings-notice', alerts.length ? alerts.map(a => a.message).join('；') : data.restore_error || '', !!alerts.length);
  const sections = SETTING_FIELDS.map(([title,fields], index) => {
    const section = el('fieldset', {class: 'settings-section'}, [el('legend', {text: title})]);
    if (index === 0) {
      const algorithms = el('div', {class: 'enabled-algorithms'});
      for (const [key,name] of Object.entries(data.algorithms)) {
        const input = el('input', {type: 'checkbox', name: 'algorithm', value: key});
        input.checked = cfg.algorithms.includes(key);
        algorithms.appendChild(el('label', {}, [input, name]));
      }
      section.appendChild(algorithms);
    }
    for (const [key,label,type,options] of fields) {
      const attrs = {id: 'setting-' + key, name: key};
      let input;
      if (type === 'select') {
        input = el('select', attrs, Object.entries(options).map(([value,text]) => el('option', {value,text})));
        input.value = cfg[key];
      } else {
        input = el('input', {...attrs, type});
        if (type === 'checkbox') input.checked = cfg[key];
        else if (type === 'password') {
          input.autocomplete = 'new-password';
          input.placeholder = cfg.has_llm_key ? '已配置，留空保留' : '未配置';
          input.value = '';
        } else input.value = cfg[key];
        if (type === 'number') {input.min=options[0]; input.max=options[1]; input.step=options[2];}
      }
      if (key === 'betting_enabled') {
        input.disabled = betting?.execution_supported !== true;
        input.setAttribute('aria-describedby', 'betting-capability-notice');
      }
      section.appendChild(el('label', {class: type === 'checkbox' ? 'setting-toggle' : 'setting-field'}, [el('span', {text: label}), input]));
    }
    if (index === 2) section.appendChild(el('label', {class: 'setting-toggle'}, [
      el('span', {text: '清除已保存 API Key'}), el('input', {type: 'checkbox', id: 'clear-llm-key'})]));
    return section;
  });
  $('settings-fields').replaceChildren(...sections);
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
  } catch (error) {notice('settings-notice', error.message, true);}
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
window.addEventListener('hashchange', () => {const view = ['recommendations', 'history', 'settings'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'live'; if (S.view !== view) switchView(view);});
async function poll() {
  loadAccount();
  if (S.view === 'recommendations') await loadRecommendations();
  else if (S.view === 'live') await loadLive();
  else if (S.view === 'settings') {
    await loadBettingStatus();
  }
  S.timer = setTimeout(poll, POLL_MS);
}
$('nav-recommendations').addEventListener('click', () => switchView('recommendations'));
if (['recommendations', 'history', 'settings'].includes(location.hash.slice(1))) switchView(location.hash.slice(1));
poll();
