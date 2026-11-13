"""Preserve intent across kickoff only where the payout meaning is provable."""


def manual_phase_sql(s='s', f='f', q='q'):
    # Verified Leyu period totals use the whole period. Live market 4 uses
    # remaining goals; it equals the pre-match handicap only at an explicit 0:0.
    native = f"{f}.id ~ '^leyu:[0-9]+$' AND {f}.sport='football'"
    total = (f"{q}.market_kind='total' AND (({q}.market_id='2' AND {q}.period='full_time') "
             f"OR ({q}.market_id='18' AND {q}.period='first_half'))")
    zero_handicap = (f"{q}.market_kind='handicap' AND {q}.market_id='4' AND {q}.period='full_time' "
                     f"AND {f}.home_score=0 AND {f}.away_score=0")
    return (f"({s}.phase={f}.phase OR ({s}.phase='scheduled' AND {f}.phase='live' "
            f"AND {s}.score_basis IN('unknown','full_period') AND "
            f"({q}.score_basis='full_period' OR ({q}.score_basis='unknown' AND "
            f"{native} AND ({total} OR ({zero_handicap}))))))")
