"""Read-only, attempt-scoped checks for premature agent conclusions."""
from __future__ import annotations

import json
import re

DETAIL_LIMIT = 100


def mentioned_fixtures(content: str) -> set[str]:
    # A mention only requests verification; it can never authorize a binding.
    return set(re.findall(r'(?<![\w:])leyu:\d{1,24}(?!\w)', content[:20000]))


async def completion_issues(conn, job_id, attempt_id, mentioned: set[str]) -> dict:
    row = await conn.fetchrow(
        "WITH unfinished AS ("
        " SELECT s.search_key,s.team_query,s.phase,s.competition_query,s.from_utc,s.to_utc,"
        " s.expected_cursor AS cursor,(SELECT t.payload->'scope' FROM v2_analysis_traces t "
        " WHERE t.job_id=s.job_id AND t.trace_kind='mcp_search_matches' "
        " AND t.payload->>'attempt_id'=($2::uuid)::text AND t.payload->>'search_key'=s.search_key "
        " AND t.payload->>'status'='succeeded' ORDER BY t.sequence_no DESC LIMIT 1) AS scope "
        " FROM v2_match_searches s WHERE s.job_id=$1 AND s.attempt_id=$2 AND NOT s.complete),"
        " unbound AS (SELECT m.fixture_id,(SELECT jsonb_build_object('search_key',s.search_key,"
        " 'search_complete',s.complete,'fixture',c.candidate_data) "
        " FROM v2_match_search_candidates c JOIN v2_match_searches s "
        " ON s.job_id=c.job_id AND s.search_key=c.search_key "
        " WHERE c.job_id=$1 AND s.attempt_id=$2 AND c.fixture_id=m.fixture_id "
        " ORDER BY s.complete DESC,s.search_key LIMIT 1) AS candidate "
        " FROM unnest($3::text[]) AS m(fixture_id) WHERE NOT EXISTS("
        " SELECT 1 FROM v2_review_fixture_bindings b WHERE b.job_id=$1 AND b.attempt_id=$2 "
        " AND b.fixture_id=m.fixture_id)) "
        " SELECT (SELECT count(*) FROM unfinished) AS unfinished_count,"
        " (SELECT jsonb_agg(x) FROM (SELECT * FROM unfinished ORDER BY search_key LIMIT $4) x) AS unfinished,"
        " (SELECT count(*) FROM unbound) AS unbound_count,"
        " (SELECT jsonb_agg(x) FROM (SELECT * FROM unbound ORDER BY fixture_id LIMIT $4) x) AS unbound",
        job_id, attempt_id, sorted(mentioned), DETAIL_LIMIT)
    if not row['unfinished_count'] and not row['unbound_count']:
        return {}
    return {'unfinished_search_count': row['unfinished_count'], 'unfinished_searches': row['unfinished'] or [],
            'unbound_mention_count': row['unbound_count'], 'unbound_mentions': row['unbound'] or [],
            'detail_limit': DETAIL_LIMIT}


def continuation_prompt(issues: dict) -> str:
    return (
        '服务端工具证据核验尚未通过，请在本会话继续当前审核。沿用原 review_id、attempt_id、原图文和权限。'
        '先完成 unfinished_searches 中每个检索的准确 next cursor，保持原 scope（尤其 sport、phase、时间范围），'
        '连续分页直到无下一页，不要重启已完成检索。对模型提及但无绑定的比赛，只有原推荐意图且双方球队、'
        '联赛、时间确实唯一对应时，才用完整检索的 search_key 调用 resolve_match；已完场也可核验身份，'
        '但不能读取其实时盘口或补造历史推荐。candidate 仅供核查，不能作为自动绑定许可。'
        '候选不符或不唯一就明确未解决，不要为了通过检查而强行匹配。若有未核验项，禁止声称全部匹配。'
        '无需重新上传图片或重复已经确认的信号。以下是本任务当前 attempt 的服务端证据：\n'
        + json.dumps(issues, ensure_ascii=False, default=str))
