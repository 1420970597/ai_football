"""Immutable search snapshots; source fragments widen retrieval, never identity."""
from __future__ import annotations

from datetime import timedelta
import re

from .chinese_names import sql_chinese_fold
from .review_tool_evidence import FIXTURE_FIELDS
from .team_matching import normalize_team_name, team_search_variants

_GENERIC_FRAGMENTS = frozenset({
    '青年', '青年队', '联队', '竞技', '体育', '足球', '大学', '城市', '联合',
    '预备', '后备', '女子', '女足', '学院', '学生', '俱乐部', '俱乐', '乐部',
})
_CANDIDATE_JSON = (
    'jsonb_build_object(' + ','.join("'" + field + "',f." + field for field in sorted(FIXTURE_FIELDS))
    + ", 'realtime_eligible', f.phase IN ('scheduled','live'), "
    + "'eligibility_reason', CASE WHEN f.phase IN ('scheduled','live') THEN NULL ELSE f.phase END)"
)
_INSERT_SNAPSHOT = (
    "WITH inserted AS (INSERT INTO v2_match_search_candidates(job_id,search_key,fixture_id,position,candidate_data) "
    "SELECT $1,$2,f.id,row_number() OVER(ORDER BY f.kickoff_at,f.id)," + _CANDIDATE_JSON
    + " || CASE WHEN $13::boolean THEN jsonb_build_object('retrieval_basis','source_fragment_candidate') "
    "ELSE '{}'::jsonb END FROM v2_fixtures f "
    "WHERE f.phase IN ('live','scheduled','finished','cancelled') AND ($3='all' OR f.phase=$3) "
    "AND (lower(f.home_team) LIKE $4 OR lower(f.away_team) LIKE $4 "
    "OR regexp_replace(translate(lower(f.home_team),$6,$7),'[^[:alnum:]]+',' ','g') LIKE ANY($5::text[]) "
    "OR regexp_replace(translate(lower(f.away_team),$6,$7),'[^[:alnum:]]+',' ','g') LIKE ANY($5::text[]) "
    "OR EXISTS (SELECT 1 FROM unnest(f.home_aliases || f.away_aliases) alias WHERE "
    "lower(btrim(alias))=$8 OR regexp_replace(translate(lower(btrim(alias)),$6,$7),'[^[:alnum:]]+',' ','g') LIKE ANY($5::text[]))) "
    "AND ($9='' OR regexp_replace(translate(lower(f.competition_name),$6,$7),"
    "'[^[:alnum:]]+',' ','g') LIKE '%'||$9||'%') "
    "AND ($10::timestamptz IS NULL OR f.kickoff_at >= $10) AND ($11::timestamptz IS NULL OR f.kickoff_at <= $11) "
    "AND ($12::text IS NULL OR f.sport=$12) RETURNING 1) SELECT count(*) FROM inserted"
)


def source_fragments(query, start, end):
    if start is None or end is None or not timedelta(0) < end-start <= timedelta(days=7):
        return ()
    fragments = {}
    for run in re.findall(r'[\u3400-\u9fff]{4,}', normalize_team_name(query)):
        # A source name such as 德瓦联合万丹 shares its club prefix with
        # 德瓦FC, but neither its suffix nor any trigram does. These are
        # retrieval fragments only; resolution still verifies both teams.
        for value in (run[-2:], run[:2], *(run[i:i+3] for i in range(len(run)-2))):
            if value not in _GENERIC_FRAGMENTS:
                fragments.setdefault(value, None)
    return tuple(fragments)[:12]


async def snapshot_candidates(conn, *, review_id, search_key, phase, team_query,
                              competition, sport, from_utc, to_utc):
    name_source = 'áàâäãåéèêëíìîïóòôöõúùûüýÿçñ'
    name_target = 'aaaaaaeeeeiiiiooooouuuuyycn'
    chinese_source, chinese_target = sql_chinese_fold(team_query+' '+competition)
    args = (review_id, search_key, phase, '%'+team_query.strip().casefold()+'%',
            ['%'+v+'%' for v in team_search_variants(team_query)],
            name_source+chinese_source, name_target+chinese_target, team_query.strip().casefold(),
            normalize_team_name(competition), from_utc, to_utc, sport, False)
    count = await conn.fetchval(_INSERT_SNAPSHOT, *args)
    fragments = source_fragments(team_query, from_utc, to_utc) if count == 0 else ()
    if fragments:
        widened = (*args[:4], ['%'+v+'%' for v in fragments], *args[5:12], True)
        count = await conn.fetchval(_INSERT_SNAPSHOT, *widened)
    return count
