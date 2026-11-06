"""Persist a validated signal and its immutable quote in one SQL command."""
import uuid
from decimal import Decimal

from .handicap_lines import line_terms
from .team_totals import team_total_terms


def _signal_context(job, attempt_id, quote, pick, confirmation):
    baseline = {key: quote[column] for key, column in (
        ('home', 'home_score'), ('away', 'away_score'),
        ('half_home', 'half_home_score'), ('half_away', 'half_away_score'))}
    evidence = {'job_id': str(job['id']), 'attempt_id': str(attempt_id),
                'quote_id': str(pick.quote_id), 'score_basis': quote['score_basis'],
                'market_kind': quote['market_kind'], 'current_confirmation': confirmation,
                **line_terms(quote),
                **team_total_terms(quote, quote['fixture_id']),
                'reviewed_price': str(quote['read_price']) if quote['read_price'] is not None else None,
                'reviewed_source_updated_at': quote['read_source_time'].isoformat() if quote['read_source_time'] else None}
    return baseline, evidence


async def persist_signal(conn, *, job, attempt_id, quote, pick, confirmation):
    """Single-pick compatibility uses the same atomic batch implementation."""
    result, = await persist_signals(conn, job=job, attempt_id=attempt_id,
                                   entries=[(quote, pick, confirmation)])
    return None if result['duplicate'] else {'id': result['id']}


async def persist_signals(conn, *, job, attempt_id, entries):
    """Save validated picks atomically, retaining first-input and conflict prices."""
    if not conn.is_in_transaction():
        raise RuntimeError('signal persistence requires a transaction')
    if not entries:
        return []
    payload, leaders, references = [], {}, []
    for quote, pick, confirmation in entries:
        key = (quote['fixture_id'], quote['market_id'], quote['period'],
               quote['line'] or 0, quote['selection'])
        if key in leaders:
            references.append(leaders[key])
            continue
        position = len(payload)
        leaders[key] = position
        references.append(position)
        baseline, evidence = _signal_context(job, attempt_id, quote, pick, confirmation)
        payload.append({'position': position, 'id': str(uuid.uuid4()),
                        'evidence_id': str(uuid.uuid4()),
                        **{name: quote[name] for name in ('fixture_id', 'phase', 'market_id',
                            'market_name', 'period', 'market_kind', 'selection', 'score_basis')},
                        'line': str(quote['line']) if quote['line'] is not None else None,
                        'price': str(quote['price']),
                        'quote_time': quote['source_updated_at'].isoformat() if quote['source_updated_at'] else None,
                        'confidence': str(pick.confidence), 'evidence_text': pick.evidence_text,
                        'baseline': baseline, 'evidence': evidence})
    inserted = await conn.fetch("""
        WITH incoming AS MATERIALIZED (
            SELECT * FROM jsonb_to_recordset($3::jsonb) AS i(position int,id uuid,
                evidence_id uuid,fixture_id text,phase text,market_id text,market_name text,
                period text,market_kind text,line numeric,selection text,price numeric,
                quote_time timestamptz,confidence numeric,evidence_text text,
                baseline jsonb,score_basis text,evidence jsonb)),
        saved AS (
            INSERT INTO v2_expert_signals(id,message_id,expert_id,fixture_id,phase,
                market_id,market_name,period,market_kind,line,selection,recommendation_price,
                quote_time,confidence,evidence_text,score_baseline,score_basis)
            SELECT id,$1,$2,fixture_id,phase,market_id,market_name,period,market_kind,line,
                selection,price,quote_time,confidence,evidence_text,baseline,score_basis
            FROM incoming ORDER BY fixture_id,market_id,period,COALESCE(line,0),selection
            ON CONFLICT(message_id,fixture_id,market_id,period,(COALESCE(line,0)),selection)
                DO NOTHING RETURNING id)
        INSERT INTO v2_quote_evidence(id,fixture_id,market_id,market_name,period,line,
            selection,price,evidence_kind,reference_id,source_updated_at,evidence,score_baseline)
        SELECT i.evidence_id,i.fixture_id,i.market_id,i.market_name,i.period,i.line,
            i.selection,i.price,'expert_signal',saved.id,i.quote_time,i.evidence,i.baseline
        FROM incoming i JOIN saved ON saved.id=i.id RETURNING reference_id AS id
        """, job['message_id'], job['expert_id'], payload)
    new_ids = {str(row['id']) for row in inserted}
    results = {row['position']: {'id': uuid.UUID(row['id']), 'price': Decimal(row['price']), 'duplicate': False}
               for row in payload if row['id'] in new_ids}
    conflicts = [row for row in payload if row['id'] not in new_ids]
    if conflicts:
        # A new READ COMMITTED statement sees a conflicting insertion that
        # completed while INSERT waited; its immutable price wins on replay.
        existing = await conn.fetch("""
            SELECT i.position,s.id,s.recommendation_price AS price
            FROM jsonb_to_recordset($2::jsonb) AS i(position int,fixture_id text,
                market_id text,period text,line numeric,selection text)
            JOIN v2_expert_signals s ON s.message_id=$1 AND s.fixture_id=i.fixture_id
                AND s.market_id=i.market_id AND s.period=i.period
                AND COALESCE(s.line,0)=COALESCE(i.line,0) AND s.selection=i.selection
            """, job['message_id'], conflicts)
        for row in existing:
            results[row['position']] = {'id': row['id'], 'price': row['price'], 'duplicate': True}
    if len(results) != len(payload):
        raise RuntimeError('signal conflict could not be reconstructed')
    emitted, output = set(), []
    for position in references:
        result = results[position]
        output.append({**result, 'duplicate': result['duplicate'] or position in emitted})
        emitted.add(position)
    return output
