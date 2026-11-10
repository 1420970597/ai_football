"""Read-only, attempt-scoped checks for premature agent conclusions."""
from __future__ import annotations

import json
import re

from .review_intent_retry import intent_retry_ctes, intent_retry_projection
from .review_phase_claims import current_phase_claims, named_phase_claims
from .review_line_claims import split_line_claim_conflicts
from .review_intent_claims import explicit_text_pick, explicit_visual_instruction, source_intent_denial

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
    # A claim only requests verification; it can never authorize a binding.
    # Explicitly excluded, identity-mismatched candidates are not pick claims.
    # An old positive claim remains in the caller's attempt-wide union.
    mentioned = set()
    for clause in re.split(r'[\n。；;!！?？]+', content[:20000]):
        rejected = re.search(r'不作为绑定|不予绑定|明确排除|排除[该此](?:候选|比赛)', clause)
        mismatch = re.search(r'[与和].{0,100}(?:原文|原推荐).{0,100}(?:不符|不同)'
                             r'|无关|非原文|不是原文|不属于原推荐', clause)
        positive = re.search(r'(?:已(?:经)?|确认)[^，,:：]{0,16}(?:绑定|匹配|识别|定位)', clause)
        if rejected and mismatch and not positive:
            continue
        # Chinese prose next to an ASCII provider ID is a valid boundary.
        mentioned.update(re.findall(r'(?<![A-Za-z0-9_:])leyu:[0-9]{1,24}(?![A-Za-z0-9_:])', clause))
    return mentioned


def _named_claim_clauses(text):
    for clause in re.split(r'[\n。；;!！?？]+', text):
        if re.search(r'已(?:经)?[^，。；\n]{0,16}(?:匹配|识别|找到|确认|定位)'
                     r'|(?:匹配|识别|找到|确认|定位)(?:成功|完成)', clause):
            yield clause
            continue
        system_pair = re.search(r'对应(?:的)?系统(?:比赛|对阵)(?:为|是|[:：])', clause)
        if system_pair and not re.search(
                r'(?:无法|不能|未能|尚未|尚不能|没有|不可|不要|未)[^，,:：]{0,16}$',
                clause[:system_pair.start()]):
            # This is a request for the existing attempt-scoped negative check,
            # never a binding or evidence inferred from the model's wording.
            yield clause


def named_identity_claims(contents) -> str:
    """Preserve claim boundaries; never authorize a binding from model prose."""
    return '\n'.join(clause for content in contents[:3]
                     for clause in _named_claim_clauses(content[:20000]))


async def completion_issues(conn, job_id, attempt_id, mentioned: set[str], content: str = '',
                            require_intent_review: bool = False, *, final_content: str = '',
                            require_visual_instruction_review: bool = False) -> dict:
    display_conflicts = split_line_claim_conflicts(final_content)
    visual_intent_conflict = require_visual_instruction_review and source_intent_denial(final_content)
    row = await conn.fetchrow(
        "WITH " + unfinished_searches_cte() + "," + intent_retry_ctes() + ","
        " named_claims AS (SELECT unnest(string_to_array($5::text,chr(10))) AS content),"
        " mentions AS (SELECT unnest($3::text[]) AS fixture_id UNION "
        " SELECT c.fixture_id FROM v2_match_search_candidates c JOIN v2_match_searches s "
        " ON s.job_id=c.job_id AND s.search_key=c.search_key "
        " WHERE $5::text<>'' AND c.job_id=$1 AND s.attempt_id=$2 "
        " AND length(c.candidate_data->>'home_team')>1 "
        " AND length(c.candidate_data->>'away_team')>1 "
        " AND c.candidate_data->>'home_team'<>c.candidate_data->>'away_team' "
        " AND EXISTS(SELECT 1 FROM named_claims n "
        " WHERE strpos(n.content,c.candidate_data->>'home_team')>0 "
        " AND strpos(n.content,c.candidate_data->>'away_team')>0) "
        " AND NOT EXISTS(SELECT 1 FROM v2_review_fixture_bindings b "
        " JOIN v2_match_search_candidates bc ON bc.job_id=b.job_id "
        " AND bc.search_key=b.search_key AND bc.fixture_id=b.fixture_id "
        " WHERE b.job_id=c.job_id AND b.attempt_id=$2 "
        " AND bc.candidate_data->>'home_team'=c.candidate_data->>'home_team' "
        " AND bc.candidate_data->>'away_team'=c.candidate_data->>'away_team')),"
        " unbound AS (SELECT m.fixture_id,(SELECT jsonb_build_object('search_key',s.search_key,"
        " 'search_complete',s.complete,'fixture',c.candidate_data) "
        " FROM v2_match_search_candidates c JOIN v2_match_searches s "
        " ON s.job_id=c.job_id AND s.search_key=c.search_key "
        " WHERE c.job_id=$1 AND s.attempt_id=$2 AND c.fixture_id=m.fixture_id "
        " ORDER BY s.complete DESC,s.search_key LIMIT 1) AS candidate "
        " FROM mentions m WHERE NOT EXISTS("
        " SELECT 1 FROM v2_review_fixture_bindings b WHERE b.job_id=$1 AND b.attempt_id=$2 "
        " AND b.fixture_id=m.fixture_id)),"
        " phase_claims AS (SELECT * FROM jsonb_to_recordset($7::jsonb) "
        " AS c(fixture_id text,claimed_phase text,content text)),"
        " phase_attributions AS (SELECT fixture_id,claimed_phase FROM phase_claims WHERE fixture_id IS NOT NULL "
        " UNION ALL SELECT min(b.fixture_id),c.claimed_phase FROM phase_claims c "
        " JOIN v2_review_fixture_bindings b ON b.job_id=$1 AND b.attempt_id=$2 "
        " JOIN v2_match_search_candidates bc ON bc.job_id=b.job_id "
        " AND bc.search_key=b.search_key AND bc.fixture_id=b.fixture_id "
        " WHERE c.fixture_id IS NULL AND length(bc.candidate_data->>'home_team')>1 "
        " AND length(bc.candidate_data->>'away_team')>1 "
        " AND bc.candidate_data->>'home_team'<>bc.candidate_data->>'away_team' "
        " AND strpos(c.content,bc.candidate_data->>'home_team')>0 "
        " AND strpos(c.content,bc.candidate_data->>'away_team')>0 "
        " GROUP BY c.content,c.claimed_phase HAVING count(DISTINCT b.fixture_id)=1),"
        " phase_conflicts AS (SELECT DISTINCT c.fixture_id,c.claimed_phase,f.phase AS actual_phase "
        " FROM phase_attributions c "
        " JOIN v2_review_fixture_bindings b ON b.fixture_id=c.fixture_id "
        " AND b.job_id=$1 AND b.attempt_id=$2 JOIN v2_fixtures f ON f.id=b.fixture_id "
        " WHERE f.phase<>c.claimed_phase) "
        " SELECT (SELECT count(*) FROM unfinished) AS unfinished_count,"
        " (SELECT jsonb_agg(x) FROM (SELECT * FROM unfinished ORDER BY search_key LIMIT $4) x) AS unfinished,"
        " (SELECT count(*) FROM unbound) AS unbound_count,"
        " (SELECT jsonb_agg(x) FROM (SELECT * FROM unbound ORDER BY fixture_id LIMIT $4) x) AS unbound,"
        " (SELECT count(*) FROM phase_conflicts) AS phase_conflict_count,"
        " (SELECT jsonb_agg(x) FROM (SELECT * FROM phase_conflicts "
        " ORDER BY fixture_id,claimed_phase LIMIT $4) x) AS phase_conflicts,"
        " ($6::boolean AND NOT EXISTS(SELECT 1 FROM v2_review_fixture_bindings b "
        " WHERE b.job_id=$1 AND b.attempt_id=$2)) AS intent_unverified," + intent_retry_projection(),
        job_id, attempt_id, sorted(mentioned), DETAIL_LIMIT,
        '\n'.join(_named_claim_clauses(content[:60002])), require_intent_review,
        current_phase_claims(final_content) + named_phase_claims(final_content))
    if (not row['unfinished_count'] and not row['unbound_count'] and not row['intent_unverified']
            and not row['line_intent_retry_count'] and not row['phase_conflict_count'] and not display_conflicts
            and not visual_intent_conflict):
        return {}
    result = {'unfinished_search_count': row['unfinished_count'], 'unfinished_searches': row['unfinished'] or [],
            'unbound_mention_count': row['unbound_count'], 'unbound_mentions': row['unbound'] or [],
            'detail_limit': DETAIL_LIMIT}
    if row['intent_unverified']:
        result['text_intent_unverified'] = True
    if visual_intent_conflict:
        result['source_visual_intent_conflict'] = True
    if row['line_intent_retry_count']:
        result['line_intent_retry_count'] = row['line_intent_retry_count']
        result['line_intent_retries'] = row['line_intent_retries'] or []
    if row['phase_conflict_count']:
        result['phase_claim_conflict_count'] = row['phase_conflict_count']
        result['phase_claim_conflicts'] = row['phase_conflicts'] or []
    if display_conflicts:
        result['display_line_conflict_count'] = len(display_conflicts)
        result['display_line_conflicts'] = display_conflicts[:DETAIL_LIMIT]
    return result


def continuation_prompt(issues: dict) -> str:
    visual_hint = (
        '原正文明确“可单可串”且附有原图，最终说明却否认新增推荐意图。请重新核验本会话原图文；'
        '不能仅因图片是已成交注单或正文没有再次跟进，就忽略该明确指令。'
        '本检查不推断图片球队、盘口或方向，也不批准绑定、报价或录入。'
        '已有唯一身份绑定和完整零报价结果应保留：有推荐意图但无报价仍须保持未解决，'
        '不重复搜索、读盘或录入已确认项；不能为通过检查补造报价或补投历史。'
        if issues.get('source_visual_intent_conflict') else '')
    display_hint = (
        '最终说明将同一明确分盘换算成了矛盾的让球幅度。按display_line_conflicts纠正说明，'
        '保留原始分盘文字，不能以模型自算替代工具selection_line或原生盘口。'
        '这不是新的比赛/盘口/方向确认或录入许可；若无其他问题只改说明，不重复搜索、读盘或录入。'
        if issues.get('display_line_conflict_count') else '')
    phase_hint = (
        '最终说明中的当前比赛阶段与服务端当前事实不一致，请按phase_claim_conflicts的actual_phase纠正说明。'
        '这不是新的推荐或录入许可；保留既有身份绑定、盘口及已接受信号。'
        '若其余检查均通过，只修正说明，不重新搜索、重复读盘或重复录入。'
        if issues.get('phase_claim_conflict_count') else '')
    line_hint = (
        '所提交quote_id的实际盘口与该项原始证据不一致，当前任务已读取同比赛/玩法/期间/方向的精确选项。'
        '请核对原图文、source_intent及matching_read_quote_ids并纠正编号；不得改写原意迁就邻近盘口，'
        '不得比较专家倍率，不重新识别成另一期间或方向。候选编号仅是已读提示，不批准录入；'
        '必要时重读以获取当前报价，实际不可用或无法确认则保留未解决。'
        if issues.get('line_intent_retry_count') else '')
    intent_hint = (
        '原正文有明确的推荐或追线指令，目前没有当前审核的工具身份绑定。请结合完整原图文逐项继续核验；'
        '不要求图片高亮、已下注或注单确认。原文指令本身不批准绑定，仍须完整检索并核对双方、'
        '梯队、时间和唯一性；完场也可确认身份但不得读取实时盘口或补录历史推荐。'
        if issues.get('text_intent_unverified') else '')
    tool_hint = (
        '本项目配置了football与football_parallel受限MCP工具。上轮声称工具不可用，尚无后续成功调用证据；'
        '请使用当前实际提供的赛事检索及盘口工具核验原图文，不能仅改口说已完成。'
        '实际调用失败则保留错误，不自行扩大权限、虚构工具结果或猜测编号。'
        if issues.get('tool_availability_unverified') else '')
    return visual_hint + display_hint + phase_hint + line_hint + intent_hint + tool_hint + (
        '服务端工具证据核验尚未通过，请在本会话继续当前审核。沿用原 review_id、attempt_id、原图文和权限。'
        '先完成 unfinished_searches 中每个检索的准确 next cursor，保持原 scope（尤其 sport、phase、时间范围），'
        '连续分页直到无下一页，不要重启已完成检索。对模型提及但无绑定的比赛，只有原推荐意图且双方球队、'
        '联赛、时间确实唯一对应时，才用完整检索的 search_key 调用 resolve_match；已完场也可核验身份，'
        '但不能读取其实时盘口或补造历史推荐。candidate 仅供核查，不能作为自动绑定许可。'
        '候选不符或不唯一就明确未解决，不要为了通过检查而强行匹配。若有未核验项，禁止声称全部匹配。'
        '无需重新上传图片或重复已经确认的信号。以下是本任务当前 attempt 的服务端证据：\n'
        + json.dumps(issues, ensure_ascii=False, default=str))
