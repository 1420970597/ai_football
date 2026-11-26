"""Replay fixed research rules on verified, previously accepted native orders."""

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from random import Random
import re


ZERO = Decimal(0)
STRESS_COST = Decimal('0.02')
HISTORY_DAYS = timedelta(days=28)
WEEK = timedelta(days=7)
DAY = timedelta(days=1)
HALF_DAY = timedelta(hours=12)
ARMS = ('baseline', 'one_per_fixture', 'price_quality', 'expert_history', 'combined')


def utc(value):
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() != timedelta(0):
        raise ValueError('expected UTC timestamp')
    return moment


def decimal(value):
    try:
        result = Decimal(str(value))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class Receipt:
    observed_at: datetime
    settled_at: datetime | None
    unit_return: Decimal | None
    unit_fees: Decimal | None
    currency: str | None
    slippage_cost: Decimal = STRESS_COST

    @classmethod
    def parse(cls, row, placed_at):
        observed = utc(row['observed_at'])
        settled = utc(row['settled_at']) if row['settled_at'] else None
        stake, payout, net, fees = map(decimal, (row['stake'], row['payout'], row['net'], row['fees']))
        valid = placed_at < observed and (settled is None or placed_at <= settled <= observed)
        currency = row['currency']
        if not isinstance(currency, str) or not re.fullmatch('[A-Z]{3}', currency):
            currency = None
        if not valid or stake is None or stake <= 0 or payout is None or payout < 0 or net is None:
            return cls(observed, settled, None, None, None)
        gross = payout - stake
        known_fee = fees if fees is not None and fees >= 0 else None
        if net != gross and (known_fee is None or net != gross - known_fee):
            return cls(observed, settled, None, None, None)
        return cls(observed, settled, gross / stake, known_fee / stake if known_fee is not None else None, currency)

    @property
    def stressed_return(self):
        # Unknown fees remain a separate coverage failure. This value is diagnostic in that case.
        return self.unit_return - (self.unit_fees or ZERO) - self.slippage_cost


@dataclass(frozen=True)
class Fill:
    order: str
    fixture: str
    expert: str | None
    market: str
    sport: str | None
    placed_at: datetime
    decision_at: datetime
    price_quality: bool
    receipts: tuple[Receipt, ...]

    @classmethod
    def parse(cls, row):
        placed = utc(row['placed_at'])
        preflight = utc(row['preflight_at']) if row['preflight_at'] else None
        signal = utc(row['signal_at']) if row['signal_at'] else None
        price = decimal(row['preflight_price'])
        reference = decimal(row['signal_price'])
        overround = decimal(row['overround'])
        valid_preflight = preflight is not None and signal is not None and signal <= preflight <= placed
        acceptable = (valid_preflight and price is not None and reference is not None
                      and price >= reference > 1 and overround is not None and ZERO <= overround <= Decimal('0.05'))
        return cls(row['order_id'], row['fixture_id'], row['expert_id'], row['market_kind'], row['sport'],
                   placed, preflight if valid_preflight else placed, acceptable,
                   tuple(Receipt.parse(receipt, placed) for receipt in row['receipts']))

    def latest_receipt(self, cutoff):
        # A correction received at the decision timestamp is not known to that decision.
        visible = [receipt for receipt in self.receipts
                   if receipt.observed_at < cutoff and receipt.unit_return is not None]
        if not visible:
            return None
        latest_at = max(receipt.observed_at for receipt in visible)
        latest = [receipt for receipt in visible if receipt.observed_at == latest_at]
        reference = latest[0]
        if any(receipt != reference for receipt in latest[1:]):
            return None
        return latest[-1]


def expert_has_edge(candidate, history):
    by_fixture = defaultdict(list)
    for fill in history:
        if fill.fixture == candidate.fixture or fill.placed_at < candidate.decision_at - HISTORY_DAYS:
            continue
        receipt = fill.latest_receipt(candidate.decision_at)
        if receipt and receipt.unit_return is not None:
            by_fixture[fill.fixture].append(receipt.stressed_return)
    if len(by_fixture) < 10:
        return False
    # Add 10 prior observations at a loss of 3% per unit. The denominator is positive.
    return sum(sum(returns) / len(returns) for returns in by_fixture.values()) > Decimal('0.30')


def choose(fills, start, end):
    chosen = {arm: [] for arm in ARMS}
    history = defaultdict(list)
    entered = set()
    combined = set()
    for fill in sorted(fills, key=lambda row: (row.decision_at, row.order)):
        profile = history[fill.expert]
        if start <= fill.placed_at < end:
            chosen['baseline'].append(fill)
            if fill.fixture not in entered:
                chosen['one_per_fixture'].append(fill)
                entered.add(fill.fixture)
            if fill.price_quality:
                chosen['price_quality'].append(fill)
            if fill.expert and expert_has_edge(fill, profile):
                chosen['expert_history'].append(fill)
                if fill.price_quality and fill.fixture not in combined:
                    chosen['combined'].append(fill)
                    combined.add(fill.fixture)
        profile.append(fill)
    return chosen


def buckets(start, end, interval):
    while start < end:
        stop = start + interval
        yield start, min(stop, end), stop <= end
        start = stop


def interval_summary(trades, start, end, step, use_settlement):
    grouped = defaultdict(list)
    for fill, receipt in trades:
        at = receipt.settled_at if use_settlement else fill.placed_at
        if at is not None and start <= at < end:
            grouped[(at - start) // step].append(receipt.stressed_return)
    return [
        {'begin': left.isoformat(), 'end': right.isoformat(), 'complete': complete, 'trades': len(grouped[index]),
         'stressed_units': sum(grouped[index], ZERO)}
        for index, (left, right, complete) in enumerate(buckets(start, end, step))
    ]


def lower_bound(trades, start, end):
    days = (end - start) // DAY
    if days < 7 or not trades:
        return None
    fixtures = defaultdict(list)
    for fill, receipt in trades:
        fixtures[fill.fixture].append((fill.placed_at, receipt.stressed_return))
    returns = [ZERO] * days
    counts = [0] * days
    for entries in fixtures.values():
        index = min((min(at for at, _ in entries) - start) // DAY, days - 1)
        returns[index] += sum(value for _, value in entries)
        counts[index] += len(entries)
    # Moving two-day blocks preserve the dependence of nearby events. Every leg
    # of one fixture is assigned to the same day before resampling.
    blocks = [(float(returns[i] + returns[(i + 1) % days]), counts[i] + counts[(i + 1) % days])
              for i in range(days)]
    rng = Random(20261120)
    scores = []
    for _ in range(4000):
        blocks_drawn = [rng.choice(blocks) for _ in range(days // 2)]
        net, count = (sum(value for value, _ in blocks_drawn), sum(n for _, n in blocks_drawn))
        if days % 2:
            day = rng.randrange(days)
            net += float(returns[day])
            count += counts[day]
        scores.append(net / count if count else 0)
    return Decimal(str(sorted(scores)[199]))


def drawdown(trades):
    grouped = defaultdict(lambda: ZERO)
    for _, receipt in trades:
        if receipt.settled_at is not None:
            grouped[receipt.settled_at] += receipt.stressed_return
    balance = peak = loss = ZERO
    for at in sorted(grouped):
        balance += grouped[at]
        peak = max(peak, balance)
        loss = max(loss, peak - balance)
    return loss


def summarize(fills, start, end, cutoff, prospective):
    trades = []
    for fill in fills:
        receipt = fill.latest_receipt(cutoff)
        if receipt is not None and receipt.unit_return is not None:
            trades.append((fill, receipt))
    net = sum((receipt.stressed_return for _, receipt in trades), ZERO)
    gross = sum((receipt.unit_return for _, receipt in trades), ZERO)
    currencies = {receipt.currency for _, receipt in trades if receipt.currency}
    missing_cost = sum(receipt.currency is None or receipt.unit_fees is None for _, receipt in trades)
    missing_time = sum(receipt.settled_at is None for _, receipt in trades)
    market_nets = defaultdict(lambda: ZERO)
    for fill, receipt in trades:
        market_nets[fill.market] += receipt.stressed_return
    top_market = max(sorted(market_nets), key=market_nets.get) if market_nets else None
    without_top = net - market_nets[top_market] if top_market else ZERO
    weeks = interval_summary(trades, start, end, WEEK, use_settlement=False)
    half_days = interval_summary(trades, start, cutoff, HALF_DAY, use_settlement=True)
    fixtures = len({fill.fixture for fill, _ in trades})
    lower = lower_bound(trades, start, end)
    blockers = []
    if not prospective:
        blockers.append('retrospective_research')
    if not fills or len(trades) != len(fills):
        blockers.append('unsettled_or_unverified_receipts')
    if not trades or missing_cost or len(currencies) != 1:
        blockers.append('unverified_costs_or_currency')
    if missing_time:
        blockers.append('unverified_settlement_time')
    if fixtures < 200:
        blockers.append('fewer_than_200_fixtures')
    if sum(week['complete'] for week in weeks) < 4 or any(
            week['stressed_units'] <= 0 for week in weeks if week['complete']):
        blockers.append('four_positive_complete_weeks_missing')
    if lower is None or lower <= 0:
        blockers.append('nonpositive_or_missing_lower_bound')
    if len(market_nets) < 2 or without_top <= 0:
        blockers.append('no_positive_return_without_top_market')
    return {'selected': len(fills), 'settled': len(trades), 'fixtures': fixtures,
            'sports': sorted({fill.sport for fill, _ in trades if fill.sport}),
            'missing_costs_or_currency': missing_cost, 'missing_settlement_time': missing_time,
            'receipt_units_before_listed_fees': gross, 'stressed_units': net,
            'stressed_roi': net / len(trades) if trades else None, 'lower_95_roi': lower,
            'top_market': top_market, 'stressed_units_without_top_market': without_top,
            'settlement_time_max_drawdown': drawdown(trades),
            'weekly_placement_cohorts': weeks, 'settlement_half_days': half_days,
            'validation_blockers': blockers}


def run(document, start, end, *, registered_at=None, primary_arm=None):
    begin, stop, cutoff = (utc(document[key]) for key in ('begin', 'end', 'cutoff'))
    if not begin <= start < end <= stop <= cutoff:
        raise ValueError('evaluation interval outside export')
    if (registered_at is None) != (primary_arm is None) or primary_arm not in (None, *ARMS):
        raise ValueError('a prospective evaluation requires a registration time and one primary rule')
    rows = document['rows']
    if len({row['order_id'] for row in rows}) != len(rows):
        raise ValueError('duplicate orders in export')
    fills = [Fill.parse(row) for row in rows if row['fixture_id'] and row['market_kind'] and row['placed_at']]
    return {'start': start.isoformat(), 'end': end.isoformat(), 'cutoff': cutoff.isoformat(),
            'selection_scope': 'orders_accepted_under_original_policy',
            'registered_at': registered_at.isoformat() if registered_at else None, 'primary_arm': primary_arm,
            'extra_stress_cost_per_stake': STRESS_COST, 'excluded_unidentified_rows': len(rows) - len(fills),
            'arms': {key: summarize(selected, start, end, cutoff,
                                    registered_at is not None and registered_at <= start and key == primary_arm)
                     for key, selected in choose(fills, start, end).items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--start', type=utc, required=True)
    parser.add_argument('--end', type=utc, required=True)
    parser.add_argument('--registered-at', type=utc)
    parser.add_argument('--primary-arm', choices=ARMS)
    args = parser.parse_args()
    raw = args.source.read_bytes()
    result = run(json.loads(raw), args.start, args.end, registered_at=args.registered_at, primary_arm=args.primary_arm)
    print(json.dumps(result | {'input_sha256': sha256(raw).hexdigest()}, default=str, indent=2))


if __name__ == '__main__':
    main()
