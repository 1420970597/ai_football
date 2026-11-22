"""P03 expert-versus-market calibration and P07 contract equivalence.

P03 reuses the verified ``market_adjusted_experts.prepare`` / ``result_at`` helpers.
Every forecast is made causally: the author's logit offset is estimated only from
recommendations created earlier whose results were already visible at that instant,
the fixture under evaluation is excluded, and an author without history falls back to
the market. Half wins and half losses enter as fractional outcomes weighted by the
effective stake; pushes and voids are dropped from the denominator.

P07 turns contract availability, depth, freshness and taker cost into one explicit
state per quote. Net inference stops whenever the rules hash, side or cost cannot be
verified, and the taker cost is charged once when it is known.
"""

from collections import Counter, defaultdict
from datetime import timedelta
from decimal import Decimal
import json
from math import exp, log
from pathlib import Path

from .evidence import INSUFFICIENT, MEASURED, decimal, load_json, section, utc

ONE = Decimal(1)
DEFAULT_STRENGTH = 20.0
DEFAULT_EDGE = 0.0
MAX_QUOTE_AGE = timedelta(minutes=5)
MIN_DEPTH = Decimal('1')
ACTIVE_OUTCOMES = {'win', 'half_win', 'loss', 'half_loss'}


def _engine():
    try:
        from market_adjusted_experts import prepare, result_at
    except ImportError as error:  # pragma: no cover - depends on mounted report path
        raise RuntimeError('market_adjusted_experts is not importable from the frozen report path') from error
    return prepare, result_at


def _clamp(value, low=1e-6, high=1 - 1e-6):
    return min(high, max(low, value))


def logit(probability):
    probability = _clamp(probability)
    return log(probability / (1 - probability))


def sigmoid(value):
    if value >= 0:
        return 1 / (1 + exp(-value))
    factor = exp(value)
    return factor / (1 + factor)


# --------------------------------------------------------------------------- P03


def outcome_weight(pick, cutoff, result_at):
    """Return ``(fraction_won, stake, reason)`` for one settled recommendation.

    ``fraction_won`` is 1 for a full win, 0.5 for a half win and 0 for a loss or
    half loss; the effective stake is the weight so a half stake counts half.
    Pushes and voids (stake 0) are excluded rather than counted as losses.
    """
    if pick.get('withdrawn_at') is not None and pick['withdrawn_at'] <= cutoff:
        return None, None, 'withdrawn'
    result = result_at(pick, cutoff)
    if result is None:
        return None, None, 'settlement_unavailable'
    won, stake = result
    if stake == 0:
        return None, None, 'push_or_void'
    return float(won / stake), float(stake), None


def author_offset(history, author, cutoff, *, result_at, strength=DEFAULT_STRENGTH, iterations=8):
    """Shrunk author logit offset from earlier, already-settled recommendations."""
    observations = []
    for pick in history:
        if pick['expert_id'] != author or not pick['created_at'] < cutoff:
            continue
        fraction, weight, _ = outcome_weight(pick, cutoff, result_at)
        if fraction is None:
            continue
        observations.append((pick['market_share'], fraction, weight))
    offset = 0.0
    for _ in range(iterations):
        score = 0.0
        information = 0.0
        for market, fraction, weight in observations:
            probability = sigmoid(logit(market) + offset)
            score += weight * (fraction - probability)
            information += weight * probability * (1 - probability)
        # Ridge toward the market: gradient minus strength*offset, Hessian plus strength.
        offset += (score - strength * offset) / (information + strength)
    return offset, len(observations)


def forward_replay(picks, cutoff, *, strength=DEFAULT_STRENGTH, result_at):
    """Causal forecast per recommendation against the market baseline."""
    ordered = sorted(picks, key=lambda item: (item['created_at'], item['id']))
    history = []
    records = []
    missing = Counter()
    for pick in ordered:
        market = pick.get('market_share')
        if market is None:
            missing['market_share_unavailable'] += 1
            continue
        prior = [item for item in history if item['fixture_id'] != pick['fixture_id']]
        offset, sample = author_offset(prior, pick['expert_id'], pick['created_at'],
                                       result_at=result_at, strength=strength)
        predicted = sigmoid(logit(float(market)) + offset)
        history.append(pick)
        fraction, weight, reason = outcome_weight(pick, cutoff, result_at)
        if reason:
            missing[reason] += 1
            continue
        records.append({
            'fixture_id': pick['fixture_id'],
            'expert_id': pick['expert_id'],
            'created_at': pick['created_at'],
            'market': float(market),
            'predicted': predicted,
            'fraction': fraction,
            'weight': weight,
            'price': float(pick['recommendation_price']),
            'offset': offset,
            'author_sample': sample,
            'edge': predicted * float(pick['recommendation_price']) - 1,
        })
    return records, missing


def _brier(records):
    total = sum(record['weight'] for record in records)
    if not total:
        return None
    adjusted = sum(record['weight'] * (record['predicted'] - record['fraction']) ** 2 for record in records)
    market = sum(record['weight'] * (record['market'] - record['fraction']) ** 2 for record in records)
    def log_loss(key):
        return -sum(row['weight'] * (row['fraction'] * log(_clamp(row[key]))
                    + (1 - row['fraction']) * log(1 - _clamp(row[key]))) for row in records) / total

    return {'brier': round(adjusted / total, 6), 'market_brier': round(market / total, 6),
            'log_loss': round(log_loss('predicted'), 6), 'market_log_loss': round(log_loss('market'), 6)}


def _scored(records):
    return _brier(records) or {}


def _calibration(records):
    bins = Counter()
    totals = defaultdict(float)
    for record in records:
        bucket = min(9, int(record['predicted'] * 10))
        bins[bucket] += 1
        totals[bucket] += record['fraction']
    return [{'bin': f'{bucket / 10:.1f}-{(bucket + 1) / 10:.1f}', 'count': bins[bucket],
             'observed': round(totals[bucket] / bins[bucket], 6)} for bucket in sorted(bins)]


def summarize(records, *, edge=DEFAULT_EDGE):
    """Paired model/market scores plus the calibration of the selected subset."""
    base = {
        'settled': len(records),
        'effective_stake': round(sum(record['weight'] for record in records), 6),
        'unknown_author_fallback': sum(1 for record in records if record['author_sample'] == 0),
        'paired': _brier(records),
        'calibration': _calibration(records),
    }
    selected = [record for record in records if record['edge'] > edge]
    base['selected'] = {'count': len(selected), 'threshold': edge, **_scored(selected),
                        'calibration': _calibration(selected)}
    if records:
        ordered = sorted(records, key=lambda item: item['created_at'])
        size = len(ordered)
        blocks = [ordered[index * size // 3:(index + 1) * size // 3] for index in range(3)]
        base['time_blocks'] = [{'block': index + 1, 'settled': len(block), **_scored(block)}
                               for index, block in enumerate(blocks)]
    return base


def run_calibration(cohort_path, *, strength=DEFAULT_STRENGTH, edge=DEFAULT_EDGE):
    """P03 entry: prepare the frozen cohort, replay causally, summarise."""
    prepare, result_at = _engine()
    document, record = load_json(cohort_path, Path(cohort_path).parent)
    cutoff = utc(document['cutoff'])
    picks, missing = [], Counter()
    for row in document.get('rows', ()):
        status, pick = prepare(row)
        if status != 'verified':
            missing[f'prepare_{status}'] += 1
            continue
        if pick.get('market_share') is None:
            missing['market_share_unavailable'] += 1
            continue
        picks.append(pick)
    records, replay_missing = forward_replay(picks, cutoff, strength=strength, result_at=result_at)
    missing.update(replay_missing)
    result = summarize(records, edge=edge)
    contributions = Counter()
    for item in records:
        contributions[item['expert_id']] += item['weight']
    top = [author for author, _ in contributions.most_common(2)]
    without_top = [item for item in records if item['expert_id'] not in top]
    result['top_contributors_removed'] = {'author_count': len(top), **summarize(without_top, edge=edge)}
    return section(
        label=MEASURED if result['settled'] >= 50 else INSUFFICIENT,
        sources=[record],
        missing=missing,
        coverage={'verified_picks': len(picks), 'settled': result['settled']},
        results={'strength': strength, 'edge_threshold': edge, **result},
        limitations=[
            'offsets are estimated only from results visible before each recommendation',
            'the market share comes from the same verified quote the recommendation used',
        ],
    )


# --------------------------------------------------------------------------- P07

STATE_PRICEABLE = 'priceable'
STATE_REJECT = 'rejected'


def quote_state(quote, market, reference, *, now=None, max_age=MAX_QUOTE_AGE, min_depth=MIN_DEPTH):
    """Full-state rule for one two-sided quote against a verified contract binding.

    Returns ``state`` plus every reason it is not priceable. A quote is priceable
    only when the contract is verified, the snapshot is not future-dated, it is not
    stale, it is two-sided with enough depth, the market is open and the ask is a
    valid probability price below 1.
    """
    reasons = []
    try:
        received = utc(quote['received_at'])
        quote_time = utc(quote['quote_time'])
    except (KeyError, TypeError, ValueError):
        return {'state': STATE_REJECT, 'reasons': ['quote_time_invalid']}
    if quote_time > received:
        reasons.append('future_quote')
    if now is not None and received > now:
        reasons.append('quote_after_observation')
    age = (now if now is not None else received) - quote_time
    if age < timedelta(0) or age > max_age:
        reasons.append('quote_stale')
    if quote.get('status') != 'two_sided':
        reasons.append('not_two_sided')
    if not market.get('accepting_orders', True) or str(market.get('market_state', '')).endswith('CLOSED'):
        reasons.append('market_not_open')
    if reference.get('rules_sha256') and reference['rules_sha256'] != market.get('rules_sha256'):
        reasons.append('rules_hash_mismatch')
    if reference.get('line') is not None and str(reference['line']) != str(market.get('line')):
        reasons.append('line_mismatch')
    if quote.get('status') != 'two_sided':
        return {'state': STATE_REJECT, 'reasons': sorted(set(reasons)),
                'ask': None, 'depth': None, 'fair_minus_ask': None}
    try:
        ask = decimal(quote['ask'], name='ask')
        depth = decimal(quote.get('ask_size', '0'), name='ask_size')
    except (KeyError, ValueError):
        return {'state': STATE_REJECT, 'reasons': sorted(set(reasons) | {'price_unparseable'}),
                'ask': None, 'depth': None, 'fair_minus_ask': None}
    if not (0 < ask < ONE):
        reasons.append('ask_out_of_range')
    if depth < min_depth:
        reasons.append('insufficient_depth')
    return {
        'state': STATE_REJECT if reasons else STATE_PRICEABLE,
        'reasons': sorted(set(reasons)),
        'ask': ask,
        'depth': depth,
        'fair_minus_ask': None,
    }


def taker_edge(fair_probability, ask, *, cost_per_unit=Decimal(0)):
    """Gross and cost-adjusted edge of buying at ``ask`` with a known cost."""
    edge = Decimal(str(fair_probability)) - ask
    return {'gross': edge, 'net': edge - cost_per_unit, 'cost': cost_per_unit}


def _book_quotes(book_document):
    """Yield ``(fixture_label, market, quote)`` from one captured book snapshot."""
    label = book_document.get('label') or book_document.get('fixture') or 'unknown'
    for market in book_document.get('markets', ()):
        for quote in market.get('books', ()):
            yield label, market, quote


def analyze_contracts(reference_document, book_documents, *, max_age=MAX_QUOTE_AGE, min_depth=MIN_DEPTH):
    """Contract mapping coverage and quote-state coverage over frozen snapshots."""
    states = Counter()
    reasons = Counter()
    references = {}
    mapping_total = 0
    for fixture_id, fixture in reference_document.get('fixtures', {}).items():
        for binding in fixture.get('bindings', ()):
            mapping_total += 1
            key = (binding.get('source'), binding.get('market'))
            orientation = fixture.get('outcomes', {}).get(binding.get('source'), {}).get(binding.get('market'), {})
            references.setdefault(key, []).append(binding | {
                'fixture_id': fixture_id, 'labels': orientation.get('labels'),
                'rules_sha256': orientation.get('rules_sha256'),
            })
    verified = sum(1 for bindings in references.values() for binding in bindings
                   if binding.get('rules_sha256') and binding.get('labels'))
    quote_count = 0
    priceable = 0
    independently_mapped = 0
    for document in book_documents:
        for _label, market, quote in _book_quotes(document):
            quote_count += 1
            bindings = references.get((market.get('source'), market.get('market')), [])
            matched = [binding for binding in bindings if binding.get('rules_sha256')
                       and binding['rules_sha256'] == market.get('rules_sha256')
                       and quote.get('outcome') in (binding.get('labels') or {})]
            independently_mapped += len(matched) == 1
            state = quote_state(quote, market, matched[0] if len(matched) == 1 else {},
                                now=utc(document['captured_at']) if document.get('captured_at') else None,
                                max_age=max_age, min_depth=min_depth)
            states[state['state']] += 1
            reasons.update(state['reasons'])
            if state['state'] == STATE_PRICEABLE:
                priceable += 1
    return {
        'contract_bindings': mapping_total,
        'bindings_with_rules_hash': verified,
        'quotes_observed': quote_count,
        'priceable_quotes': priceable,
        'independently_mapped_quotes': independently_mapped,
        'verified_cross_venue_pairs': 0,
        'priceable_share': round(priceable / quote_count, 6) if quote_count else 0.0,
        'quote_states': dict(states),
        'rejection_reasons': dict(reasons),
    }


def run_contracts(reference_path, books_dir, *, native_frames=(), cost_root=None, cutoff=None):
    """P07 entry: replay the frozen reference bindings and book snapshots."""
    reference, reference_record = load_json(reference_path, Path(reference_path).parent)
    books, sources = [], [reference_record]
    for path in sorted(Path(books_dir).glob('*.jsonl')):
        for line in path.read_bytes().splitlines():
            if line.strip():
                books.append(json.loads(line) | {'label': path.stem})
        sources.append({'path': path.name, 'bytes': path.stat().st_size})
    result = analyze_contracts(reference, books)
    # Cross-venue eligibility is established by the same rule used for C02.
    # A priced single-venue book is a coverage observation only.
    cross = None
    if cutoff is not None:
        from .pricing import analyze as replay_price

        cross = replay_price(native_frames, reference_root=Path(reference_path).parent,
                             cost_root=cost_root, cutoff=cutoff)
        result['verified_cross_venue_pairs'] = sum(
            cross['classes'].get(status, 0) for status in ('price_eligible', 'below_minimum_odds')
        )
        result['cross_venue_replay'] = cross
    return section(
        label=MEASURED if result['verified_cross_venue_pairs'] else INSUFFICIENT,
        sources=sources,
        missing=[*result['rejection_reasons']] + (
            [] if result['verified_cross_venue_pairs'] else ['same_time_cross_venue_pair_and_cost_unverified']
        ),
        coverage={'bindings': result['contract_bindings'], 'quotes': result['quotes_observed']},
        results=result,
        limitations=[
            'net inference stops when the rules hash, side or taker cost is unverified',
            'a single venue snapshot cannot establish cross-source equivalence',
        ],
    )


if __name__ == '__main__':  # sign/offset self-check without archives
    assert abs(sigmoid(logit(0.4)) - 0.4) < 1e-12
    assert logit(0.5) == 0.0
    quote = {'status': 'two_sided', 'ask': '0.40', 'ask_size': '10',
             'quote_time': '2026-11-20T04:00:00+00:00', 'received_at': '2026-11-20T04:00:05+00:00'}
    state = quote_state(quote, {'rules_sha256': 'x', 'accepting_orders': True}, {'rules_sha256': 'x'})
    assert state['state'] == STATE_PRICEABLE, state
    assert state['reasons'] == []
    stale = quote | {'quote_time': '2026-11-20T03:00:00+00:00'}
    assert 'quote_stale' in quote_state(stale, {'rules_sha256': 'x'}, {'rules_sha256': 'x'})['reasons']
    print('market self-check ok', taker_edge(0.45, Decimal('0.40'), cost_per_unit=Decimal('0.01')))
