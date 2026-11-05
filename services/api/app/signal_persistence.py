"""Persist a validated signal and its immutable quote in one SQL command."""
import uuid

from .handicap_lines import line_terms


async def persist_signal(conn, *, job, attempt_id, quote, pick, confirmation):
    """Caller owns authorization, current quote locks and completion transaction."""
    if not conn.is_in_transaction():
        raise RuntimeError('signal persistence requires a transaction')
    signal_id = uuid.uuid4()
    baseline = {key: quote[column] for key, column in (
        ('home', 'home_score'), ('away', 'away_score'),
        ('half_home', 'half_home_score'), ('half_away', 'half_away_score'))}
    evidence = {'job_id': str(job['id']), 'attempt_id': str(attempt_id),
                'quote_id': str(pick.quote_id), 'score_basis': quote['score_basis'],
                'market_kind': quote['market_kind'], 'current_confirmation': confirmation,
                **line_terms(quote),
                'reviewed_price': str(quote['read_price']) if quote['read_price'] is not None else None,
                'reviewed_source_updated_at': quote['read_source_time'].isoformat() if quote['read_source_time'] else None}
    return await conn.fetchrow("""
        WITH inserted AS (
            INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,
                market_id,market_name,period,market_kind,line,selection,recommendation_price,
                quote_time,confidence,evidence_text,score_baseline,score_basis)
            VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17)
            ON CONFLICT(message_id,fixture_id,market_id,period,(COALESCE(line,0)),selection)
                DO NOTHING RETURNING id)
        INSERT INTO v2_quote_evidence(id,fixture_id,market_id,market_name,period,line,
            selection,price,evidence_kind,reference_id,source_updated_at,evidence,score_baseline)
        SELECT $18,$4,$6,$7,$8,$10,$11,$12,'expert_signal',inserted.id,$13,$19,$16
        FROM inserted RETURNING reference_id AS id
        """, signal_id, job['message_id'], job['expert_id'], quote['fixture_id'], quote['phase'],
        quote['market_id'], quote['market_name'], quote['period'], quote['market_kind'], quote['line'],
        quote['selection'], quote['price'], quote['source_updated_at'], pick.confidence,
        pick.evidence_text, baseline, quote['score_basis'], uuid.uuid4(), evidence)
