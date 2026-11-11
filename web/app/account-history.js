import { api, esc, stamp, signed, color } from './ui.js';

// One upstream refresh per page/user; polling only reads the local snapshot.
export function createAccountHistory() {
  let owner = null, generation = 0, started = false, snapshot = null, controller = null;
  let byFixture = new Map();
  const statusText = {
    guest: '登录后查看本人近 7 日账户记录。',
    unbound: '尚未绑定乐鱼账户，请在个人中心绑定并校验。',
    verification_required: '本人账户尚未通过校验或会话不可用，请在个人中心完成校验。',
    queued: '历史订单已排队，正在后台读取。', running: '正在读取待结算和已结算订单…',
    account_busy: '本人账户正在校验，完成后读取历史订单…',
    failed: '历史订单读取失败，买入状态与盈亏尚未核验。',
    timeout: '历史订单尚未返回，请稍后重新读取；不能据此判断未买入。',
    partial: '历史订单仅部分核验，缺失记录不代表未买入。',
    unavailable: '历史订单服务暂不可用，买入状态与盈亏尚未核验。',
  };
  function matchMarkup(fixtureId) {
    if (!owner) return '';
    const match = byFixture.get(fixtureId);
    if (!match) return `<span class="muted">本场买入：${snapshot?.association_complete ? '近7日无记录' : '未核验'}</span>`;
    const money = (match.currencies || []).map(item => {
      const unit = esc(item.currency || '币种未知');
      const value = item.settled_net == null ? '未提供' : `${signed(item.settled_net)} ${unit}`;
      return `<span class="${color(item.settled_net)}">实盘结算净收益 ${esc(value)}</span>${item.unknown_profit_count ? `<small>另有 ${esc(item.unknown_profit_count)} 单盈亏未提供</small>` : ''}${item.fees_unknown ? '<small>手续费未提供</small>' : ''}`;
    }).join('');
    return `<span>本场已买入 · ${esc(match.order_count)} 单</span><span>待结算 ${esc(match.unsettled_count)} 单 · 已结算 ${esc(match.settled_count)} 单</span>${money || '<span class="muted">实盘净收益：待结算</span>'}`;
  }
  function render() {
    const panel = document.querySelector('#account-history-summary');
    if (panel) {
      const data = snapshot || { status: owner ? 'queued' : 'guest' };
      const totals = data.observed_at
        ? `<span>已核验 ${esc(data.order_count)} 单 / ${esc(data.match_count)} 场 · 待结算 ${esc(data.unsettled_match_count)} 场 · 已结算 ${esc(data.settled_match_count)} 场</span><small>下单窗口 ${stamp(data.window_start)} — ${stamp(data.window_end)}（北京时间）；读取于 ${stamp(data.observed_at)}${data.unmapped_order_count ? `；${esc(data.unmapped_order_count)} 单缺少赛事编号` : ''}</small>` : '';
      const completed = data.status === 'completed';
      panel.innerHTML = `<strong>本人近 7 日实盘记录</strong><span>${esc(completed ? '历史订单已读取' : statusText[data.status] || '账户历史尚未读取')}</span>${totals}<small>按整场比赛关联，不表示买入了相同专家盘口。仅统计真实已结算回执；待结算不计盈亏，不同币种分别展示。</small>${['failed', 'partial', 'timeout', 'unavailable'].includes(data.status) ? '<button class="small-button" data-history-retry>重新读取历史</button>' : ''}`;
      panel.querySelector('[data-history-retry]')?.addEventListener('click', () => {
        started = false;
        load();
      });
    }
    document.querySelectorAll('[data-account-fixture]').forEach(element => {
      element.innerHTML = matchMarkup(element.dataset.accountFixture);
      element.hidden = !owner;
    });
  }
  function identify(userId) {
    userId ||= null;
    if (userId === owner) return;
    owner = userId;
    generation++;
    controller?.abort();
    started = false;
    snapshot = null;
    byFixture = new Map();
    render();
  }
  function binding(account) {
    if (snapshot && ((snapshot.account_id || null) !== (account?.id || null) ||
        snapshot.binding_revision !== account?.binding_revision)) {
      generation++;
      controller?.abort();
      started = false;
      snapshot = null;
      byFixture = new Map();
      render();
    }
  }
  async function load() {
    if (!owner || started) { render(); return; }
    started = true;
    const version = ++generation;
    controller?.abort();
    controller = new AbortController();
    const signal = controller.signal;
    snapshot = { status: 'queued' };
    byFixture = new Map();
    render();
    const deadline = Date.now() + 170000;
    try {
      let result = await api('/accounts/history/refresh', { method: 'POST', body: {}, signal });
      while (version === generation) {
        snapshot = result;
        byFixture = new Map((result.matches || []).map(match => [match.fixture_id, match]));
        render();
        if (!['queued', 'running', 'account_busy'].includes(result.status)) return;
        if (Date.now() >= deadline) {
          snapshot = { ...result, status: 'timeout', association_complete: false };
          render();
          return;
        }
        await new Promise(resolve => setTimeout(resolve, result.status === 'account_busy' ? 3000 : 1500));
        if (version !== generation) return;
        result = result.status === 'account_busy'
          ? await api('/accounts/history/refresh', { method: 'POST', body: {}, signal })
          : await api('/accounts/history', { signal });
      }
    } catch {
      if (version !== generation) return;
      snapshot = { status: 'unavailable', association_complete: false };
      byFixture = new Map();
      render();
    }
  }
  return { identify, load, binding, matchMarkup };
}
