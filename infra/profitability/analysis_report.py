"""Publish aggregate diagnostics and the evidence gaps visible at the analysis cutoff."""

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys

from .window_evidence import matches_window, summarize


def read_json(path):
    return json.loads(path.read_bytes(), parse_float=Decimal)


def settlement_evidence(root, observed):
    snapshot_path = root / 'state/latest.json'
    try:
        raw = snapshot_path.read_bytes()
        snapshot = json.loads(raw, parse_float=Decimal)
        snapshot_at = datetime.fromisoformat(snapshot['observed_at'])
        age = observed - snapshot_at
        if not timedelta(0) <= age <= timedelta(minutes=11):
            return {'status': 'stale_or_after_cutoff', 'observed_at': snapshot_at}

        def read_as_of(path):
            if path.stat().st_mtime > observed.timestamp():
                return None
            return read_json(path)

        def read_optional(path):
            try:
                return read_as_of(path)
            except (FileNotFoundError, UnicodeDecodeError, json.JSONDecodeError):
                return None

        closed = summarize(root, snapshot, read=read_optional)
        for window in closed['windows']:
            path = root / 'reconciliations' / f"{window['ordinal']}.wallet.json"
            wallet = read_optional(path)
            if matches_window(wallet, 'window_start', 'window_end', window['begin'], window['end']) and window['wallet']:
                measures = ('observed_change', 'settlement_net_from_payouts', 'net_principal_released',
                            'cash_flow_difference', 'absolute_cash_flow_difference')
                window['wallet'].update({key: wallet['comparison'].get(key) for key in measures})
        return {'status': 'ok', 'observed_at': snapshot_at, 'snapshot_sha256': sha256(raw).hexdigest(),
                'closed': closed, 'windows': snapshot['windows'], 'unknown_total': snapshot['unknown_total'],
                'pending_total': snapshot['pending_total']}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return {'status': type(error).__name__}


REPLAY = re.compile(r'(.+)-replay-(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\.json')


def reference_evidence(root, observed):
    latest = {}
    for path in root.glob('*-replay-*.json'):
        match = REPLAY.fullmatch(path.name)
        if match is None:
            continue
        label, stamp = match.groups()
        try:
            at = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if observed - timedelta(days=7) <= at <= observed and (label not in latest or at > latest[label][0]):
            latest[label] = (at, path)

    summaries = []
    for label, (at, path) in sorted(latest.items()):
        entry = {'label': label, 'archived_at': at, 'smoke': '-smoke' in label}
        try:
            raw = path.read_bytes()
            doc = json.loads(raw, parse_float=Decimal)
            preflights = doc['preflights']
            quote_statuses = Counter()
            score_statuses = Counter()
            mapped_timed = score_and_quote = 0
            for row in preflights:
                references = row['references']
                score_status = row.get('score_comparison', {}).get('status')
                if score_status:
                    score_statuses[score_status] += 1
                quote_statuses.update(ref['status'] for ref in references)
                if any(ref['status'] == 'timed_depth' and ref.get('outcome_status') == 'mapped' for ref in references):
                    mapped_timed += 1
                    score_and_quote += score_status == 'observed_score_matches'
            entry.update(status='ok', sha256=sha256(raw).hexdigest(), capture_rounds=doc['capture_rounds'],
                         preflights=len(preflights), source_quote_statuses=dict(quote_statuses),
                         score_statuses=dict(score_statuses), with_mapped_timed_quote=mapped_timed,
                         with_mapped_timed_quote_and_matching_score=score_and_quote)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            entry['status'] = type(error).__name__
        summaries.append(entry)
    return summaries


def value(number):
    return '无' if number is None else str(number)


def ratio(net, stake):
    if net is None or not stake:
        return '无'
    return f'{100 * Decimal(net) / Decimal(stake):.2f}%'


def table_row(*cells):
    return '| ' + ' | '.join(map(value, cells)) + ' |'


def render(report):
    diagnostics = report['diagnostics']

    def section(name):
        return diagnostics.get(name, {}).get('data', {})

    ledger = report['settlement']
    lines = ['# 乐鱼与系统盈利分析', '', f"生产数据库观察时间 `{report['observed_at']}`，时区 UTC。",
             '', '## 结算与钱包', '']
    if ledger['status'] == 'ok':
        verified = ledger['closed']['rolling_four_windows']['verified']
        lines += [f"最近连续四个闭窗已核实盈利 · {'是' if verified else '证据不足或未达标'}。"
                  f"当前待结或待确认 {ledger['pending_total']} 笔，其中未知回执 {ledger['unknown_total']} 笔。", '',
                  '| 窗口起点 UTC | 状态 | 已结 | 回执净额 | 缺费用 | 缺币种 | 回执对账 | 钱包采样 |',
                  '| --- | --- | ---: | ---: | ---: | ---: | --- | ---: |']
        closed = {item['ordinal']: item for item in ledger['closed']['windows']}
        windows = [window for window in ledger['windows'] if window['status'] != 'upcoming']
        for window in windows[-5:]:
            item = closed.get(window['ordinal'], {})
            scan = item.get('reconciliation') or {}
            wallet = item.get('wallet') or {}
            reconciled = ('字段已核实' if scan.get('current_receipt_fields_reconciled')
                          else '订单列表匹配' if scan.get('order_list_matches_current_count') else '待核实')
            lines.append(table_row(window['since'], '闭合' if window['closed'] else '开放', window['settled'],
                                   window['provider_net'], window['missing_fee_field'], window['missing_currency'],
                                   reconciled, wallet.get('samples')))
        lines += ['', '钱包可用余额还包含本金冻结与释放。以下金额覆盖各窗口实际采样区间。']
        for item in ledger['closed']['windows'][-4:]:
            wallet = item['wallet']
            if wallet and wallet.get('observed_change') is not None:
                lines.append(f"- `{item['begin']}` · 可用余额变动 {value(wallet['observed_change'])}，"
                             f"结算派彩减本金 {value(wallet['settlement_net_from_payouts'])}，"
                             f"净释放本金 {value(wallet['net_principal_released'])}，"
                             f"累计绝对差额 {value(wallet['absolute_cash_flow_difference'])}，"
                             f"采样断点 {value(wallet['sampling_gaps'])}。")
    else:
        lines.append(f"结算观测不可用（`{ledger['status']}`）。")

    lines += ['', '## 近 48 小时推荐、审核与成交', '']
    coverage = section('opportunity_coverage')
    active = [r for r in coverage.get('coverage', []) if not r['withdrawn']]
    opportunity = {r['order_status']: r['recommendations'] for r in active if r['all_markets']}
    if opportunity and coverage.get('account_bindings') == 1:
        totals = '，'.join(f'{label} {opportunity.get(key, 0)}' for key, label in (
            ('settled', '已结'), ('accepted', '已受理'), ('cancelled_or_rejected', '取消或拒绝'),
            ('no_automatic_order', '无自动单'), ('reserved', '预留'), ('awaiting_confirmation', '等待确认'),
            ('unclassified_order_state', '状态未分类')) if opportunity.get(key))
        lines.append(f'有效推荐 {sum(opportunity.values())} 项，{totals}。推荐到订单的对应率受来源授权、体育项目和相同意图去重影响。')
        for sport, label in (('football', '足球'), ('basketball', '篮球')):
            rows = [r for r in active if not r['all_markets'] and r['sport'] == sport]
            total = sum(r['recommendations'] for r in rows)
            matched = sum(r['recommendations'] for r in rows if r['order_status'] in {'settled', 'accepted'})
            lines.append(f'{label}有效推荐 {total} 项，其中 {matched} 项关联已受理或已结自动单。')
    timing = section('review_latency').get('timings', [])
    for row in timing:
        if row['stage'] == 'request_to_signal' and row['phase'] in {'live', 'scheduled'}:
            lines.append(f"{('滚球' if row['phase'] == 'live' else '赛前')}审核请求到信号 P90 为 "
                         f"{value(row['p90_seconds'])} 秒，覆盖 {row['signals']} 个信号。")
    pricing = section('execution_quality')
    for row in pricing.get('cohorts', []):
        if row['all_markets']:
            label = '最近 48 小时' if row['cohort'] == 'initial_48h' else '此前 48 小时'
            lines.append(f"{label}按结算时间共 {row['settled']} 笔，回执净额 {value(row['provider_net'])}，"
                         f"回执净额除以本金 {ratio(row['provider_net'], row['stake'])}。同次预检价覆盖 {row['preflight_prices']} 笔，"
                         f"成交低于预检 {row['worse_than_preflight']} 笔，可按三个价格复算 {row['price_bridge_count']} 笔。")
            if row['price_bridge_count']:
                lines.append(f"该窗口可比样本按推荐价复算 {value(row['diagnostic_net_at_recommendation'])}，"
                             f"推荐到预检的价格影响 {value(row['recommendation_to_preflight_delta'])}，"
                             f"预检到成交的价格影响 {value(row['preflight_to_fill_delta'])}。")
    house_prices = next((r for r in (section('review_price_coverage').get('coverage') or []) if r['all_markets']), {})
    overround = house_prices.get('mean_implied_sum')
    if overround is not None:
        lines.append(f"同一上游时点有完整选项 {house_prices['complete_at_same_source_time']} 组，"
                     f"乐鱼赔率倒数和平均超出 1 约 {(overround - 1) * 100:.2f}%。该值描述报价结构。")

    lines += ['', '## 来源、风险与场外参考', '',
              '来源回放用较早的 28 天登记候选规则，评估紧随其后的 7 天。回放的受理截止比数据库观测点早五分钟。'
              '同场、同方向、消息、时效和跨来源诊断沿用归档订单与可观察时间。'
              '回放样本受原策略实际受理的订单限制，金额属于诊断。']
    holdout = section('source_holdout')
    baseline = next((row for row in holdout.get('results', []) if row['phase'] == 'all' and row['rule'] == 'baseline'), {})
    if baseline:
        train = holdout['training_exclusions']
        lines.append(f"前段有净额 {holdout['coverage']['training_with_net']} 笔，"
                     f"后段实际受理 {baseline['opportunities']} 笔，已有净额 {baseline['retained_with_net']} 笔。"
                     f"等额回执收益率 {value(baseline['equal_stake_receipt_pct'])}%。"
                     f"前段符合候选排除条件的来源 {train['sources']} 个，来源与作者组合 {train['source_authors']} 个。")
    elif holdout:
        cover = holdout['coverage']
        lines.append(f"来源回放未生成收益行。前段受理 {cover['training_fills']} 笔，后段 {cover['holdout_fills']} 笔，"
                     f"其中存在资料缺失或无效的受理记录 {cover['invalid_fills']} 笔。")
    fixture = section('fixture_exposure')
    limits = [row for row in fixture.get('cap_sensitivity', []) if row['scope'] == 'fixture']
    if limits:
        lines += ['', '| 同场敞口限制 | 早段回放净额变化 | 晚段回放净额变化 | 晚段被排除但缺净额 |',
                  '| --- | ---: | ---: | ---: |']
        for cap in (1, 2):
            rows = {row['period']: row for row in limits if row['cap'] == cap}
            early, late = rows.get('early', {}), rows.get('late', {})
            lines.append(table_row(f'{cap} 笔', early.get('observed_delta'), late.get('observed_delta'),
                                   late.get('removed_without_net')))
    elif fixture and not fixture.get('comparisons'):
        cover = fixture['coverage']
        lines.append(f"同场敞口回放未生成比较行。样本 {cover['fills_in_period']} 笔，"
                     f"缺有效受理证据 {cover['fills_without_observed_acceptance']} 笔，"
                     f"无效或资料不全 {cover['invalid_fills']} 笔。")
    external = report['references']
    actual = [item for item in external if not item['smoke'] and item['status'] == 'ok']
    lines += ['', f'近七天有 {len(actual)} 个场外正式试点回放归档标签，另有 '
                  f'{sum(item["smoke"] for item in external)} 个烟测标签。']
    for item in actual:
        lines.append(f"- `{item['label']}` · {item['capture_rounds']} 次采样，{item['preflights']} 条乐鱼自然预检，"
                     f"有方向及盘口深度证据的同步报价 {item['with_mapped_timed_quote']} 条，"
                     f"其中同时满足独立比分观测 {item['with_mapped_timed_quote_and_matching_score']} 条。")
    lines += ['', '场外归档衡量参考价覆盖。有效的净收益判断还需同一结算合同、当时可成交数量、总费用和两边实际成交或拒绝回执。',
              '', '## 覆盖与异常', '', '同一目录中的 `report.json` 保存各项统计、SQL 摘要及场外归档状态。']
    lines += [f"- `{name}` · {result.get('error', '已生成')}" for name, result in diagnostics.items()]
    return '\n'.join(lines) + '\n'


def main():
    production, monitor, output = map(Path, sys.argv[1:])
    os.umask(0o077)
    report = read_json(production)
    observed = datetime.fromisoformat(report['observed_at'])
    for result in report['diagnostics'].values():
        if 'json' in result:
            result['data'] = json.loads(result.pop('json'), parse_float=Decimal)
    report['settlement'] = settlement_evidence(monitor, observed)
    report['references'] = reference_evidence(monitor / 'public-orderbook-pilot-20261118', observed)
    # Decimal values retain their original precision as JSON strings.
    (output / 'report.json').write_text(json.dumps(report, default=str, ensure_ascii=False, indent=2) + '\n')
    (output / 'report.md').write_text(render(report))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as error:
        raise SystemExit(type(error).__name__) from None
