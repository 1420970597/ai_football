"""Read-only, attempt-scoped checks for premature agent conclusions."""
from __future__ import annotations

import json
import re
import unicodedata

from .review_intent_retry import intent_retry_ctes, intent_retry_projection

DETAIL_LIMIT = 100


def tool_unavailability_claim(content: str) -> bool:
    """A negative completeness hint; never evidence for a match or tool grant."""
    first = re.split(r'\n\s*\n', content.strip()[:1600], maxsplit=1)[0]
    if not re.match(r'^(?:我(?:当前)?(?:无法|不能|不会|没有|不具备)|(?:本次|当前|这个)(?:会话|环境)'
                    r'|I\s+(?:cannot|can\x27t|do not|don\x27t)|This\b|The current\b|No\b)', first, re.I):
        return False
    if not re.search(r'MCP|football|足球.*工具|赛事.*工具|盘口.*工具', first, re.I):
        return False
    return bool(re.search(
        r'(?:本次|当前|这个)(?:会话|环境).{0,80}(?:没有|未)(?:提供|开放|启用|配置).{0,40}工具'
        r'|我.{0,20}(?:无法|不能)(?:访问|使用).{0,45}(?:MCP|football|足球).{0,20}工具'
        r'|\bI\s+(?:cannot|can\x27t)\s+(?:use|access).{0,45}(?:MCP|football).{0,20}tools\b'
        r'|\bI\s+(?:do not|don\x27t)\s+have\s+access\s+to.{0,45}(?:MCP|football).{0,20}tools\b'
        r'|\b(?:this|current)\s+(?:session|environment).{0,40}(?:has no|does not provide).{0,30}tools\b'
        r'|^No\s+(?:football\s+)?MCP\s+tools\s+(?:are\s+)?available\b',
        first, re.I))


def tool_unavailability_issues() -> dict:
    return {'unfinished_search_count': 0, 'unfinished_searches': [],
            'unbound_mention_count': 0, 'unbound_mentions': [], 'detail_limit': DETAIL_LIMIT,
            'tool_availability_unverified': True}


async def tool_recovery_pending(conn, job_id, attempt_id) -> bool:
    """Only real successful MCP evidence after this attempt's last alert counts."""
    return bool(await conn.fetchval(
        "WITH alert AS (SELECT max(sequence_no) AS sequence_no FROM v2_analysis_traces "
        "WHERE job_id=$1 AND payload->>'attempt_id'=($2::uuid)::text "
        "AND trace_kind='hermes_partial_result' "
        "AND payload->'completion_issues'->>'tool_availability_unverified'='true') "
        "SELECT alert.sequence_no IS NOT NULL AND NOT EXISTS(SELECT 1 FROM v2_analysis_traces t "
        "WHERE t.job_id=$1 AND t.payload->>'attempt_id'=($2::uuid)::text "
        "AND t.sequence_no>alert.sequence_no AND t.payload->>'status'='succeeded' "
        "AND t.trace_kind=ANY($3::text[])) FROM alert",
        job_id, attempt_id,
        ['mcp_search_matches', 'mcp_resolve_match', 'mcp_list_match_markets', 'mcp_record_expert_signals']))


def unfinished_searches_cte() -> str:
    """Same attempt-scoped pagination evidence for final and live checks."""
    return (
        "unfinished AS ("
        " SELECT s.search_key,s.team_query,s.phase,s.competition_query,s.from_utc,s.to_utc,"
        " s.expected_cursor AS cursor,(SELECT t.payload->'scope' FROM v2_analysis_traces t "
        " WHERE t.job_id=s.job_id AND t.trace_kind='mcp_search_matches' "
        " AND t.payload->>'attempt_id'=($2::uuid)::text AND t.payload->>'search_key'=s.search_key "
        " AND t.payload->>'status'='succeeded' ORDER BY t.sequence_no DESC LIMIT 1) AS scope "
        " FROM v2_match_searches s WHERE s.job_id=$1 AND s.attempt_id=$2 AND NOT s.complete)"
    )


def pagination_issues(count: int, searches: list) -> dict:
    if not count:
        return {}
    return {'unfinished_search_count': count, 'unfinished_searches': searches or [],
            'unbound_mention_count': 0, 'unbound_mentions': [], 'detail_limit': DETAIL_LIMIT}


def mentioned_fixtures(content: str) -> set[str]:
    # A mention only requests verification; it can never authorize a binding.
    return set(re.findall(r'(?<![\w:])leyu:\d{1,24}(?!\w)', content[:20000]))


def named_identity_claims(contents) -> str:
    """A negative verification hint, never permission to bind a candidate."""
    claims = []
    for content in contents[:3]:
        text = content[:20000]
        if re.search(r'已(?:经)?[^，。；\n]{0,16}(?:匹配|识别|找到|确认|定位)'
                     r'|(?:匹配|识别|找到|确认|定位)(?:成功|完成)', text):
            claims.append(text)
    return '\n'.join(claims)


def explicit_text_pick(content: str) -> bool:
    """Request verification of a plain instruction; never create a pick."""
    if not isinstance(content, str) or len(content) > 100:
        return False
    text = unicodedata.normalize('NFKC', content)
    number = r'\d{1,3}(?:\.\d{1,3})?(?:\s*/\s*\d{1,3}(?:\.\d{1,3})?)?'
    return bool(re.fullmatch(
        rf'\s*(?:追|跟|上)\s*(?:{number}\s*[大小](?:球)?|[大小](?:球)?\s*{number})\s*[。!]*\s*', text))


async def completion_issues(conn, job_id, attempt_id, mentioned: set[str], content: str = '',
                            require_intent_review: bool = False) -> dict:
    row = await conn.fetchrow(
        "WITH " + unfinished_searches_cte() + "," + intent_retry_ctes() + ","
        " mentions AS (SELECT unnest($3::text[]) AS fixture_id UNION "
        " SELECT c.fixture_id FROM v2_match_search_candidates c JOIN v2_match_searches s "
        " ON s.job_id=c.job_id AND s.search_key=c.search_key "
        " WHERE $5::text<>'' AND c.job_id=$1 AND s.attempt_id=$2 "
        " AND length(c.candidate_data->>'home_team')>1 "
        " AND length(c.candidate_data->>'away_team')>1 "
        " AND c.candidate_data->>'home_team'<>c.candidate_data->>'away_team' "
        " AND strpos($5,c.candidate_data->>'home_team')>0 "
        " AND strpos($5,c.candidate_data->>'away_team')>0),"
        " unbound AS (SELECT m.fixture_id,(SELECT jsonb_build_object('search_key',s.search_key,"
        " 'search_complete',s.complete,'fixture',c.candidate_data) "
        " FROM v2_match_search_candidates c JOIN v2_match_searches s "
        " ON s.job_id=c.job_id AND s.search_key=c.search_key "
        " WHERE c.job_id=$1 AND s.attempt_id=$2 AND c.fixture_id=m.fixture_id "
        " ORDER BY s.complete DESC,s.search_key LIMIT 1) AS candidate "
        " FROM mentions m WHERE NOT EXISTS("
        " SELECT 1 FROM v2_review_fixture_bindings b WHERE b.job_id=$1 AND b.attempt_id=$2 "
        " AND b.fixture_id=m.fixture_id)) "
        " SELECT (SELECT count(*) FROM unfinished) AS unfinished_count,"
        " (SELECT jsonb_agg(x) FROM (SELECT * FROM unfinished ORDER BY search_key LIMIT $4) x) AS unfinished,"
        " (SELECT count(*) FROM unbound) AS unbound_count,"
        " (SELECT jsonb_agg(x) FROM (SELECT * FROM unbound ORDER BY fixture_id LIMIT $4) x) AS unbound,"
        " ($6::boolean AND NOT EXISTS(SELECT 1 FROM v2_review_fixture_bindings b "
        " WHERE b.job_id=$1 AND b.attempt_id=$2)) AS intent_unverified," + intent_retry_projection(),
        job_id, attempt_id, sorted(mentioned), DETAIL_LIMIT,
        content[:60002] if named_identity_claims([content]) else '', require_intent_review)
    if (not row['unfinished_count'] and not row['unbound_count'] and not row['intent_unverified']
            and not row['line_intent_retry_count']):
        return {}
    result = {'unfinished_search_count': row['unfinished_count'], 'unfinished_searches': row['unfinished'] or [],
            'unbound_mention_count': row['unbound_count'], 'unbound_mentions': row['unbound'] or [],
            'detail_limit': DETAIL_LIMIT}
    if row['intent_unverified']:
        result['text_intent_unverified'] = True
    if row['line_intent_retry_count']:
        result['line_intent_retry_count'] = row['line_intent_retry_count']
        result['line_intent_retries'] = row['line_intent_retries'] or []
    return result


def continuation_prompt(issues: dict) -> str:
    line_hint = (
        '所提交quote_id的实际盘口与该项原始证据不一致，当前任务已读取同比赛/玩法/期间/方向的精确选项。'
        '请核对原图文、source_intent及matching_read_quote_ids并纠正编号；不得改写原意迁就邻近盘口，'
        '不得比较专家倍率，不重新识别成另一期间或方向。候选编号仅是已读提示，不批准录入；'
        '必要时重读以获取当前报价，实际不可用或无法确认则保留未解决。'
        if issues.get('line_intent_retry_count') else '')
    intent_hint = (
        '原正文是明确的新增追大小指令，目前没有当前审核的工具身份绑定。请结合会话原图文继续核验；'
        '不要求图片高亮、已下注或注单确认。原文指令本身不批准绑定，仍须完整检索并核对双方、'
        '梯队、时间和唯一性；完场也可确认身份但不得读取实时盘口或补录历史推荐。'
        if issues.get('text_intent_unverified') else '')
    tool_hint = (
        '本项目配置了football与football_parallel受限MCP工具。上轮声称工具不可用，尚无后续成功调用证据；'
        '请使用当前实际提供的赛事检索及盘口工具核验原图文，不能仅改口说已完成。'
        '实际调用失败则保留错误，不自行扩大权限、虚构工具结果或猜测编号。'
        if issues.get('tool_availability_unverified') else '')
    return line_hint + intent_hint + tool_hint + (
        '服务端工具证据核验尚未通过，请在本会话继续当前审核。沿用原 review_id、attempt_id、原图文和权限。'
        '先完成 unfinished_searches 中每个检索的准确 next cursor，保持原 scope（尤其 sport、phase、时间范围），'
        '连续分页直到无下一页，不要重启已完成检索。对模型提及但无绑定的比赛，只有原推荐意图且双方球队、'
        '联赛、时间确实唯一对应时，才用完整检索的 search_key 调用 resolve_match；已完场也可核验身份，'
        '但不能读取其实时盘口或补造历史推荐。candidate 仅供核查，不能作为自动绑定许可。'
        '候选不符或不唯一就明确未解决，不要为了通过检查而强行匹配。若有未核验项，禁止声称全部匹配。'
        '无需重新上传图片或重复已经确认的信号。以下是本任务当前 attempt 的服务端证据：\n'
        + json.dumps(issues, ensure_ascii=False, default=str))
