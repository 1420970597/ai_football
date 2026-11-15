"""Preserve intent across kickoff only where the payout meaning is provable."""


def manual_phase_sql(s='s', f='f', q='q'):
    # Verified Leyu period totals use the whole period. Live market 4 uses
    # remaining goals. With the same line/selection it equals the pre-match
    # handicap whenever the known current score difference is zero, not only 0:0.
    native = f"{f}.id ~ '^leyu:[0-9]+$' AND {f}.sport='football'"
    total = (f"{q}.market_kind='total' AND (({q}.market_id='2' AND {q}.period='full_time') "
             f"OR ({q}.market_id='18' AND {q}.period='first_half'))")
    tied_handicap = (f"{q}.market_kind='handicap' AND {q}.market_id='4' AND {q}.period='full_time' "
                     f"AND {f}.home_score>=0 AND {f}.home_score={f}.away_score")
    return (f"({s}.phase={f}.phase OR ({s}.phase='scheduled' AND {f}.phase='live' "
            f"AND {s}.score_basis IN('unknown','full_period') AND "
            f"({q}.score_basis='full_period' OR ({q}.score_basis='unknown' AND "
            f"{native} AND ({total} OR ({tied_handicap}))))))")


def current_selection_sql(s='s', q='q', *, require_active=True):
    """Native option identity; automated execution verifies status upstream."""
    identity = (f"{q}.fixture_id={s}.fixture_id AND {q}.market_id={s}.market_id "
            f"AND {q}.period={s}.period AND {q}.market_kind={s}.market_kind "
            f"AND ({q}.line IS NOT DISTINCT FROM {s}.line OR {s}.market_kind='winner' "
            f"AND COALESCE({q}.line,0)=0 AND COALESCE({s}.line,0)=0) "
            f"AND {q}.selection={s}.selection "
            f"AND NULLIF({q}.play_id,'') IS NOT NULL "
            f"AND NULLIF({q}.market_instance_id,'') IS NOT NULL "
            f"AND NULLIF({q}.selection_id,'') IS NOT NULL")
    if not require_active:
        return identity
    return (identity + f" AND {q}.status='active' AND {q}.price>1 "
            f"AND {q}.price<'Infinity'::numeric "
            f"AND ({q}.source_updated_at IS NULL OR {q}.source_updated_at<=now())")
