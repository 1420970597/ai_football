#!/usr/bin/env python3
"""Offline load measurement; fake venue only, never reads account credentials."""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

from collector.leyu_realtime import PriceTick, RealtimeHub, parse_c105
from service.betting import BettingExecutor
from service.analysis import AnalysisConfig, AnalysisService
from service.live_expert import LiveExpertService, percentiles
from service.runtime_settings import ALGORITHMS, RuntimeConfig, RuntimeSettings
from store.history import HistoryJournal

DEFAULT_MATCHES = 24
RAW_QUOTES = 200
FAKE_PREFLIGHT_S = .08
FAKE_RECEIPT_S = .04
ROUNDS = 3


class FakeVenue:
    def prepare_bet(self, detail, stake):
        time.sleep(FAKE_PREFLIGHT_S)
        return detail

    def submit_bet(self, payload):
        time.sleep(FAKE_RECEIPT_S)
        return {'status': 'accepted', 'submitted': True, 'order_no': 'SIMULATED'}


def run(matches: int) -> dict:
    hub = RealtimeHub(MagicMock(), resume=False)
    config = replace(RuntimeConfig(), betting_enabled=True, min_ev=0, min_probability=0,
                     algorithms=tuple(ALGORITHMS), primary_algorithm='poisson_time_decay')
    live = LiveExpertService()
    live.configure(config, 1)
    ledger = MagicMock()
    ledger.recommendation_performance.return_value = {'hit_count': 20, 'settled_samples': 30}
    result_latency, submissions, stages = [], [], []
    with tempfile.TemporaryDirectory() as root:
        live.start(str(Path(root)/'ledger'))
        executor = BettingExecutor(RuntimeSettings(config), lambda: hub, lambda: ledger,
                                   lambda: FakeVenue(), on_persisted=live.wait_replay)
        executor.bind(str(Path(root)/'ledger'))
        orchestrator = AnalysisService(MagicMock(), realtime=hub, config=AnalysisConfig(
            use_llm=False, ledger_root=str(Path(root)/'ledger')))
        orchestrator.live_expert = live
        orchestrator.betting = executor
        orchestrator.start_scheduler()
        try:
            for round_no in range(ROUNDS):
                mids = [f'sim-{round_no}-{i}' for i in range(matches)]
                for mid in mids:
                    now = time.monotonic()
                    hub._info[mid] = {'match_id': mid, 'home': '甲', 'away': '乙',
                                      'league': '真实足球联赛', 'sport_id': '1'}
                    hub._scores[mid] = (1, 0)
                    hub._score_at[mid] = hub._status_at[mid] = now
                    hub._status[mid] = {'mmp': '7', 'mst': '3600'}
                for mid in mids:
                    now_ms = int(time.time()*1000)
                    ticks = [PriceTick(mid, '1', '1', '', oc, oc, odds, odds, now_ms)
                             for oc, odds in (('1', 1.4), ('X', 4.0), ('2', 8.0))]
                    ticks += [PriceTick(mid, '900', '900', str(i), str(i), str(i), 1.9, 1.9, now_ms)
                              for i in range(RAW_QUOTES)]
                    ticks += parse_c105({'mid': mid, 'time': now_ms, 'hls2': {'2': [{
                        'chpid': '2', 'hpid': '2', 'hid': 'market1', 'hv': '3.25',
                        'ol': [{'oid': 'Over', 'ot': 'Over', 'ov': '1000000'},
                               {'oid': 'Under', 'ot': 'Under', 'ov': '198000'}]}]}})
                    hub._record_ticks(ticks)
                before = live.computations
                orchestrator.notify_price_change(mids)
                until = time.monotonic()+10
                while time.monotonic() < until:
                    with executor._lock:
                        empty = not executor._queue
                    if live.computations-before >= len(mids) and empty and not executor._run_lock.locked():
                        break
                    time.sleep(.02)
                for mid in mids:
                    row = live._results.get(mid)
                    if row:
                        result_latency.append(row['input_to_result_ms'])
                with executor._db(executor.path) as db:
                    all_orders = [json.loads(r[0]) for r in db.execute('SELECT payload FROM orders')]
                submissions = all_orders
                stages += [r.get('timings_ms', {}) for r in all_orders if r['match_id'] in mids]
        finally:
            orchestrator.stop_scheduler()
        health = executor.health()['latency']
        accepted = [r for r in submissions if r['status'] == 'accepted']
        outcome = {'mode': 'offline_fake_venue', 'matches': matches, 'rounds': ROUNDS,
                   'scheduler': 'production_async_shards', 'algorithms': list(config.algorithms), 'raw_quotes_per_match': RAW_QUOTES, 'preflight_s': FAKE_PREFLIGHT_S,
                   'receipt_s': FAKE_RECEIPT_S, 'decisions': len(result_latency),
                   'decision_ms': percentiles(result_latency), 'accepted': len(accepted),
                   'submission_ms': percentiles([r['quote_to_submit_ms'] for r in accepted]),
                   'receipt_ms': percentiles([r['quote_to_receipt_ms'] for r in accepted]),
                   'deadline_blocked': health['deadline_blocked'],
                   'within_budget': health['within_budget'],
                   'stages_ms': {key: percentiles([v[key] for v in stages if key in v])
                                 for key in {k for row in stages for k in row}},
                   'history': HistoryJournal(Path(root)/'ledger/live-replay.jsonl.gz').health()['retention']}
        return outcome


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--matches', type=int, default=DEFAULT_MATCHES)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if not 1 <= args.matches <= 128:
        parser.error('matches must be between 1 and 128')
    result = run(args.matches)
    encoded = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded+'\n')
    print(encoded)
