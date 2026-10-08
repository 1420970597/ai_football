"use strict";
const $ = id => document.getElementById(id);
const S = {view: 'live', type: 'real', league: '', search: '', matches: [], selected: null,
  request: 0, detailRequest: 0, historyRequest: 0, busy: false, loaded: false, timer: null};
const API = '/api/v1';
const POLL_MS = 1000;
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
  if (!response.ok) throw new Error('读取失败（' + response.status + '）');
  return response.json();
}
function switchView(view) {
  S.view = view;
  for (const key of ['live', 'history']) {
    $(key + '-view').hidden = key !== view;
    $('nav-' + key).classList.toggle('active', key === view);
    if (key === view) $('nav-' + key).setAttribute('aria-current', 'page');
    else $('nav-' + key).removeAttribute('aria-current');
  }
  location.hash = view;
  if (view === 'history') loadHistory();
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
  const row = el('div', {class: 'market-row' + (market.quotes.length === 2 ? ' two' : '')},
    [el('span', {class: 'market-name', text: prefix + name + (market.line ? '\n' + market.line : '')})]);
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
    for (const market of match.markets || []) card.appendChild(marketRow(market, match));
    card.appendChild(el('div', {class: 'match-bottom'}, [el('span', {class: 'observation', text: match.suspended ? '暂停观察' : '研究观察'}),
      el('button', {class: 'analysis-link', text: '赛况与算法依据 →', onclick: () => selectMatch(match.match_id)})]));
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
    $('match-count').previousElementSibling.textContent = type === 'real' ? '进行中的真实足球' : type === 'virtual' ? '虚拟 / EAFC' : '全部实时赛事';
    const quoteAges = S.matches.map(m => m.quote_age_s).filter(Number.isFinite);
    $('quote-freshness').textContent = quoteAges.length ? fixed(Math.min(...quoteAges), 1) + 's 前' : '—';
    const p95 = (data.performance?.by_type_event_to_result_ms?.[type] || data.performance?.event_to_result_ms)?.p95;
    $('algorithm-latency').textContent = p95 !== null && p95 !== undefined ? fixed(p95, 0) + ' ms' : '—';
    notice('notice', online ? (S.matches.some(m => m.stale) ? '部分赛事报价较旧；查看详情时请留意数据时间。' : '') : '采集连接暂不可用，当前显示已保存数据。恢复连接后将自动更新。');
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
    el('h2', {text: '把目光放到一场比赛'}), el('p', {text: '选择赛事，查看完整盘口、概率与赛况依据。'})]));
  renderMatches();
}
function detailSection(title) {
  return el('section', {class: 'detail-section'}, [el('h3', {text: title})]);
}
function priceChart(points) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 300 105'); svg.setAttribute('class', 'chart');
  svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', '已接收报价的时间走势');
  const vals = points.map(p => Number(p[1]));
  const low = Math.min(...vals), high = Math.max(...vals);
  const start = Number(points[0][0]), end = Number(points.at(-1)[0]);
  const coordinates = points.map(p => (10 + 280 * (Number(p[0]) - start) / Math.max(1, end - start)).toFixed(1) + ',' +
    (85 - 65 * (Number(p[1]) - low) / Math.max(.02, high - low)).toFixed(1));
  const line = document.createElementNS(svg.namespaceURI, 'polyline');
  line.setAttribute('points', coordinates.join(' ')); line.setAttribute('fill', 'none');
  line.setAttribute('stroke', '#1c88ff'); line.setAttribute('stroke-width', '2');
  svg.appendChild(line);
  return svg;
}
function renderDetail(match) {
  const panel = $('detail');
  const scroll = panel.scrollTop;
  const header = el('div', {class: 'detail-header'}, [el('div', {class: 'detail-top'}, [el('span', {text: match.league || '比赛详情'}),
    el('button', {class: 'close-detail', text: '关闭 ×', onclick: closeDetail})]),
    el('h2', {text: (match.home || '主队') + ' vs ' + (match.away || '客队'), title: (match.home || '') + ' vs ' + (match.away || '')}),
    el('span', {class: 'observation', text: (match.score ? match.score.join(' : ') : '— : —') + ' · ' + match.clock})]);
  const evidence = detailSection('决策依据');
  if (match.stale) evidence.appendChild(el('p', {class: 'evidence', text: '当前为已保存数据，等待报价恢复。'}));
  for (const reason of match.reasons || []) evidence.appendChild(el('p', {class: 'evidence', text: reason}));
  const probability = detailSection('胜平负概率 · 研究基准');
  for (const [oc, value] of Object.entries(match.probabilities || {})) {
    const bar = el('span'); bar.style.width = Math.max(0, Math.min(100, Number(value) * 100)) + '%';
    probability.appendChild(el('div', {class: 'probability-row'}, [el('span', {text: {home: '主胜', draw: '平局', away: '客胜'}[oc] || oc}),
      el('div', {class: 'probability-bar'}, [bar]), el('span', {text: pct(value)})]));
  }
  if (!Object.keys(match.probabilities || {}).length) probability.appendChild(el('p', {class: 'chart-note', text: '赛况或盘口不足，暂不生成方向概率。'}));
  const trend = detailSection('盘口走势');
  const series = Object.entries(match.price_history || {}).find(([key, points]) => key.startsWith('OU|') && points.length > 1) ||
    Object.entries(match.price_history || {}).find(([, points]) => points.length > 1);
  if (series) {
    trend.appendChild(el('p', {class: 'chart-note', text: series[0].replaceAll('|', ' · ')}));
    trend.appendChild(priceChart(series[1]));
    trend.appendChild(el('p', {class: 'chart-note', text: '时间向右 · ' + series[1].length + ' 个已接收报价点'}));
  } else trend.appendChild(el('p', {class: 'chart-note', text: '等待至少两个报价点形成走势。'}));
  const candidates = detailSection('盘口定价与期望收益');
  candidates.appendChild(el('p', {class: 'chart-note', text: '每单位本金的模型期望；含走水与半注。观察信号不代表可信买入建议。'}));
  for (const candidate of match.candidates || []) candidates.appendChild(el('div', {class: 'candidate'}, [
    el('span', {text: candidate.label}), el('strong', {text: 'EV ' + pct(candidate.ev)})]));
  if (!(match.candidates || []).length) candidates.appendChild(el('p', {class: 'chart-note', text: '输入未核验，保留当前报价供观察。'}));
  const markets = detailSection('完整盘口'); markets.classList.add('detail-markets');
  for (const market of match.markets || []) markets.appendChild(marketRow(market, match));
  const context = detailSection('赛况与数据时间');
  context.appendChild(el('p', {class: 'chart-note', text: '报价 ' + fixed(match.quote_age_s, 1) + 's 前 · 计算 ' + fixed(match.result_age_s, 1) + 's 前 · 耗时 ' + fixed(match.compute_ms, 1) + 'ms'}));
  const events = match.events || [];
  context.appendChild(el('p', {class: 'chart-note', text: events.length ? '已接收 ' + events.length + ' 条近期状态事件。未知事件类型不会被解释成红牌或射门。' : '详细场上事件尚未提供。'}));
  panel.replaceChildren(header, evidence, probability, trend, candidates, markets, context);
  panel.scrollTop = scroll;
}
async function loadHistory() {
  const request = ++S.historyRequest;
  $('history-refresh').disabled = true;
  try {
    const data = await get('/ledger/history?limit=300&days=' + encodeURIComponent($('history-days').value) + '&type=' + encodeURIComponent($('history-type').value));
    if (request !== S.historyRequest) return;
    const summary = data.overall || {};
    const metrics = [['半注命中率', pct(summary.hit_rate)], ['模拟收益率 ROI', pct(summary.roi)],
      ['已结算建议', String(summary.graded || 0)], ['待确认赛果', String(summary.pending || 0)]];
    $('history-stats').replaceChildren(...metrics.map(([label, value]) => el('div', {}, [el('span', {text: label}), el('strong', {text: value})])));
    $('history-count').textContent = (data.entries_total || 0) + ' 条 · ' + (summary.matches || 0) + ' 场';
    const warning = data.legacy_identity_rows ? '旧格式有 ' + data.legacy_identity_rows + ' 条记录缺少独立建议身份；被历史覆盖的建议无法恢复。' : '';
    notice('history-notice', warning + (summary.graded ? '' : ' 尚无已确认结算样本，暂不能评估模型正确率。'));
    const statusLabel = {won: '赢', lost: '输', half_won: '赢半', half_lost: '输半', push: '走水', pending: '待结算', void: '无法结算'};
    const rows = (data.entries || []).map(entry => {
      const date = new Date(entry.at);
      const time = Number.isNaN(date.getTime()) ? entry.at : date.toLocaleString('zh-CN', {hour12: false});
      const kind = entry.pnl > 0 ? 'history-win' : entry.pnl < 0 ? 'history-loss' : '';
      return el('tr', {}, [el('td', {text: time}, [el('small', {text: entry.competition_type === 'virtual' ? '虚拟比赛' : entry.competition_type === 'real' ? '真实足球' : '旧记录 / 类型未核验'})]),
        el('td', {text: (entry.home || '主队') + ' vs ' + (entry.away || '客队')}, [el('small', {text: entry.label || entry.market})]),
        el('td', {text: fixed(entry.odds)}), el('td', {text: entry.ft_score ? entry.ft_score.join(' : ') : '—'}),
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
$('nav-live').addEventListener('click', () => switchView('live'));
$('nav-history').addEventListener('click', () => switchView('history'));
$('refresh').addEventListener('click', loadLive);
$('history-refresh').addEventListener('click', loadHistory);
$('history-days').addEventListener('change', loadHistory);
$('history-type').addEventListener('change', loadHistory);
$('all-leagues').addEventListener('click', () => {S.league = ''; renderLeagues(); renderMatches();});
$('search').addEventListener('input', event => {S.search = event.target.value; renderMatches();});
for (const button of document.querySelectorAll('[data-type]')) button.addEventListener('click', () => {
  S.type = button.getAttribute('data-type'); S.league = ''; S.request++;
  for (const tab of document.querySelectorAll('[data-type]')) tab.classList.toggle('active', tab === button);
  loadLive();
});
document.addEventListener('keydown', event => {if (event.key === 'Escape') closeDetail();});
window.addEventListener('hashchange', () => {const view = location.hash === '#history' ? 'history' : 'live'; if (S.view !== view) switchView(view);});
async function poll() {await loadLive(); S.timer = setTimeout(poll, POLL_MS);}
if (location.hash === '#history') switchView('history');
poll();
