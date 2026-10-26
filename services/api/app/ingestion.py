"""规范化事实入库；不产生推荐或覆盖较新的报价。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from fastapi import HTTPException


async def ingest_fixture(conn, item) -> bool:
    now = datetime.now(timezone.utc)
    if item.received_at > now + timedelta(seconds=5) or (item.source_updated_at and item.source_updated_at > now + timedelta(seconds=5)):
        raise HTTPException(status_code=422, detail="fixture timestamp is in the future")
    row = await conn.fetchrow(
        "INSERT INTO v2_fixtures(id,source_id,sport,competition_id,competition_name,home_team_id,home_team,home_aliases,"
        "away_team_id,away_team,away_aliases,kickoff_at,phase,match_state,home_score,away_score,clock_minute,"
        "source_updated_at,received_at,source_time_quality,half_home_score,half_away_score) "
        "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22) "
        "ON CONFLICT(source_id) DO UPDATE SET sport=EXCLUDED.sport,competition_id=EXCLUDED.competition_id,"
        "competition_name=EXCLUDED.competition_name,home_team_id=EXCLUDED.home_team_id,home_team=EXCLUDED.home_team,"
        "home_aliases=EXCLUDED.home_aliases,away_team_id=EXCLUDED.away_team_id,away_team=EXCLUDED.away_team,"
        "away_aliases=EXCLUDED.away_aliases,kickoff_at=EXCLUDED.kickoff_at,phase=EXCLUDED.phase,"
        "match_state=EXCLUDED.match_state,home_score=EXCLUDED.home_score,away_score=EXCLUDED.away_score,"
        "clock_minute=EXCLUDED.clock_minute,source_updated_at=EXCLUDED.source_updated_at,received_at=EXCLUDED.received_at,"
        "source_time_quality=EXCLUDED.source_time_quality,half_home_score=EXCLUDED.half_home_score,half_away_score=EXCLUDED.half_away_score "
        "WHERE coalesce(EXCLUDED.source_updated_at,EXCLUDED.received_at)>=coalesce(v2_fixtures.source_updated_at,v2_fixtures.received_at) RETURNING id",
        item.id, item.source_id, item.sport.lower(), item.competition_id, item.competition_name, item.home_team_id, item.home_team,
        [v.strip().casefold() for v in item.home_aliases if v.strip()], item.away_team_id, item.away_team,
        [v.strip().casefold() for v in item.away_aliases if v.strip()], item.kickoff_at, item.phase, item.match_state,
        item.home_score, item.away_score, item.clock_minute, item.source_updated_at, item.received_at, item.source_time_quality,
        item.half_home_score, item.half_away_score,
    )
    if not row:
        return False
    fixture_id = row["id"]
    retained = []
    for quote in item.markets:
        if quote.received_at > now + timedelta(seconds=5) or (quote.source_updated_at and quote.source_updated_at > now + timedelta(seconds=5)):
            raise HTTPException(status_code=422, detail="quote timestamp is in the future")
        inserted = await conn.fetchrow(
            "INSERT INTO v2_current_quotes(id,fixture_id,market_id,market_name,period,market_kind,line,selection,price,source_updated_at,"
            "received_at,source_time_quality,status,play_id,market_instance_id,selection_id,is_main,score_basis) "
            "VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18) "
            "ON CONFLICT(fixture_id,market_id,period,line,selection) DO UPDATE SET market_name=EXCLUDED.market_name,market_kind=EXCLUDED.market_kind,"
            "price=EXCLUDED.price,source_updated_at=EXCLUDED.source_updated_at,received_at=EXCLUDED.received_at,"
            "source_time_quality=EXCLUDED.source_time_quality,status=EXCLUDED.status,play_id=EXCLUDED.play_id,"
            "market_instance_id=EXCLUDED.market_instance_id,selection_id=EXCLUDED.selection_id,is_main=EXCLUDED.is_main,score_basis=EXCLUDED.score_basis "
            "WHERE coalesce(EXCLUDED.source_updated_at,EXCLUDED.received_at)>=coalesce(v2_current_quotes.source_updated_at,v2_current_quotes.received_at) RETURNING id",
            uuid.uuid4(), fixture_id, quote.market_id, quote.market_name, quote.period, quote.kind, quote.line,
            quote.selection.casefold(), quote.price, quote.source_updated_at, quote.received_at, quote.source_time_quality,
            quote.status, quote.play_id, quote.market_instance_id, quote.selection_id, quote.is_main, quote.score_basis,
        )
        if inserted:
            retained.append(inserted["id"])
        else:
            existing = await conn.fetchval(
                "SELECT id FROM v2_current_quotes WHERE fixture_id=$1 AND market_id=$2 AND period=$3 AND line=$4 AND selection=$5",
                fixture_id, quote.market_id, quote.period, quote.line, quote.selection.casefold(),
            )
            if existing:
                retained.append(existing)
    if item.markets_complete:
        await conn.execute(
            "UPDATE v2_current_quotes SET status='closed',received_at=$3 WHERE fixture_id=$1 "
            "AND NOT(id=ANY($2::uuid[])) AND received_at <= $3 AND status<>'closed'", fixture_id, retained, item.received_at,
        )
    if item.phase in {"finished", "cancelled"}:
        await conn.execute("UPDATE v2_current_quotes SET status='closed' WHERE fixture_id=$1", fixture_id)
    return True
