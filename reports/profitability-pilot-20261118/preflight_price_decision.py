"""Apply the market pricing rule to contemporaneous, approved reference books."""

from collections import Counter
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from market_reference import aware_time, market_terms, price_from_verified_references
from price_contract import decimal


TERMS = ('fixture_id', 'market_id', 'sport', 'phase', 'kind', 'period', 'line',
         'score_basis', 'line_basis', 'team_side', 'settlement_scope')


def complete_sources(section, expected_status='complete'):
    return {name for name, group in section.get('source_groups', {}).items()
            if group['status'] == expected_status}


def terms_match(approved, offer):
    line = approved.get('line')
    normalized = approved | {'line': decimal(line) if line is not None else None}
    return all(normalized.get(key) == offer.get(key) for key in TERMS)


def book_sample(offer, ref, source, *, line, kind):
    depth = decimal(source['minimum_book_depth'])
    if depth <= 0 or source['rules'].get(ref['market']) != ref['rules_sha256']:
        raise ValueError('reference depth or settlement rules lack an approval')
    books = {}
    for book in ref['books']:
        if min(decimal(book['bid_size']), decimal(book['ask_size'])) < depth:
            raise ValueError('reference depth below the approved threshold')
        side = book['canonical_outcome']
        if side in books:
            raise ValueError('duplicate canonical book outcome')
        books[side] = book
    contract = dict(offer, line=line, kind=kind)
    if kind == 'winner':
        contract.pop('line_basis', None)
        if 'proposition' in ref:
            contract['selection'] = ref['proposition']
    sample = {'contract': contract, 'status': 'active', 'order_book': books}
    if offer['phase'] == 'live':
        sample['event_state'] = ref['verified_event_state']
    return sample


def reference_input(offer, source, name, sections, mode):
    samples = []
    for line, refs in sections:
        bound = [ref for ref in refs if ref['source'] == name and ref.get('outcome_status') == 'mapped'
                 and ref['status'] == 'timed_depth'
                 and ref.get('live_event_quote_status') != 'quote_predates_known_event']
        kind = 'winner' if mode == 'three_way_books' else offer['kind']
        samples.extend(book_sample(offer, ref, source, line=line, kind=kind) for ref in bound)
    result = {'venue': source['venue'], 'feed_origin': source['feed_origin']}
    if mode == 'three_way_books':
        return result | {'outcome_quotes': {sample['contract']['selection']: sample for sample in samples}}
    if mode == 'adjacent_half_lines':
        return result | {'adjacent_quotes': samples}
    sample, = samples
    return result | sample


def evaluate(row, approvals, max_age_seconds):
    preflight = row['preflight']
    offer = {key: preflight.get(key) for key in TERMS} | {
        'score_basis': preflight.get('effective_score_basis'),
        'selection': preflight.get('selection'),
    }
    if offer['kind'] != 'handicap':
        offer['line_basis'] = None
    if offer['kind'] != 'team_total':
        offer['team_side'] = None
    if offer['sport'] == 'football':
        offer['settlement_scope'] = 'regulation'
    try:
        _, offer['line'] = market_terms(offer)
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return {'status': 'ineligible', 'reasons': ['contract_terms_unverified']}

    kind, line = offer['kind'], offer['line']
    adjacent = row.get('adjacent_line_coverage') or {}
    winner = row.get('winner_reference_for_zero_handicap') or {}
    if offer['sport'] == 'football' and (kind == 'winner' or kind == 'handicap' and line == 0 and winner):
        mode = 'three_way_books'
        group = row if kind == 'winner' else winner
        sections = [(None, group['references'])]
        sources = complete_sources(group)
    elif line is not None and abs(line) % 1 != Decimal('0.5'):
        mode = 'adjacent_half_lines'
        sections = [(section['line'], section['references']) for section in adjacent.get('lines', [])]
        sources = complete_sources(adjacent, 'paired')
    else:
        mode = 'same_line_books'
        sections = [(line, row['references'])]
        sources = complete_sources(row)
    result = {'reference_mode': mode, 'complete_sources': sorted(sources)}
    reasons = []
    if not any(refs for _, refs in sections):
        reasons.append('reference_contract_missing')
    elif len(sources) < 2:
        reasons.append('two_complete_sources_missing')
    if offer['phase'] == 'live' and (row.get('score_comparison', {}).get('status') != 'observed_score_matches'
                                    or not row.get('verified_offer_event_state')):
        reasons.append('live_state_unverified')
    context = row.get('score_comparison', {}).get('event_context', {})
    if context.get('delay_status') == 'delay_unresolved':
        reasons.append('live_delay_unresolved')
    if (context.get('red_card_status') in {'source_counts_conflict', 'invalid_red_card_event'}
            or context.get('latest_material_event_status') == 'missing_native_event_time'):
        reasons.append('live_event_evidence_conflict')
    if any(ref.get('live_event_quote_status') == 'quote_predates_known_event'
           for _, refs in sections for ref in refs):
        result['has_reference_quote_before_known_event'] = True
    try:
        cutoff = aware_time(preflight['captured_at'])
        checked = aware_time(preflight.get('provider_quote_checked_at'))
        if not timedelta(0) <= cutoff - checked <= timedelta(seconds=15):
            raise ValueError('preflight check outside its acceptance window')
    except (TypeError, ValueError):
        reasons.append('native_offer_check_time_unverified')
    try:
        approved = [entry for entry in approvals if terms_match(entry['contract'], offer)
                    and aware_time(entry['approved_at']) <= aware_time(preflight['captured_at'])]
    except (KeyError, TypeError, ValueError, InvalidOperation):
        approved = []
        reasons.append('invalid_approval')
    if len(approved) != 1:
        reasons.append('settlement_and_cost_approval_missing' if not approved else 'ambiguous_approval')
    if reasons:
        return result | {'status': 'ineligible', 'reasons': reasons}

    approval, = approved
    try:
        if (not approval['cost_evidence'] or not approval['settlement_evidence']
                or decimal(approval['required_net_edge']) <= 0):
            raise ValueError('settlement, cost and positive margin need evidence')
        references = {}
        for name in sorted(sources & approval['sources'].keys()):
            try:
                references[name] = reference_input(offer, approval['sources'][name], name, sections, mode)
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
        if len(references) < 2:
            return result | {'status': 'ineligible', 'reasons': ['two_approved_depth_sources_missing']}
        document = {
            'reference_mode': mode, 'contract': offer, 'as_of': preflight['captured_at'],
            'max_age_seconds': max_age_seconds, 'max_quote_skew_seconds': min(5, max_age_seconds),
            'offer_venue': 'leyu', 'offer_feed_origin': approval['offer_feed_origin'],
            'offer_status': 'active', 'offer_checked_at': preflight['provider_quote_checked_at'],
            'offered_odds': preflight['price'], 'cost_per_unit': approval['cost_per_unit'],
            'required_net_edge': approval['required_net_edge'], 'references': references,
        }
        if 'verified_offer_event_state' in row:
            document['event_state'] = row['verified_offer_event_state']
        decision = price_from_verified_references(document)
    except (KeyError, TypeError, ValueError, ArithmeticError) as error:
        return result | {'status': 'ineligible', 'reasons': ['reference_validation_failed'],
                         'detail': str(error)}
    return result | {'status': 'price_eligible' if decision['passes_price_test'] else 'below_minimum_odds',
                     'approval_at': approval['approved_at'], 'pricing': decision}


def compare_with_receipts(rows):
    """Compare policies on the same natural automatic preflights without using outcomes to select orders."""
    comparison = {name: {'selected': 0, 'receipt_statuses': Counter(), 'net_by_currency': Counter()}
                  for name in ('current_following', 'price_rule', 'abstain')}
    for row in rows:
        order, decision = row['preflight'], row['price_decision']
        if order.get('manual') is not False:
            continue
        arms = ['current_following']
        if decision['status'] == 'price_eligible':
            arms.append('price_rule')
        for name in arms:
            group = comparison[name]
            group['selected'] += 1
            receipt = order['settlement_status']
            if receipt == 'locally_matched_receipt':
                try:
                    net = decimal(order['recorded_receipt_net'])
                    group['net_by_currency'][order['receipt_currency']] += net
                except (InvalidOperation, TypeError, ValueError):
                    receipt = 'settlement_amounts_unverified'
            group['receipt_statuses'][receipt] += 1
    return comparison
