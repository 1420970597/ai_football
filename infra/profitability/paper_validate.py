"""Evaluate a predeclared paper strategy on captured quotes and versioned results."""

import argparse
from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
import re

from app.order_price import offer_document
from execution_scope import NATIVE_SELECTION_CODES, SPORT_IDS, native_outcome_codes
from preflight_price_decision import evaluate
from reference_preflight import archive_records, compare

from infra.profitability.paper_settle import SLIPPAGE_PROFIT_HAIRCUT, frames_digest
from infra.profitability.simulate_strategies import Fill, Receipt, decimal, summarize, utc


REFERENCE_AGE = 15
ONE_MINUTE = timedelta(minutes=1)
PRIMARY_ARM = 'independent_price_after_slippage'
MINIMUM_NET_EDGE = Decimal('0.02')


def validate_offer(row, frame, cutoff):
    checked = utc(row['native_quote_checked_at'])
    observed = utc(frame['observed_at'])
    created = utc(row['created_at'])
    if not (utc(frame['begin']) <= created < utc(frame['end']) <= observed <= checked <= cutoff
            and checked - observed <= timedelta(seconds=REFERENCE_AGE)):
        raise ValueError('native offer falls outside its captured slot')
    offered = decimal(row['native_selected_price'])
    amount = decimal(row['paper_amount_passing_native_limits'])
    prices = {key: decimal(value) for key, value in row['native_prices'].items()}
    selected = NATIVE_SELECTION_CODES.get(row['selection'])
    sports_id = SPORT_IDS.get(row['sport'])
    if (offered is None or offered <= 1 or amount is None or amount <= 0 or not sports_id
            or prices.keys() != native_outcome_codes(row['market_kind'], sports_id)
            or prices.get(selected) != offered or any(value is None or value <= 1 for value in prices.values())):
        raise ValueError('native limits or complete quoted market unavailable')
    score = row['score_baseline']
    signal = row | {'current_kind': row['market_kind'], 'home_score': score.get('home'), 'away_score': score.get('away')}
    stressed_odds = 1 + (offered - 1) * (1 - SLIPPAGE_PROFIT_HAIRCUT)
    # Restrict both source and receive times to the native check. A later
    # archive round cannot provide quotes for a paper decision already made.
    return offer_document(signal, stressed_odds, checked, checked)


def price_decisions(offers, root, fixtures):
    groups = defaultdict(list)
    decisions = {}
    evidence = {}
    for signal_id, offer in offers.items():
        groups[offer['fixture_id']].append((signal_id, offer))
    for fixture_id, entries in groups.items():
        fixture = fixtures.get(fixture_id)
        if fixture is None:
            decisions.update((key, {'reasons': ['reference_contract_missing']}) for key, _ in entries)
            continue
        label = fixture['label']
        if not re.fullmatch(r'[a-z0-9-]{1,160}', label):
            raise ValueError('invalid reference archive label')
        stats = dict(rounds=0, interrupted=0)
        books = archive_records(sorted((root / 'books').glob(f'{label}-*.jsonl')), stats)
        scores = archive_records(sorted((root / 'scores').glob(f'{label}-*.jsonl')), stats)
        rows = compare([offer for _, offer in entries], fixture['bindings'], books, REFERENCE_AGE,
                       fixture['outcomes'], score_records=scores, score_bindings=fixture.get('score_bindings'))
        for (signal_id, _), row in zip(entries, rows, strict=True):
            decisions[signal_id] = evaluate(row, fixture['approvals'], REFERENCE_AGE)
            evidence[signal_id] = sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
    return decisions, evidence


def account_cost(offer, decision, evidence_root):
    approval = decision['account_cost_approval']
    schedule = approval.get('schedule')
    if not isinstance(schedule, dict):
        return None
    try:
        digest, binding, currency = (schedule[key] for key in ('evidence_sha256', 'account_binding', 'currency'))
        if (not re.fullmatch(r'[a-f0-9]{64}', digest) or not re.fullmatch(r'[a-f0-9]{64}', binding)
                or binding != offer.get('paper_account_binding') or not re.fullmatch(r'[A-Z]{3}', currency)):
            return None
        checked = utc(offer['native_quote_checked_at'])
        minimum, maximum, amount = map(Decimal, (
            schedule['stake_min'], schedule['stake_max'], offer['paper_amount_passing_native_limits'],
        ))
        cost = Decimal(approval['cost_per_unit'])
        if (not all(value.is_finite() for value in (minimum, maximum, amount, cost))
                or not 0 < minimum <= amount <= maximum or cost < 0
                or not utc(schedule['effective_from']) <= checked
                or schedule.get('effective_until') and checked >= utc(schedule['effective_until'])):
            return None
        evidence = (evidence_root / digest).read_bytes()
        if not evidence or sha256(evidence).hexdigest() != digest:
            return None
    except (KeyError, InvalidOperation, OSError, TypeError, ValueError):
        return None
    return cost, currency


def settlement_receipts(offer, result, cutoff, cost, currency):
    if result is None or result['status'] != 'paper_quote_diagnostic':
        return ()
    checked = utc(offer['native_quote_checked_at'])
    if ((result['fixture_id'], result['sport'], result['phase'], result['market_kind'])
            != (offer['fixture_id'], offer['sport'], offer['phase'], offer['market_kind'])
            or utc(result['native_quote_checked_at']) != checked
            or decimal(result['native_selected_price']) != decimal(offer['native_selected_price'])):
        raise ValueError('settlement comes from a different paper offer')
    observed, computed = utc(result['result_received_at']), utc(result['result_computed_at'])
    gross = decimal(result['quoted_unit_return'])
    stressed = decimal(result['stressed_unit_return_before_unknown_costs'])
    win_profit = decimal(offer['native_selected_price']) - 1
    expected = {'win': win_profit, 'half_win': win_profit / 2, 'push': Decimal(0), 'void': Decimal(0),
                'loss': Decimal(-1), 'half_loss': Decimal('-0.5')}.get(result['outcome'])
    stress_value = expected * (1 - SLIPPAGE_PROFIT_HAIRCUT) if expected and expected > 0 else expected
    if (not checked < observed <= computed < cutoff or expected is None or gross != expected
            or stressed != stress_value or type(result['result_version']) is not int or result['result_version'] <= 0):
        raise ValueError('invalid paper settlement receipt')
    return (Receipt(computed, observed, gross, cost, currency, slippage_cost=gross - stressed),)


def run(frames, settlement, start, end, reference_root, cost_root, registered_at=None):
    cutoff = utc(settlement['cutoff'])
    if (start.second or start.microsecond or end.second or end.microsecond or not start < end <= cutoff
            or settlement['frames_sha256'] != frames_digest(frames)):
        raise ValueError('paper study requires matching frozen input and UTC minute boundaries')
    offers = {}
    originals = {}
    discovery = Counter()
    slots = set()
    seen = set()
    for frame in frames:
        slot = utc(frame['slot'])
        if not start <= slot < end:
            continue
        if (slot in slots or slot.second or slot.microsecond
                or utc(frame['begin']) != slot - timedelta(seconds=90)
                or utc(frame['end']) != slot - timedelta(seconds=30)
                or not slot <= utc(frame['observed_at']) < slot + timedelta(seconds=30)):
            raise ValueError('duplicate or invalid capture slot')
        slots.add(slot)
        for row in frame['rows']:
            signal_id = row['signal_id']
            if signal_id in seen:
                raise ValueError('duplicate captured recommendation')
            seen.add(signal_id)
            discovery[row['status']] += 1
            if row['status'] == 'native_preflight_available':
                offers[signal_id] = validate_offer(row, frame, cutoff)
                originals[signal_id] = row
    outcomes = {row['signal_id']: row for row in settlement['paper_settlements']}
    if len(outcomes) != len(settlement['paper_settlements']):
        raise ValueError('duplicate simulated settlements')
    manifest = (reference_root / 'reference-price.json').read_bytes()
    decisions, reference_digests = price_decisions(offers, reference_root, json.loads(manifest)['fixtures'])
    baseline, selected = [], []
    selected_bindings = set()
    reasons = Counter()
    for signal_id, decision in decisions.items():
        if decision.get('status') not in ('price_eligible', 'below_minimum_odds'):
            reasons.update(decision['reasons'])
            continue
        offer = originals[signal_id]
        verified_cost = account_cost(offer, decision, cost_root)
        if verified_cost is None:
            reasons['account_cost_scope_unverified'] += 1
            continue
        receipts = settlement_receipts(offer, outcomes.get(signal_id), cutoff, *verified_cost)
        at = utc(offer['native_quote_checked_at'])
        fill = Fill(signal_id, offer['fixture_id'], offer['expert_id'], offer['market_kind'], offer['sport'],
                    at, at, False, receipts)
        baseline.append(fill)
        if (decision['status'] == 'price_eligible'
                and decision['pricing']['worst_net_at_offered_odds'] >= MINIMUM_NET_EDGE):
            selected.append(fill)
            selected_bindings.add(offer['paper_account_binding'])
    prospective = registered_at is not None and registered_at <= start
    expected_slots = (end - start) // ONE_MINUTE
    primary = summarize(selected, start, end, cutoff, prospective)
    if len(selected_bindings) > 1:
        primary['validation_blockers'].append('mixed_paper_accounts')
    return {'strategy': PRIMARY_ARM, 'scope': 'paper_only', 'start': start, 'end': end, 'cutoff': cutoff,
            'registered_at': registered_at, 'observed_slots': len(slots), 'missing_slots': expected_slots - len(slots),
            'discovery': dict(discovery), 'exclusions': dict(reasons), 'reference_manifest_sha256': sha256(manifest).hexdigest(),
            'reference_matches_sha256': frames_digest(reference_digests),
            'frames_sha256': settlement['frames_sha256'], 'native_quote_profit_haircut': SLIPPAGE_PROFIT_HAIRCUT,
            'minimum_net_edge': MINIMUM_NET_EDGE,
            'arms': {'follow_priced': summarize(baseline, start, end, cutoff, False), PRIMARY_ARM: primary}}


def registration(path, start, end):
    raw = path.read_bytes()
    document = json.loads(raw)
    registered_at = utc(document['registered_at'])
    if (document['strategy'] != PRIMARY_ARM or (utc(document['start']), utc(document['end'])) != (start, end)
            or registered_at > start or not re.fullmatch(r'[a-f0-9]{40}', document['implementation_commit'])
            or document['evaluator_sha256'] != sha256(Path(__file__).read_bytes()).hexdigest()):
        raise ValueError('registration does not match the strategy, evaluator or fixed interval')
    return {'registered_at': registered_at, 'implementation_commit': document['implementation_commit'],
            'registration_sha256': sha256(raw).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('frames', 'settlements', 'reference_root', 'cost_root'):
        parser.add_argument(arg, type=Path)
    parser.add_argument('--start', type=utc, required=True)
    parser.add_argument('--end', type=utc, required=True)
    parser.add_argument('--registration', type=Path)
    args = parser.parse_args()
    raw = args.settlements.read_bytes()
    evidence = registration(args.registration, args.start, args.end) if args.registration else {}
    report = run(json.loads(args.frames.read_bytes()), json.loads(raw), args.start, args.end,
                 args.reference_root, args.cost_root, evidence.get('registered_at'))
    print(json.dumps(report | evidence | {'settlement_sha256': sha256(raw).hexdigest()}, default=str, indent=2))


if __name__ == '__main__':
    main()
