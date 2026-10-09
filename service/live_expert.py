"""Bounded, local in-play computation and replay journal.

Data enrichment and optional LLM analysis run outside this service. Probabilities
from a market-fit baseline are explicitly observational until out-of-sample proof.
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from core.live_model import (
    MODEL_VERSION, REGULATION_SECONDS, clock_label, clock_seconds,
    competition_type, fit_share, fit_total, payment, remaining_distribution, valid_score,
)
from core.market_labels import format_market, format_raw_market, normalize_market_name
from core.economics import effective_ev as economics_effective_ev
from core.economics import fractional_kelly, q_fill_model, shrink_probabilities
from core.devig import devig as devig_snapshot
from core.models import OddsSnapshot, SnapshotState, DevigMethod
from service.runtime_settings import RuntimeConfig

QUOTE_MAX_AGE_S = 15.0
STATE_MAX_AGE_S = 90.0
ANCHOR_MAX_AGE_S = 120.0
JOURNAL_INTERVAL_S = 15.0
MAX_RESULTS = 256
MAX_JOURNAL_MB = 32
CHPID = {'1': ('HAD', False), '2': ('OU', False), '4': ('AH', False),
         '17': ('HAD', True), '18': ('OU', True), '19': ('AH', True)}
OUTCOMES = {'1': 'home', '2': 'away', 'X': 'draw', 'x': 'draw', 'Over': 'over', 'over': 'over',
            'Under': 'under', 'under': 'under'}


def percentiles(values: Sequence[float]) -> Dict[str, Optional[float]]:
    ordered = sorted(values)
    def at(p: float) -> Optional[float]:
        return round(ordered[min(len(ordered) - 1, math.ceil(len(ordered) * p) - 1)], 3) if ordered else None
    return {'p50': at(.5), 'p95': at(.95), 'p99': at(.99),
            'max': round(max(ordered), 3) if ordered else None}


class LiveExpertService:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._compute_lock = threading.Lock()
        self._results: Dict[str, Dict[str, Any]] = {}
        self._anchors: Dict[str, Dict[str, Any]] = {}
        self._series: Dict[Tuple[str, str, str, str], deque] = {}
        self._compute_ms: deque = deque(maxlen=2048)
        self._latency_ms: deque = deque(maxlen=2048)
        self._by_type: Dict[str, deque] = {}
        self._journal: deque = deque(maxlen=512)
        self._journal_at: Dict[str, float] = {}
        self._stop = threading.Event()
        self._writer: Optional[threading.Thread] = None
        self.journal_root: Optional[str] = None
        self.errors = 0
        self.superseded = 0
        self.journal_dropped = 0
        self.journal_error = ''
        self.computations = 0
        self.config = RuntimeConfig()
        self.config_version = 1
        self.on_decision: Optional[Callable[[Mapping[str, Any]], int]] = None
        self._decisions: Dict[tuple, Dict[str, Any]] = {}
        self._recorded: set = set()
        self._recording: set = set()
        self.record_error = ''

    def configure(self, config: RuntimeConfig, version: int) -> None:
        with self._compute_lock:
            with self._lock:
                self.config, self.config_version = config, version
                self._anchors.clear()
                self._results.clear()

    def start(self, root: Optional[str]) -> None:
        self.journal_root = root
        if self._writer and self._writer.is_alive():
            return
        self._stop.clear()
        self._writer = threading.Thread(target=self._write_loop, name='live-replay-writer', daemon=True)
        self._writer.start()

    def stop(self) -> None:
        self._stop.set()
        if self._writer:
            self._writer.join(3)

    def _write_loop(self) -> None:
        while not self._stop.wait(1):
            self.flush()
        self.flush()

    def flush(self) -> int:
        self.flush_decisions()
        if not self.journal_root:
            return 0
        with self._lock:
            rows = list(self._journal)
            self._journal.clear()
        if not rows:
            return 0
        try:
            path = Path(self.journal_root) / 'live-replay.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > MAX_JOURNAL_MB * 1024 * 1024:
                path.rename(path.with_name('live-replay.%d.jsonl' % time.time_ns()))
            with path.open('a', encoding='utf-8') as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + '\n')
            self.journal_error = ''
            return len(rows)
        except (OSError, ValueError) as exc:
            self.journal_error = '%s: %s' % (type(exc).__name__, exc)
            self.journal_dropped += len(rows)
            return 0

    def flush_decisions(self) -> int:
        if not self.on_decision:
            return 0
        with self._lock:
            rows = dict(self._decisions)
            self._decisions.clear()
            self._recording.update(rows)
        n = 0
        for key, row in rows.items():
            try:
                n += self.on_decision(row)
                with self._lock:
                    self._recorded.add(key)
                    self._recording.discard(key)
                self.record_error = ''
            except (OSError, ValueError, TypeError):
                self.record_error = '决策留痕失败，等待重试'
                with self._lock:
                    self._recording.discard(key)
                    self._decisions.setdefault(key, row)
        return n

    def compute(self, snapshot: Mapping[str, Any], hub: Any,
                first_event_at: Optional[float] = None) -> Dict[str, Any]:
        started = time.perf_counter()
        with self._compute_lock:
            result = self._calculate(snapshot)
        elapsed = (time.perf_counter() - started) * 1000
        result['compute_ms'] = round(elapsed, 3)
        mid = str(snapshot['match_id'])
        if hub.state_version(mid) != snapshot['version'] or result['config_version'] != self.config_version:
            self.superseded += 1
            return {}  # New state must be recomputed; never publish a mixed version.
        now = time.monotonic()
        latency = (now - (first_event_at or snapshot.get('received_at') or now)) * 1000
        result['event_to_result_ms'] = round(max(0, latency), 3)
        with self._lock:
            if result['config_version'] != self.config_version:
                return {}
            self._results[mid] = result
            if self.on_decision:
                for evaluation in result.get('evaluations', []):
                    for record_kind, items in (('forecast', evaluation['forecasts']),
                                               ('recommendation', evaluation['recommendations'])):
                        for forecast in items:
                            key = (mid, evaluation['algorithm'], forecast['market'],
                                   str(forecast.get('line', '')), record_kind)
                            if key not in self._recorded and key not in self._decisions and key not in self._recording:
                                self._decisions[key] = {**result, 'forecast': forecast, 'record_kind': record_kind,
                                                        'algorithm': evaluation['algorithm']}
            result['recording_status'] = ('queued' if any(k[0] == mid for k in self._decisions)
                                          else 'recorded' if any(k[0] == mid for k in self._recorded) else 'none')
            self._compute_ms.append(elapsed)
            self._latency_ms.append(max(0, latency))
            self._by_type.setdefault(result["competition_type"], deque(maxlen=2048)).append(max(0, latency))
            self.computations += 1
            if len(self._results) > MAX_RESULTS:
                oldest = min(self._results, key=lambda k: self._results[k]['published_at_ms'])
                self._results.pop(oldest, None)
                self._anchors.pop(oldest, None)
                self._journal_at.pop(oldest, None)
                for series_key in list(self._series):
                    if series_key[0] == oldest:
                        self._series.pop(series_key, None)
            if now - self._journal_at.get(mid, 0) >= JOURNAL_INTERVAL_S:
                if len(self._journal) == self._journal.maxlen:
                    self.journal_dropped += 1
                self._journal.append({
                    'at': result['computed_at'], 'match_id': mid,
                    'competition_type': result['competition_type'], 'model_version': MODEL_VERSION,
                    'version': result['version'], 'score': result['score'],
                    'elapsed_s': result['elapsed_s'], 'phase': result['phase'],
                    'reasons': result['reasons'], 'candidates': result['candidates'],
                    'probabilities': result['probabilities'], 'quote_time_ms': result['quote_time_ms'],
                    'schema_version': 1, 'remaining_goals': result['remaining_goals'],
                    'markets': result['markets'], 'events': result['events'],
                    'input_state': {k: snapshot.get(k) for k in
                                    ('info', 'status', 'score_age_s', 'status_age_s',
                                     'finished', 'suspended', 'suspended_ids')},
                    'anchor': dict(self._anchors.get(mid, {})),
                })
                self._journal_at[mid] = now
        return result

    def _calculate(self, snapshot: Mapping[str, Any]) -> Dict[str, Any]:
        now = time.time()
        cfg = self.config
        mid = str(snapshot['match_id'])
        info = dict(snapshot.get('info') or {})
        kind = competition_type(info)
        score = valid_score(snapshot.get('score'))
        half_score = valid_score(snapshot.get('half_score'))
        status = snapshot.get('status') or {}
        phase = str(status.get('mmp') or '')
        elapsed_s = clock_seconds(status.get('mst'), phase)
        reasons: List[str] = []
        if snapshot.get('finished'):
            reasons.append('比赛已结束')
        if snapshot.get('suspended'):
            reasons.append('盘口暂停，等待已核验的新报价')
        if kind != 'real':
            reasons.append('虚拟比赛使用独立统计；本模型仅适用于真实足球' if kind == 'virtual' else '比赛类型尚未核验')
        if score is None:
            reasons.append('缺少即时比分')
        if elapsed_s is None:
            reasons.append('比赛时钟或阶段尚未核验')
        for name, label in (('score_age_s', '比分'), ('status_age_s', '比赛时钟')):
            age = snapshot.get(name)
            if age is None or age > cfg.state_max_age_s:
                reasons.append(label + '数据过期或未接收')
        groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
        newest = 0
        meta = snapshot.get('market_meta') or {}
        for q in snapshot.get('quotes') or []:
            chpid = str(q.chpid)
            if not math.isfinite(q.odds) or q.odds <= 1:
                continue
            family, half = CHPID.get(chpid, ('RAW_' + chpid, False))
            code = family + ('_1H' if half else '')
            known = chpid in CHPID
            oc = OUTCOMES.get(str(q.ot), str(q.ot)) if known else str(q.ot)
            key = (code, str(q.hv or ''))
            group = groups.setdefault(key, {'market': code, 'line': key[1], 'chpid': chpid,
                                             'name': meta.get(chpid, {}).get('name', code),
                                             'outcomes': {}, 'known': known, 'quote_time_ms': 0})
            previous = group['outcomes'].get(oc)
            if previous and previous.ts_ms >= q.ts_ms:
                continue
            group['outcomes'][oc] = q
            group['quote_time_ms'] = max(group['quote_time_ms'], q.ts_ms)
            newest = max(newest, q.ts_ms)
        markets = []
        for key in sorted(groups, key=lambda k: (k[0].startswith('RAW'), '_1H' in k[0], k[0], k[1])):
            group = groups[key]
            outcomes = group['outcomes']
            expected = ('home', 'draw', 'away') if group['market'].startswith('HAD') else (
                ('over', 'under') if group['market'].startswith('OU') else ('home', 'away'))
            complete = group['known'] and all(o in outcomes for o in expected)
            ordered = [o for o in expected if o in outcomes] + [o for o in outcomes if o not in expected]
            inv = [1 / outcomes[o].odds for o in ordered]
            margin = sum(inv) - 1 if complete else None
            fair = self._devig(inv, cfg.devig_method) if complete else [None] * len(inv)
            age = max(q.quote_age_s for q in outcomes.values())
            quotes = []
            for i, oc in enumerate(ordered):
                q = outcomes[oc]
                series_key = (mid, group['market'], group['line'], oc)
                series = self._series.setdefault(series_key, deque(maxlen=40))
                if not series or (q.ts_ms > series[-1][0] and now - series[-1][0] / 1000 >= 1):
                    series.append((q.ts_ms, q.odds))
                drift = (q.odds / series[0][1] - 1) * 100 if len(series) > 1 else 0
                label = (format_market(group['market'], oc, group['line'], home=info.get('home') or '',
                                       away=info.get('away') or '') if group['known'] else
                         format_raw_market(group['market'], oc, group['line'], group['name'],
                                           home=info.get('home') or '', away=info.get('away') or ''))
                probability = fair[i]
                quotes.append({'outcome': oc, 'label': label, 'odds': q.odds,
                               'p_market': round(probability, 6) if probability is not None else None,
                               'trend_pct': round(drift, 3), 'ts_ms': q.ts_ms})
            markets.append({'market': group['market'], 'line': group['line'],
                            'name': group['name'] if group['known'] else normalize_market_name(group['name'], group['market']),
                            'chpid': group['chpid'], 'quotes': quotes, 'complete': complete,
                            'missing_outcomes': [o for o in expected if o not in outcomes] if group['known'] else [],
                            'supported': group['market'].split('_1H')[0] in ('HAD', 'OU', 'AH'),
                            'known': group['known'],
                            'margin': round(margin, 6) if margin is not None else None,
                            'age_s': round(age, 1) if math.isfinite(age) else None,
                            'fresh': age <= cfg.quote_max_age_s, 'quote_time_ms': group['quote_time_ms']})
        if not any(m['fresh'] and m['complete'] for m in markets):
            reasons.append('报价过期或尚无完整盘口')
        if elapsed_s is not None and elapsed_s >= REGULATION_SECONDS:
            reasons.append('补时长度未知，剩余时间模型暂停')
        rates: Optional[Tuple[float, float]] = None
        current_rates: Optional[Tuple[float, float]] = None
        direction_available = False
        if not reasons and score is not None and elapsed_s is not None:
            totals = [m for m in markets if m['market'] == 'OU' and m['fresh'] and m['complete'] and m['margin'] <= .15]
            totals.sort(key=lambda m: abs(m['quotes'][0]['p_market'] - .5))
            total = (fit_total(totals[0]['line'], totals[0]['quotes'][0]['p_market'], sum(score)) if totals else None)
            if total is not None:
                had = next((m for m in markets if m['market'] == 'HAD' and m['fresh'] and m['complete']), None)
                h, a = (fit_share(total, score, had['quotes'][0]['p_market'], had['quotes'][2]['p_market'])
                        if had else (total / 2, total / 2))
                direction_available = had is not None
                current_rates = (h, a)
                anchor = self._anchors.get(mid)
                if (anchor is None or anchor['score'] != score or now - anchor['at'] > cfg.anchor_max_age_s
                        or elapsed_s < anchor['elapsed_s'] or anchor['direction_available'] != direction_available):
                    anchor = {'score': score, 'elapsed_s': elapsed_s, 'h': h, 'a': a, 'at': now,
                              'direction_available': direction_available}
                    self._anchors[mid] = anchor
                fraction = max(0, REGULATION_SECONDS - elapsed_s) / max(1, REGULATION_SECONDS - anchor['elapsed_s'])
                rates = (anchor['h'] * fraction, anchor['a'] * fraction)
            else:
                reasons.append('没有可拟合的全场大小球盘口')
        evaluations: List[Dict[str, Any]] = []
        for algorithm in cfg.algorithms:
            model_rates = current_rates if algorithm == 'poisson_market' else rates
            candidates: List[Dict[str, Any]] = []
            probabilities: Dict[str, float] = {}
            forecasts: List[Dict[str, Any]] = []
            if model_rates and score is not None:
                dist = remaining_distribution(model_rates[0], model_rates[1], score)
                if direction_available:
                    probabilities = {oc: round(payment(dist, 'HAD', oc).win, 6) for oc in ('home', 'draw', 'away')}
                for market in markets:
                    family = market['market']
                    base_family = family.split('_1H')[0]
                    if base_family not in ('HAD', 'OU', 'AH') or not market['fresh'] or not market['complete']:
                        continue
                    if base_family == 'HAD' and not direction_available:
                        continue
                    for quote in market['quotes']:
                        half_market = family.endswith('_1H')
                        settlement_dist = (((half_score[0], half_score[1], 1.0),)
                                           if half_market and half_score is not None else dist)
                        pay = payment(settlement_dist, base_family, quote['outcome'], market['line'])
                        raw_p = pay.effective_probability or 0.0
                        consensus, spread = self._market_consensus(market, cfg.devig_spread_warn_pp)
                        if algorithm == 'devig_consensus' and quote['outcome'] in consensus:
                            raw_p = consensus[quote['outcome']]
                        if algorithm == 'economics_risk_adjusted':
                            prior = quote.get('p_market') or raw_p
                            raw_p = shrink_probabilities((raw_p,), (prior,), 1, k=2.0)[0][0]
                        ev = pay.ev(quote['odds']) if algorithm != 'devig_consensus' else raw_p * quote['odds'] - 1.0
                        q_fill = q_fill_model(ev, 1.0)
                        effective = economics_effective_ev(ev, q_fill, cfg.execution_cost)
                        kelly = max(0.0, fractional_kelly(raw_p, quote['odds'], cfg.fractional_kelly))
                        risk_penalty = 0.0
                        if algorithm == 'economics_risk_adjusted':
                            # 同场盘口共享比赛状态，相关性越高，可执行仓位越保守。
                            risk_penalty = min(0.5, cfg.risk_correlation * kelly)
                            kelly *= max(0.0, 1.0 - cfg.risk_correlation)
                            effective -= risk_penalty
                        if algorithm == 'microstructure_adjusted':
                            risk_penalty = min(0.5, abs(float(quote.get('trend_pct') or 0.0)) / 100.0)
                            raw_p = min(1.0, max(0.0, raw_p - risk_penalty * (1 if quote.get('trend_pct', 0) > 0 else -1)))
                            ev = raw_p * quote['odds'] - 1.0
                            effective = economics_effective_ev(ev, q_fill, cfg.execution_cost)
                        research_only = half_market and half_score is None
                        settlement_basis = 'half_score_required' if half_market else 'full_score'
                        if not market.get('known', False):
                            research_only, settlement_basis = True, 'unknown'
                        estimate = {**quote, 'market': family, 'line': market['line'], 'algorithm': algorithm,
                                    'p_model': round(min(1.0, max(0.0, raw_p)), 6),
                                    'ev': round(ev, 6), 'raw_ev': round(pay.ev(quote['odds']), 6),
                                    'effective_ev': round(effective, 6), 'q_fill': round(q_fill, 6),
                                    'kelly': round(min(cfg.max_total_exposure, kelly), 6),
                                    'risk_penalty': round(risk_penalty, 6),
                                    'devig_spread_pp': round(spread, 6),
                                    'win_stake': round(pay.win, 6), 'loss_stake': round(pay.loss, 6),
                                    'push_stake': round(pay.push, 6), 'tail': round(pay.tail, 6),
                                    'settlement_basis': settlement_basis, 'research_only': research_only,
                                    'decision_status': 'research_only' if research_only else 'settleable'}
                        candidates.append(estimate)
                forecast_groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
                for candidate in candidates:
                    forecast_groups.setdefault((candidate['market'], candidate['line']), []).append(candidate)
                forecasts.extend(max(choices, key=lambda c: c['p_model'])
                                 for choices in forecast_groups.values() if choices)
                candidates.sort(key=lambda c: c['ev'], reverse=True)
            evaluations.append({'algorithm': algorithm, 'probabilities': probabilities,
                                'remaining_goals': list(model_rates) if model_rates else None,
                                'candidates': candidates, 'forecasts': forecasts,
                                'recommendations': [c for c in candidates if not c['research_only']
                                                    and c['p_model'] >= cfg.min_probability
                                                    and c['effective_ev'] >= cfg.min_ev
                                                    and c['tail'] < .001]})
        primary = next(e for e in evaluations if e['algorithm'] == cfg.primary_algorithm)
        candidates = primary['candidates']
        picks = primary['recommendations']
        if not direction_available:
            reasons.append('缺少独赢盘口，球队进球强度无法分别识别')
        reasons.append('市场基准模型，独立预测优势待验证')
        return {
            'match_id': mid, **info, 'competition_type': kind, 'version': snapshot['version'],
            'model_version': MODEL_VERSION, 'config_version': self.config_version,
            'algorithm': cfg.primary_algorithm, 'computed_at': datetime.now(timezone.utc).isoformat(),
            'published_at_ms': int(now * 1000), 'quote_time_ms': newest,
            'score': list(score) if score is not None else None, 'phase': phase,
            'clock': clock_label(status.get('mst'), phase), 'elapsed_s': elapsed_s,
            'finished': bool(snapshot.get('finished')) or phase == '999', 'suspended': bool(snapshot.get('suspended')),
            'markets': markets, 'candidates': candidates, 'probabilities': primary['probabilities'],
            'remaining_goals': primary['remaining_goals'], 'evaluations': evaluations,
            'forecasts': primary['forecasts'],
            'market_coverage': {'total': len(markets), 'complete': sum(m['complete'] for m in markets),
                                'fresh': sum(m['fresh'] for m in markets),
                                'valued': sum(m['complete'] and m['fresh'] and m['supported'] for m in markets)},
            'reasons': list(dict.fromkeys(reasons)),
            'decision': 'recommend' if picks else 'forecast' if primary['forecasts'] else 'observe',
            'picks': picks, 'has_buy': bool(picks),
            'validation_status': 'research', 'llm_used': False,
            'events': list(snapshot.get('events') or [])[-12:],
            'note': '概率是研究基准。以即时比分、比赛时间和当前报价计算；未知事件码未映射为红牌或xG。',
        }

    @staticmethod
    def _devig(inv: Sequence[float], method: str) -> List[float]:
        if method == 'power':
            low, high = .05, 10.0
            for _ in range(40):
                k = (low + high) / 2
                if sum(p ** k for p in inv) > 1:
                    low = k
                else:
                    high = k
            values = [p ** ((low + high) / 2) for p in inv]
        else:
            values = list(inv)
        return [p / sum(values) for p in values]

    @staticmethod
    def _market_consensus(market: Mapping[str, Any], spread_warn_pp: float) -> Tuple[Dict[str, float], float]:
        """用已有五种去水方法计算同一盘口的共识概率与方法分歧。"""
        quotes = list(market.get('quotes') or [])
        if not quotes:
            return {}, 0.0
        outcomes = tuple(str(q.get('outcome', '')) for q in quotes)
        odds = tuple(float(q.get('odds', 0.0)) for q in quotes)
        try:
            snap = OddsSnapshot(match_id='live', league='', home='', away='', market=str(market.get('market', '')),
                                outcomes=outcomes, odds=odds, state=SnapshotState.ACTIVE)
            fair = devig_snapshot(snap, method=DevigMethod.AUTO, spread_warn_pp=spread_warn_pp)
            return dict(zip(outcomes, fair.probabilities)), float(fair.method_spread_pp)
        except (TypeError, ValueError, ZeroDivisionError):
            return {}, 0.0

    def results(self, kind: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            rows = [r for r in self._results.values() if not r['finished'] and
                    (kind in (None, '', 'all') or r['competition_type'] == kind)]
            return {'count': len(rows), 'decisions': list(rows),
                    'summary': {'n': len(rows), 'buy': sum(r['has_buy'] for r in rows),
                                'observe': sum(r['decision'] == 'observe' for r in rows)},
                    'model_version': MODEL_VERSION, 'mode': 'live', 'performance': self.health()}

    def detail(self, mid: str) -> Optional[Dict[str, Any]]:
        with self._compute_lock:
            with self._lock:
                row = self._results.get(mid)
                if row is None:
                    return None
                histories = {"|".join(key[1:]): list(series)
                             for key, series in self._series.items() if key[0] == mid}
                return {**row, 'price_history': histories}

    def health(self) -> Dict[str, Any]:
        with self._lock:
            return {'computations': self.computations, 'compute_ms': percentiles(self._compute_ms),
                    'event_to_result_ms': percentiles(self._latency_ms), 'samples': len(self._compute_ms),
                    'by_type_event_to_result_ms': {k: {'samples': len(v), **percentiles(v)}
                                                 for k, v in self._by_type.items()},
                    'superseded': self.superseded, 'errors': self.errors, 'journal_dropped': self.journal_dropped,
                    'journal_error': self.journal_error, 'journal_pending': len(self._journal),
                    'record_error': self.record_error, 'decisions_pending': len(self._decisions),
                    'config_version': self.config_version,
                    'model_version': MODEL_VERSION, 'validation_status': 'research'}
