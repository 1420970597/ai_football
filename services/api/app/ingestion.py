"""规范化事实批量入库；远端往返次数不随报价数量增长。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import HTTPException


def quote_update_condition(current: str, incoming: str) -> str:
    """SQL aliases are fixed by this module; values remain bound parameters."""
    return (
        f"coalesce({incoming}.source_updated_at,{incoming}.received_at)>=coalesce({current}.source_updated_at,{current}.received_at) "
        f"AND (ROW({current}.market_name,{current}.market_kind,{current}.line,"
        f"{current}.price,{current}.source_updated_at,{current}.source_time_quality,"
        f"{current}.status,{current}.play_id,{current}.market_instance_id,"
        f"{current}.selection_id,{current}.is_main,{current}.score_basis) "
        f"IS DISTINCT FROM ROW({incoming}.market_name,{incoming}.market_kind,{incoming}.line,{incoming}.price,"
        f"{incoming}.source_updated_at,{incoming}.source_time_quality,{incoming}.status,{incoming}.play_id,"
        f"{incoming}.market_instance_id,{incoming}.selection_id,{incoming}.is_main,{incoming}.score_basis) "
        f"OR {incoming}.source_updated_at IS NULL AND {current}.received_at IS DISTINCT FROM {incoming}.received_at)"
    )


async def ingest_fixture_batch(conn, items) -> int:
    now = datetime.now(timezone.utc)
    fixtures = {}
    for item in items:
        if item.received_at > now + timedelta(seconds=5) or (item.source_updated_at and item.source_updated_at > now + timedelta(seconds=5)):
            raise HTTPException(status_code=422, detail="fixture timestamp is in the future")
        for quote in item.all_markets():
            if quote.received_at > now + timedelta(seconds=5) or (quote.source_updated_at and quote.source_updated_at > now + timedelta(seconds=5)):
                raise HTTPException(status_code=422, detail="quote timestamp is in the future")
        if item.source_id in fixtures:
            raise HTTPException(status_code=422, detail="duplicate fixture source in batch")
        fixtures[item.source_id] = item
    if not fixtures:
        return 0
    payload = []
    for item in fixtures.values():
        value = item.model_dump(mode="json", exclude={"markets", "markets_complete", "market_chunks", "market_chunk_count", "market_quote_count"})
        value["sport"] = item.sport.lower()
        for field in ("home_aliases", "away_aliases"):
            value[field] = [v.strip().casefold() for v in value[field] if v.strip()]
        payload.append(value)
    await conn.execute(
        "INSERT INTO v2_fixtures(id,source_id,sport,competition_id,competition_name,home_team_id,home_team,home_aliases,"
        "away_team_id,away_team,away_aliases,kickoff_at,phase,match_state,home_score,away_score,clock_minute,"
        "source_updated_at,received_at,source_time_quality,half_home_score,half_away_score) "
        "SELECT id,source_id,sport,competition_id,competition_name,home_team_id,home_team,home_aliases,"
        "away_team_id,away_team,away_aliases,kickoff_at,phase,match_state,home_score,away_score,clock_minute,"
        "source_updated_at,received_at,source_time_quality,half_home_score,half_away_score "
        "FROM jsonb_to_recordset($1::jsonb) AS f(id text,source_id text,sport text,competition_id text,"
        "competition_name text,home_team_id text,home_team text,home_aliases text[],away_team_id text,away_team text,"
        "away_aliases text[],kickoff_at timestamptz,phase text,match_state text,home_score int,away_score int,"
        "clock_minute int,source_updated_at timestamptz,received_at timestamptz,source_time_quality text,"
        "half_home_score int,half_away_score int) ORDER BY source_id "
        "ON CONFLICT(source_id) DO UPDATE SET sport=EXCLUDED.sport,competition_id=EXCLUDED.competition_id,"
        "competition_name=EXCLUDED.competition_name,home_team_id=EXCLUDED.home_team_id,home_team=EXCLUDED.home_team,"
        "home_aliases=EXCLUDED.home_aliases,away_team_id=EXCLUDED.away_team_id,away_team=EXCLUDED.away_team,"
        "away_aliases=EXCLUDED.away_aliases,kickoff_at=EXCLUDED.kickoff_at,phase=EXCLUDED.phase,"
        "match_state=EXCLUDED.match_state,home_score=EXCLUDED.home_score,away_score=EXCLUDED.away_score,"
        "clock_minute=EXCLUDED.clock_minute,source_updated_at=EXCLUDED.source_updated_at,received_at=EXCLUDED.received_at,"
        "source_time_quality=EXCLUDED.source_time_quality,half_home_score=EXCLUDED.half_home_score,half_away_score=EXCLUDED.half_away_score "
        "WHERE coalesce(EXCLUDED.source_updated_at,EXCLUDED.received_at)>=coalesce(v2_fixtures.source_updated_at,v2_fixtures.received_at) "
        # An unconfirmed cancellation is a current projection, not a final
        # result. A newer authoritative snapshot may correct/reinstate it.
        # Confirmed/void results remain protected by the result check below.
        "AND NOT(v2_fixtures.phase='finished' AND EXCLUDED.phase IN('scheduled','live')) "
        "AND NOT(v2_fixtures.phase='live' AND EXCLUDED.phase='scheduled') "
        # Final result evidence owns scores after settlement; a later schedule
        # snapshot may omit them or carry a different period's aggregate.
        "AND NOT EXISTS(SELECT 1 FROM v2_fixture_results r WHERE r.fixture_id=v2_fixtures.id "
        "AND r.version=(SELECT max(latest.version) FROM v2_fixture_results latest WHERE latest.fixture_id=v2_fixtures.id) "
        "AND r.status IN('confirmed','void')) "
        "AND (ROW(v2_fixtures.sport,v2_fixtures.competition_id,v2_fixtures.competition_name,"
        "v2_fixtures.home_team_id,v2_fixtures.home_team,v2_fixtures.home_aliases,v2_fixtures.away_team_id,"
        "v2_fixtures.away_team,v2_fixtures.away_aliases,v2_fixtures.kickoff_at,v2_fixtures.phase,"
        "v2_fixtures.match_state,v2_fixtures.home_score,v2_fixtures.away_score,v2_fixtures.clock_minute,"
        "v2_fixtures.source_updated_at,v2_fixtures.source_time_quality,v2_fixtures.half_home_score,"
        "v2_fixtures.half_away_score) IS DISTINCT FROM ROW(EXCLUDED.sport,EXCLUDED.competition_id,"
        "EXCLUDED.competition_name,EXCLUDED.home_team_id,EXCLUDED.home_team,EXCLUDED.home_aliases,"
        "EXCLUDED.away_team_id,EXCLUDED.away_team,EXCLUDED.away_aliases,EXCLUDED.kickoff_at,EXCLUDED.phase,"
        "EXCLUDED.match_state,EXCLUDED.home_score,EXCLUDED.away_score,EXCLUDED.clock_minute,"
        "EXCLUDED.source_updated_at,EXCLUDED.source_time_quality,EXCLUDED.half_home_score,EXCLUDED.half_away_score) "
        "OR EXCLUDED.source_updated_at IS NULL AND v2_fixtures.received_at IS DISTINCT FROM EXCLUDED.received_at)",
        payload,
    )
    # A fixture timestamp and its individual ctsp quote timestamps are independent.
    # Lock the stable stored identity/state for every submitted source, including
    # stale parent snapshots; result ingestion uses the same non-key row lock.
    # Neither path changes id/source_id. Keep writers serialized while allowing
    # review candidates/bindings to take the foreign key's KEY SHARE lock.
    stored = await conn.fetch(
        "WITH incoming AS MATERIALIZED (SELECT source_id,phase,source_updated_at,received_at "
        "FROM jsonb_to_recordset($1::jsonb) AS i(source_id text,phase text,source_updated_at timestamptz,received_at timestamptz)) "
        "SELECT f.id,f.source_id,f.phase, "
        "(coalesce(i.source_updated_at,i.received_at)>=coalesce(f.source_updated_at,f.received_at) "
        "AND NOT(f.phase='finished' AND i.phase IN('scheduled','live')) "
        "AND NOT(f.phase='live' AND i.phase='scheduled') "
        "AND NOT EXISTS(SELECT 1 FROM v2_fixture_results r WHERE r.fixture_id=f.id "
        "AND r.version=(SELECT max(latest.version) FROM v2_fixture_results latest WHERE latest.fixture_id=f.id) "
        "AND r.status IN('confirmed','void'))) AS accepted "
        "FROM v2_fixtures f JOIN incoming i USING(source_id) ORDER BY f.source_id FOR NO KEY UPDATE OF f",
        payload,
    )
    accepted_sources = {record['source_id'] for record in stored if record['accepted']}
    quote_rows, closures, finished = [], [], []
    for record in stored:
        item = fixtures[record["source_id"]]
        fixture_id = record["id"]
        markets = item.all_markets()
        for quote in markets:
            quote_rows.append((
                uuid.uuid4(), fixture_id, quote.market_id, quote.market_name, quote.period, quote.kind, quote.line,
                quote.selection.casefold(), quote.price, quote.source_updated_at, quote.received_at, quote.source_time_quality,
                'closed' if record['phase'] in {'finished', 'cancelled'} else quote.status,
                quote.play_id, quote.market_instance_id, quote.selection_id, quote.is_main, quote.score_basis,
            ))
        if item.markets_complete and record['source_id'] in accepted_sources:
            closures.append((fixture_id, [q.market_id for q in markets], [q.period for q in markets],
                             [q.line for q in markets], [q.selection.casefold() for q in markets], item.received_at))
        if record['phase'] in {"finished", "cancelled"}:
            finished.append(fixture_id)
    if quote_rows:
        quote_payload = [{
            'id': str(row[0]), 'fixture_id': row[1], 'market_id': row[2], 'market_name': row[3],
            'period': row[4], 'market_kind': row[5], 'line': str(row[6]) if row[6] is not None else None,
            'selection': row[7], 'price': str(row[8]) if row[8] is not None else None,
            'source_updated_at': row[9].isoformat() if row[9] is not None else None,
            'received_at': row[10].isoformat(), 'source_time_quality': row[11], 'status': row[12],
            'play_id': row[13], 'market_instance_id': row[14], 'selection_id': row[15],
            'is_main': row[16], 'score_basis': row[17],
        } for row in quote_rows]
        await conn.execute(
            "INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,period,market_kind,line,selection,price,source_updated_at,"
            "received_at,source_time_quality,status,play_id,market_instance_id,selection_id,is_main,score_basis) "
            "SELECT q.id,q.fixture_id,q.market_id,q.market_name,q.period,q.market_kind,q.line,q.selection,q.price,q.source_updated_at,"
            "q.received_at,q.source_time_quality,q.status,q.play_id,q.market_instance_id,q.selection_id,q.is_main,q.score_basis "
            "FROM jsonb_to_recordset($1::jsonb) AS q(id uuid,fixture_id text,market_id text,market_name text,period text,"
            "market_kind text,line numeric,selection text,price numeric,source_updated_at timestamptz,received_at timestamptz,"
            "source_time_quality text,status text,play_id text,market_instance_id text,selection_id text,is_main boolean,score_basis text) "
            # Avoid speculative INSERT/random primary-key probes for unchanged
            # snapshots. The conflict predicate below still rechecks current
            # state if another writer commits after this statement snapshot.
            "LEFT JOIN v2_current_quotes stored ON stored.fixture_id=q.fixture_id "
            "AND stored.market_id=q.market_id AND stored.period=q.period "
            "AND coalesce(stored.line,0)=coalesce(q.line,0) AND stored.selection=q.selection "
            "WHERE stored.id IS NULL OR (" + quote_update_condition("stored", "q") + ") "
            "ON CONFLICT(fixture_id,market_id,period,(COALESCE(line,0)),selection) DO UPDATE SET market_name=EXCLUDED.market_name,market_kind=EXCLUDED.market_kind,line=EXCLUDED.line,"
            "price=EXCLUDED.price,source_updated_at=EXCLUDED.source_updated_at,received_at=EXCLUDED.received_at,"
            "source_time_quality=EXCLUDED.source_time_quality,status=EXCLUDED.status,play_id=EXCLUDED.play_id,"
            "market_instance_id=EXCLUDED.market_instance_id,selection_id=EXCLUDED.selection_id,is_main=EXCLUDED.is_main,score_basis=EXCLUDED.score_basis "
            "WHERE " + quote_update_condition("v2_current_quotes", "EXCLUDED"),
            quote_payload,
        )
    if closures:
        await conn.executemany(
            "UPDATE v2_current_quotes q SET status='closed',received_at=$6 WHERE fixture_id=$1 "
            "AND NOT EXISTS (SELECT 1 FROM unnest($2::text[],$3::text[],$4::numeric[],$5::text[]) "
            "AS kept(market_id,period,line,selection) WHERE q.market_id=kept.market_id AND q.period=kept.period "
            "AND q.selection=kept.selection AND (q.line IS NOT DISTINCT FROM kept.line OR "
            "q.market_kind='winner' AND COALESCE(q.line,0)=0 AND COALESCE(kept.line,0)=0)) "
            "AND received_at <= $6 AND status<>'closed'",
            closures,
        )
    if finished:
        # Terminal fixtures remain in periodic snapshots. Avoid generating a new
        # PostgreSQL tuple/WAL record for every already-closed historical quote.
        await conn.execute("UPDATE v2_current_quotes SET status='closed' WHERE fixture_id=ANY($1::text[]) AND status<>'closed'", finished)
    return len(accepted_sources)


async def ingest_fixture(conn, item) -> bool:
    return bool(await ingest_fixture_batch(conn, [item]))
