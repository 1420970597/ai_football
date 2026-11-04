"""Preserve native home-view lines while declaring the selected option's line.

Verified Leyu football markets 4/19 carry home-view hl.hv; option on is
opposite for ot=2. Other providers and market kinds retain their own contract.
No stored quote, signal, selection identifier or order snapshot is rewritten.
"""
from decimal import Decimal


def line_basis(row, fixture_id=None):
    identity = str(fixture_id or row.get('fixture_id') or '')
    native = identity.startswith('leyu:') and identity[5:].isdigit()
    return ('provider_home' if native and row.get('market_kind') == 'handicap'
            and str(row.get('market_id')) in {'4', '19'} else 'selection')


def line_terms(row, fixture_id=None):
    basis = line_basis(row, fixture_id)
    value = row.get('line')
    if value is not None and basis == 'provider_home' and row.get('selection') == 'away':
        value = -Decimal(str(value))
    return {'line_basis': basis, 'selection_line': str(value) if value is not None else None}


def line_projection(row, fixture_id=None):
    return dict(row) | line_terms(row, fixture_id)
