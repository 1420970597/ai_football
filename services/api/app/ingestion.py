"""规范化事实批量入库；远端往返次数不随报价数量增长。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import HTTPException


async def ingest_fixture_batch(conn, items) -> int:
    now = datetime.now(timezone.utc)
    fixtures = {}
    for item in items:
        if item.received_at > now + timedelta(seconds=5) or (item.source_updated_at and item.source_updated_at > now + timedelta(seconds=5)):
            raise HTTPException(status_code=422, detail="fixture timestamp is in the future")
        for quote in item.markets:
            if quote.received_at > now + timedelta(seconds=5) or (quote.source_updated_at and quote.source_updated_at > now + timedelta(seconds=5)):
                raise HTTPException(status_code=422, detail="quote timestamp is in the future")
        if item.source_id in fixtures:
            raise HTTPException(status_code=422, detail="duplicate fixture source in batch")
        fixtures[item.source_id] = item
    if not fixtures:
        return 0
    payload = []
    for item in fixtures.values():
        value = item.model_dump(mode="json", exclude={"markets", "markets_complete"})
        value["sport"] = item.sport.lower()
        for field in ("home_aliases", "away_aliases"):
            value[field] = [v.strip().casefold() for v in value[field] if v.strip()]
        payload.append(value)
    accepted = await conn.fetch(
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
        "RETURNING id,source_id", payload,
    )
    quote_rows, closures, finished = [], [], []
    for record in accepted:
        item = fixtures[record["source_id"]]
        fixture_id = record["id"]
        for quote in item.markets:
            quote_rows.append((
                uuid.uuid4(), fixture_id, quote.market_id, quote.market_name, quote.period, quote.kind, quote.line,
                quote.selection.casefold(), quote.price, quote.source_updated_at, quote.received_at, quote.source_time_quality,
                quote.status, quote.play_id, quote.market_instance_id, quote.selection_id, quote.is_main, quote.score_basis,
            ))
        if item.markets_complete:
            closures.append((fixture_id, [q.market_id for q in item.markets], [q.period for q in item.markets],
                             [q.line for q in item.markets], [q.selection.casefold() for q in item.markets], item.received_at))
        if item.phase in {"finished", "cancelled"}:
            finished.append(fixture_id)
    if quote_rows:
        await conn.executemany(
            "INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,period,market_kind,line,selection,price,source_updated_at,"
            "received_at,source_time_quality,status,play_id,market_instance_id,selection_id,is_main,score_basis) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18) "
            "ON CONFLICT(fixture_id,market_id,period,line,selection) DO UPDATE SET market_name=EXCLUDED.market_name,market_kind=EXCLUDED.market_kind,"
            "price=EXCLUDED.price,source_updated_at=EXCLUDED.source_updated_at,received_at=EXCLUDED.received_at,"
            "source_time_quality=EXCLUDED.source_time_quality,status=EXCLUDED.status,play_id=EXCLUDED.play_id,"
            "market_instance_id=EXCLUDED.market_instance_id,selection_id=EXCLUDED.selection_id,is_main=EXCLUDED.is_main,score_basis=EXCLUDED.score_basis "
            "WHERE coalesce(EXCLUDED.source_updated_at,EXCLUDED.received_at)>=coalesce(v2_current_quotes.source_updated_at,v2_current_quotes.received_at)",
            quote_rows,
        )
    if closures:
        await conn.executemany(
            "UPDATE v2_current_quotes q SET status='closed',received_at=$6 WHERE fixture_id=$1 "
            "AND NOT EXISTS (SELECT 1 FROM unnest($2::text[],$3::text[],$4::numeric[],$5::text[]) "
            "AS kept(market_id,period,line,selection) WHERE (q.market_id,q.period,q.line,q.selection)="
            "(kept.market_id,kept.period,kept.line,kept.selection)) AND received_at <= $6 AND status<>'closed'",
            closures,
        )
    if finished:
        await conn.execute("UPDATE v2_current_quotes SET status='closed' WHERE fixture_id=ANY($1::text[])", finished)
    return len(accepted)


async def ingest_fixture(conn, item) -> bool:
    return bool(await ingest_fixture_batch(conn, [item]))
