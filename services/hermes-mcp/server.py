from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from contextvars import ContextVar
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field, model_validator

API_URL = os.environ.get("PLATFORM_INTERNAL_API", "http://api:8080/internal/mcp").rstrip("/")
API_TOKEN = os.environ.get("EXPERT_MCP_TOKEN", "")
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8090"))
if not API_TOKEN:
    raise RuntimeError("EXPERT_MCP_TOKEN is required")

client = httpx.AsyncClient(
    timeout=httpx.Timeout(20.0, connect=2.0), trust_env=False,
    limits=httpx.Limits(max_connections=32, max_keepalive_connections=8),
    headers={"Authorization": f"Bearer {API_TOKEN}"},
)


def market_table_view(value):
    """Factor repeated quote keys only; every row remains exactly recoverable."""
    if not isinstance(value, dict):
        return value
    if isinstance(value.get('items'), list) and all(
            isinstance(item, dict) and 'index' in item for item in value['items']):
        return value | {'items': [item | {'result': market_table_view(item['result'])}
            if isinstance(item.get('result'), dict) else item for item in value['items']]}
    if isinstance(value.get('market_result'), dict):
        child = market_table_view(value['market_result'])
        if child is not value['market_result']:
            return value | {'market_result': child}
    quotes = value.get('markets')
    if (not isinstance(quotes, list) or not 8 <= len(quotes) <= 2000
            or 'market_table' in value or not isinstance(quotes[0], dict)):
        return value
    columns = sorted(quotes[0])
    if not columns or 'quote_id' not in columns:
        return value
    keys = set(columns)
    if any(not isinstance(row, dict) or set(row) != keys
           or not isinstance(row['quote_id'], str) or not row['quote_id'] for row in quotes):
        return value
    table = {'encoding': 'column-rows-v1', 'columns': columns,
             'rows': [[row[column] for column in columns] for row in quotes],
             'decoding': '每行按columns逐列对应一个完整盘口；同一行的quote_id、期间、线值、方向和可录入状态必须一起核对，不跨行混合。'}
    candidate = {key: item for key, item in value.items() if key != 'markets'} | {'market_table': table}
    if len(json.dumps(candidate, ensure_ascii=False, separators=(',', ':'), allow_nan=False)) >= len(
            json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)):
        return value
    return candidate


def compact_tool_result(result):
    """Keep native metadata, with an explicit opt-in lossless quote table view."""
    if not isinstance(result, CallToolResult) or result.is_error or len(result.content) != 1:
        return result
    content = result.content[0]
    if not isinstance(content, TextContent):
        return result
    try:
        value = json.loads(content.text)
        if not isinstance(value, dict):
            return result
        if result.structured_content is not None and value != result.structured_content:
            return result
        if compact_market_tables.get():
            value = market_table_view(value)
        text = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        return result
    if text == content.text:
        return result
    updates = {'content': [content.model_copy(update={'text': text})]}
    if result.structured_content is not None:
        updates['structured_content'] = value
    return result.model_copy(update=updates)


compact_descriptions: ContextVar[bool] = ContextVar('compact_tool_descriptions', default=False)
terminal_review: ContextVar[dict | None] = ContextVar('terminal_review', default=None)
compact_market_tables: ContextVar[bool] = ContextVar('compact_market_tables', default=False)
explicit_market_intent: ContextVar[bool] = ContextVar('explicit_market_intent', default=False)
budget_descriptions: ContextVar[bool] = ContextVar('budget_tool_descriptions', default=False)
SourcePeriods = Annotated[list[Literal['full_time','first_half','second_half']] | None,
    Field(max_length=3,description='首次读盘前从原文/所选图片明确的推荐期间清单；未知填[]，单项如[first_half]，多期间图片须在首次一次列全。读盘period只是筛选，不证明来源；读盘后不得扩充清单或按库存补期间。')]
INTENT_ARGUMENT_DESCRIPTIONS = {
    'read_markets': '原文或图片已明确双方、玩法及期间的新增推荐，首次search_matches或消歧resolve_match传true，并同时传market_kind、period和market_query；只在完整搜索且唯一活动绑定后返回market_result。复用该结果，不重复list_match_markets；完整过滤结果中原期间/线/方向全部closed或suspended时直接说明原盘口不可用，禁止去掉筛选再读盘找替代线。未知意图保持false，不猜参数。',
    'market_kind': '原文明示的玩法：大小球用total，让球用handicap；与read_markets=true一起传，不从赔率猜玩法。其他玩法按工具已知原生类型核验。',
    'period': '按原文明示期间：全场full_time、上半场first_half、下半场second_half。已知时与read_markets=true一起传；不能把上半场换成全场，未知时不要猜。',
}

# These profiles are used with the complete, preloaded expert-review skill.
# Only descriptions change: validation schemas and business calls stay native.
BUDGET_TOOL_DESCRIPTIONS = {
    'review_match_batch': 'expert-review单场或多场明确来源首选（1项也用此工具）：最多6个不同对阵，内部2路连续分页、唯一绑定、读盘并精确录入total/handicap/team_total；单队进球须selection_team。核实双方、联赛、期间及原文，不从库存补字段；无原期间盘口立即未解决，禁换期间。',
    'search_matches': 'expert-review已预载，勿重复skill_view。同场查询只调用一次。查全部阶段并连续翻页；传原文双方、sport及原始日期。唯一活动绑定可合并读盘；明确单项total/期间/方向/线时record_source_intent=true，以record_result为准。未知期间不录入。',
    'resolve_match': 'expert-review已预载；用完整search_key及双方/联赛/时间消歧，已绑定不重做。明确单项total可合并读盘录入，以record_result为准；非活动不读盘。',
    'list_match_markets': 'expert-review：明确total来源时record_source_intent=true，带market_query/evidence_text及原source_periods；恢复读盘后精确有效项立即录入，以record_result为准。未知不录入，完整零库存停止。',
    'record_expert_signals': 'expert-review已预载；提交本任务已读quote_id，以accepted/rejected/系统价格为准。retry_same_evidence=false停止该项重试，其余继续。',
}
BUDGET_ARGUMENT_DESCRIPTIONS = {
    'cursor': '逐字复制本查询next_cursor；首查不传，连续读至空。',
    'sport': '来源实际项目，如football/basketball；未知才省略。',
    'team_a': '来源明确第一队；与team_b成对。',
    'team_b': '来源明确第二队，不从候选补造。',
    'competition_query': '可读来源联赛必填；未知显式null。',
    'source_phase': '仅原文/所选原图明示阶段才传；未知null，不从可售候选推断。',
    'selection_team': '所选原文team_a或team_b；不是系统主客。',
    'selection_line': '所选球队带符号线：受让正、让球负。',
    'read_markets': '来源明确玩法/期间时true；唯一活动绑定合并读盘并复用。',
    'market_kind': '来源玩法，如total/handicap；不从赔率猜。',
    'period': '来源明确期间；未知不猜。',
    'source_periods': '来源全部期间首读前一次声明；未知[]，读盘后禁扩充。',
    'market_query': '逐字来源方向/线值，如小3.5、大0.5/1；不比较倍率。',
    'record_source_intent': '来源明确total期间/方向/线时true，带market_query/evidence_text；只认record_result。',
    'evidence_text': '本项原文/所选原图证据；不得按库存补造期间、方向或线。',
    'search_key': '逐字复制当前完整search_matches回执的64位标识。',
}


def budget_schema_descriptions(schema):
    """Copy schema descriptions without changing types, defaults or constraints."""
    if isinstance(schema, list):
        return [budget_schema_descriptions(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {key: budget_schema_descriptions(value) for key, value in schema.items()}
    for name, prop in result.get('properties', {}).items():
        if (name in BUDGET_ARGUMENT_DESCRIPTIONS
                and ('description' in prop or name in {'market_query', 'evidence_text'})):
            prop['description'] = BUDGET_ARGUMENT_DESCRIPTIONS[name]
    properties = result.get('properties', {})
    if all(name in properties for name in ('market_query', 'selection_team', 'selection_line')):
        properties['market_query']['description'] = 'total/team_total填原表达；单队须selection_team；handicap用selection_team/selection_line。'
    return result


class CompactMCPServer(MCPServer):
    async def list_tools(self):
        tools = await super().list_tools()
        # Legacy profiles retain native schemas. Only the explicit intent
        # profiles request these nullable fields on every lookup. The second
        # connection also avoids the pinned client's per-session RPC lock.
        result = []
        for tool in tools:
            updates = {}
            if explicit_market_intent.get() and tool.name == 'search_matches':
                updates['description'] = (tool.description or '').replace(
                    'resolved=true且realtime_eligible=true可直接读盘，否则用resolve_match继续核验。',
                    'resolved=true时绑定已完成：fixture.realtime_eligible=true才可读盘；false或phase为finished/cancelled时停止读盘，说明不可实时录入；仅未唯一绑定时再resolve_match。')
            if compact_descriptions.get():
                updates['description'] = (f'独立并行调用football.{tool.name}；完整规则见同名主工具，'
                    '参数、分页、绑定、读盘及录入校验不变。')
            if explicit_market_intent.get() and tool.name in {'search_matches', 'resolve_match','list_match_markets'}:
                fields = (('read_markets', 'market_kind', 'period', 'market_query','source_periods')
                          if tool.name != 'list_match_markets' else ('source_periods',))
                schema = dict(tool.input_schema)
                properties = dict(schema['properties'])
                for field in fields:
                    properties[field] = {key: value for key, value in properties[field].items()
                                         if key != 'default'}
                    if field in INTENT_ARGUMENT_DESCRIPTIONS:
                        properties[field]['description'] = INTENT_ARGUMENT_DESCRIPTIONS[field]
                schema['properties'] = properties
                schema['required'] = list(dict.fromkeys([*schema.get('required', []), *fields]))
                updates['input_schema'] = schema
            if budget_descriptions.get():
                updates['description'] = BUDGET_TOOL_DESCRIPTIONS[tool.name]
                if compact_descriptions.get():
                    updates['description'] = (
                        'expert-review多场用主连接football.review_match_batch，本连接不重复提交。'
                        if tool.name == 'review_match_batch' else
                        '仅供不同对阵独立并行，勿重复主连接同场查询。' + updates['description']
                        if tool.name == 'search_matches' else
                        f'expert-review独立对阵并行，参数及校验同football.{tool.name}。')
                updates['input_schema'] = budget_schema_descriptions(
                    updates.get('input_schema', tool.input_schema))
            result.append(tool.model_copy(update=updates) if updates else tool)
        return result

    async def call_tool(self, name: str, arguments: dict, context=None):
        # Child read tasks inherit this per-call holder; setting a new
        # ContextVar value in a child would not reach the parent RPC.
        token = terminal_review.set({})
        try:
            try:
                result = await super().call_tool(name, arguments, context=context)
            except ToolError as exc:
                if not terminal_review.get():
                    raise
                result = CallToolResult(content=[TextContent(type='text', text=str(exc))], is_error=True)
            stopped = terminal_review.get()
            if stopped and result.is_error:
                # An authenticated business refusal is not a failed MCP
                # connection. Keep the refusal explicit, stop this attempt,
                # and do not trip the shared transport breaker for new jobs.
                value = {'review_terminal_state': stopped, 'stop_review': True,
                         'retryable': False, 'accepted': [],
                         'diagnostic': [part.text[:2400] for part in result.content
                                        if isinstance(part, TextContent)]}
                result = CallToolResult(content=[TextContent(type='text',
                    text=json.dumps(value, ensure_ascii=False))], structured_content=value, is_error=False)
            return compact_tool_result(result)
        finally:
            terminal_review.reset(token)


mcp = CompactMCPServer("football-expert-platform")
# Both advertised MCP aliases reach this single process. The collector admits
# one precise refresh at a time; queue market operations here without blocking
# independent fixture searches or changing the collector's upstream budget.
MARKET_WAIT_SECONDS = 8
MAX_PENDING_MARKET_CALLS = 8
market_gate = asyncio.Lock()
pending_market_calls = 0
pending_market_requests = 0
market_reads: dict[str, dict] = {}


class ExpertPick(BaseModel):
    fixture_id: str = Field(min_length=1, max_length=160)
    quote_id: str = Field(min_length=1, max_length=80)
    evidence_text: str = Field(min_length=1, max_length=3000)
    confidence: float = Field(default=0.5, ge=0, le=1)


class SourceMatchIntent(BaseModel):
    """A bounded source claim; only native operations can confirm or accept it."""
    model_config = {'extra': 'forbid'}
    team_a: str = Field(min_length=1, max_length=160, description='原文第一支球队，保留梯队/年龄/性别；不是从候选补造。')
    team_b: str = Field(min_length=1, max_length=160, description='原文第二支球队，来源顺序不等于系统主客。')
    sport: str = Field(min_length=1, max_length=40)
    competition_query: str | None = Field(max_length=160, description='必填：来源可读联赛须原样保留，避免真实比赛与电子赛同名歧义；来源未知才显式null。')
    source_phase: Literal['scheduled', 'live', 'finished', 'cancelled'] | None = Field(default=None,
        description='仅原文或所选原图明示赛前/滚球/完场/取消时传；未知null，不按候选可售状态猜。')
    from_utc: str = Field(min_length=1, max_length=40)
    to_utc: str = Field(min_length=1, max_length=40)
    market_kind: str = Field(min_length=1, max_length=80)
    period: Literal['full_time', 'first_half', 'second_half'] | None
    source_periods: Annotated[list[Literal['full_time', 'first_half', 'second_half']], Field(max_length=3)]
    market_query: str | None = Field(default=None, max_length=120, description='total/team_total填大小球原表达；handicap必须省略或null，使用selection_team/selection_line，不同时填此筛选。')
    selection_team: Literal['team_a', 'team_b'] | None = Field(default=None, description='handicap或team_total来源明确所选原文队伍；使用team_a或team_b，不假设主客。total省略。')
    selection_line: Decimal | None = Field(default=None, max_digits=8, decimal_places=3,
        multiple_of=Decimal('.25'), allow_inf_nan=False, description='来源所选球队带符号线；受让为正、让球为负，不填系统主队口径。')
    evidence_text: str = Field(min_length=1, max_length=3000)

    @model_validator(mode='after')
    def explicit_scope(self):
        # Some clients serialize optional text as an empty string. It carries
        # no market instruction; normalize only that representation of absence.
        # A nonempty, conflicting handicap query must still fail closed.
        if isinstance(self.market_query, str) and not self.market_query.strip():
            self.market_query = None
        if (not self.team_a.strip() or not self.team_b.strip() or self.team_a.strip() == self.team_b.strip()
                or not self.evidence_text.strip()
                or (self.source_periods and self.period not in self.source_periods)):
            raise ValueError('完整来源双方、证据及首读前期间清单为必需，未知项用单场工具保留未解决')
        if self.market_kind == 'handicap':
            if self.selection_team is None or self.selection_line is None or self.market_query is not None:
                raise ValueError('让球需要原文所选队伍及带符号selection_line，不传market_query')
        elif self.market_kind == 'total':
            if not self.market_query or self.selection_team is not None or self.selection_line is not None:
                raise ValueError('大小球需要来源market_query，不使用让球选择字段')
        elif self.market_kind == 'team_total':
            if not self.market_query or self.selection_team is None or self.selection_line is not None:
                raise ValueError('单队大小球需要market_query及原文selection_team，不使用让球selection_line')
        elif self.selection_team is not None or self.selection_line is not None:
            raise ValueError('其它玩法不支持自动按所选方线录入')
        return self


async def _post(path: str, payload: dict) -> dict:
    global pending_market_requests
    if path not in {'list_match_markets', 'record_expert_signals'}:
        return await _request(path, payload)
    if pending_market_requests >= MAX_PENDING_MARKET_CALLS:
        raise ToolError("盘口复核队列繁忙；保留当前消息为待核验")
    pending_market_requests += 1
    try:
        result = await _market_request(path, payload)
        # A provider spacing refusal is not a stale-price permission. Wait
        # once inside this authenticated read instead of spending another
        # model turn; the backend still checks the original 20s deadline.
        # Never replay a write or bypass upstream cooldown/unknown results.
        refresh = result.get('refresh') if isinstance(result, dict) else None
        if path == 'list_match_markets' and isinstance(refresh, dict):
            wait = refresh.get('retry_after_seconds')
            if (refresh.get('reason') == 'refresh_throttled'
                    and refresh.get('accepted') is False
                    and type(wait) in (int, float) and 0 < wait <= 2):
                await asyncio.sleep(wait)
                result = await _market_request(path, payload)
        return result
    finally:
        pending_market_requests -= 1


async def _market_request(path: str, payload: dict) -> dict:
    if path == 'record_expert_signals':
        return await _admitted_market_request(path, payload)
    # Both MCP aliases may request the exact same read concurrently. Reuse
    # only that in-flight read, including its attempt and every filter; there
    # is no completed-result cache or sharing across authorization contexts.
    key = json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False)
    entry = market_reads.get(key)
    if entry is not None and entry['task'].done():
        market_reads.pop(key)
        entry = None
    if entry is None:
        if len(market_reads) >= MAX_PENDING_MARKET_CALLS:
            raise ToolError("盘口复核队列繁忙；保留当前消息为待核验")
        entry = {'task': asyncio.create_task(_admitted_market_request(path, payload)), 'waiters': 0}
        market_reads[key] = entry
    entry['waiters'] += 1
    try:
        return await asyncio.shield(entry['task'])
    finally:
        entry['waiters'] -= 1
        if not entry['waiters']:
            if market_reads.get(key) is entry:
                market_reads.pop(key)
            if not entry['task'].done():
                entry['task'].cancel()
            await asyncio.gather(entry['task'], return_exceptions=True)


async def _admitted_market_request(path: str, payload: dict) -> dict:
    global pending_market_calls
    if pending_market_calls >= MAX_PENDING_MARKET_CALLS:
        raise ToolError("盘口复核队列繁忙；保留当前消息为待核验")
    pending_market_calls += 1
    acquired = False
    try:
        try:
            async with asyncio.timeout(MARKET_WAIT_SECONDS):
                await market_gate.acquire()
                acquired = True
        except TimeoutError:
            raise ToolError("盘口复核队列等待超时；保留当前消息为待核验") from None
        return await _request(path, payload)
    finally:
        if acquired:
            market_gate.release()
        pending_market_calls -= 1


async def _request(path: str, payload: dict) -> dict:
    try:
        response = await client.post(f"{API_URL}/{path}", json=payload,
            timeout=httpx.Timeout(45 if path == 'record_expert_signals' else 20, connect=2))
        response.raise_for_status()
        return response.json()
    except httpx.TimeoutException:
        raise ToolError("系统工具超时；保留当前消息为待核验") from None
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", "工具请求被拒绝")
        except (ValueError, AttributeError):
            detail = "工具请求被拒绝"
        reasons = {
            'review attempt is no longer active': 'attempt_inactive',
            'message or source is not eligible': 'source_ineligible',
            'message has a newer revision': 'source_revised',
            'referenced image message changed after review started': 'reply_revised',
        }
        reason = reasons.get(detail) if isinstance(detail, str) else None
        if isinstance(detail, dict) and detail.get('category') == 'detection_deadline_exceeded':
            reason = 'detection_deadline_exceeded'
        if exc.response.status_code == 409 and reason is not None:
            stopped = terminal_review.get()
            if stopped is not None:
                stopped.update(reason=reason, operation=path)
        raise ToolError(str(detail)[:240]) from None
    except httpx.HTTPError:
        raise ToolError("系统工具暂不可用") from None


async def _read_source_markets(payload):
    """Keep a complete empty declared scope distinct from other inventory."""
    if payload.get('source_periods') == []:
        return {'markets': [], 'market_read_succeeded': False,
            'source_intent_recording': {'attempted': False, 'reason': 'source_period_unknown'},
            'source_scope_unavailable': {'reason': 'source_period_unknown',
                'retry_same_scope': False, 'relax_source_period': False,
                'action': 'report_unresolved_source_period',
                'message': '来源期间明确声明未知，不请求报价、不从库存或分钟补期间；'
                           '只保留已核验的比赛身份，说明缺来源期间，结束本项审核。'},
            'market_coverage': {'complete_for_filter': False}}
    result = await _post('list_match_markets', payload)
    coverage = result.get('market_coverage') or {}
    scope = result.get('market_filter') or {}
    periods = payload.get('source_periods')
    if (isinstance(periods, list) and payload.get('period') in periods
            and payload.get('market_kind') is not None
            and scope.get('period') == payload['period']
            and scope.get('market_kind') == payload['market_kind']
            and coverage.get('complete_for_filter') is True
            and coverage.get('matched_quotes') == 0
            and coverage.get('returned_quotes') == 0
            and result.get('markets') == []):
        return result | {'source_scope_unavailable': {
            'reason': 'no_quote_in_complete_source_scope',
            'scope': scope, 'retry_same_scope': False,
            'relax_source_period': False, 'relax_source_market_kind': False,
            'action': 'report_unresolved_source_option',
            'message': '原文声明的玩法/期间/线值完整筛选零盘口；total_quotes是其它库存，'
                       '不证明原推荐有盘。保留未解决，不重复读盘、不去掉期间或改成其它玩法。'}}
    return result


async def _with_bound_markets(result: dict, resolution: dict | None,
                              review_id: str, attempt_id: str,
                              market_kind: str | None, period: str | None,
                              *, search_key: str | None = None,
                              market_query: str | None = None,
                              record_source_intent: bool = False,
                              evidence_text: str | None = None,
                              source_periods: SourcePeriods = None,
                              source_selection: str | None = None,
                              source_selection_line: Decimal | None = None) -> dict:
    """Compose existing guarded operations, never derive a fixture identity."""
    if not isinstance(resolution, dict) or resolution.get('resolved') is not True:
        return result
    fixture = resolution.get('fixture')
    if (not isinstance(fixture, dict) or fixture.get('realtime_eligible') is not True
            or not isinstance(fixture.get('id'), str) or not fixture['id']):
        return result
    try:
        market_parameters = {
            'review_id': review_id, 'attempt_id': attempt_id, 'fixture_id': fixture['id'],
            'market_kind': market_kind, 'period': period,
        }
        if market_query is not None:
            market_parameters['market_query'] = market_query
        market_parameters.update(_source_period_parameters(source_periods))
        markets = await _read_source_markets(market_parameters)
    except ToolError as exc:
        # Search/binding has already committed before this optional read.
        # Keep its native receipt visible even though the composed call failed;
        # otherwise a repeated search is correctly rejected as already complete.
        receipt = {'review_id': review_id, 'attempt_id': attempt_id,
            **{key: result[key] for key in ('search_key', 'search_complete', 'next_cursor') if key in result},
            'resolution': {'resolved': True, 'fixture': {key: fixture[key] for key in (
                'id', 'realtime_eligible', 'home_team', 'away_team', 'competition_name',
                'sport', 'phase', 'kickoff_at') if key in fixture}}}
        if search_key is not None:
            receipt.setdefault('search_key', search_key)
        raise ToolError(json.dumps({'completed_receipt': receipt,
            'failed_operation': 'list_match_markets', 'market_read_succeeded': False,
            'error': str(exc),
            'recovery': '检索/绑定回执已保留，但未成功读盘；仅在同一活动review_id/attempt_id下使用原生'
                        'search_key或已绑定fixture继续核验，不要重复已完成检索，不要猜编号；租约失效则停止。'},
            ensure_ascii=False)) from None
    combined = {**result, 'market_result': markets}
    if not record_source_intent:
        return combined
    return await _record_bound_markets(combined, markets, fixture, review_id, attempt_id,
        market_kind, period, evidence_text, search_key=search_key,
        source_selection=source_selection, source_selection_line=source_selection_line)


async def _record_bound_markets(combined, markets, fixture, review_id, attempt_id,
                                market_kind, period, evidence_text, *, search_key=None,
                                source_selection=None, source_selection_line=None):
    """Reuse the same native exact-scope gate after lookup or a recovery read."""
    if market_kind == 'handicap':
        quote = _unique_source_handicap(markets, fixture['id'], period, source_selection, source_selection_line)
    elif market_kind == 'team_total':
        quote = _unique_source_team_total(markets, fixture['id'], period, source_selection)
    else:
        quote = _unique_source_total(markets, fixture['id'], market_kind, period)
    if quote is None:
        return combined | {'source_intent_recording': {
            'attempted': False, 'reason': 'no_unique_source_option'}}
    try:
        recorded = await _post('record_expert_signals', {
            'review_id': review_id, 'attempt_id': attempt_id,
            'signals': [ExpertPick(fixture_id=fixture['id'], quote_id=quote['quote_id'],
                                   evidence_text=evidence_text).model_dump()],
        })
        if (not isinstance(recorded, dict) or not isinstance(recorded.get('accepted'), list)
                or not isinstance(recorded.get('rejected'), list)):
            raise ToolError('录入响应无法核验；保留结果未知状态')
    except ToolError as exc:
        receipt = {'review_id': review_id, 'attempt_id': attempt_id,
            **{key: combined[key] for key in ('search_key', 'search_complete', 'next_cursor') if key in combined},
            'resolution': {'resolved': True, 'fixture': {key: fixture[key]
                for key in ('id', 'realtime_eligible') if key in fixture}},
            'market_result': markets}
        if search_key is not None:
            receipt.setdefault('search_key', search_key)
        raise ToolError(json.dumps({'completed_receipt': receipt,
            'failed_operation': 'record_expert_signals', 'record_result_verified': False,
            'error': str(exc),
            'recovery': '检索、绑定与读盘已完成，录入结果未核验。不要重启已完成检索；'
                        '仅在同一活动review_id/attempt_id下核对原生回执，必要时使用相同已读quote_id'
                        '和原证据进行幂等录入。租约失效立即停止，不把读盘成功称为推荐成功。'},
            ensure_ascii=False)) from None
    return combined | {'record_result': recorded, 'source_intent_recording': {'attempted': True}}


def _require_source_record_scope(read_markets, market_kind, period, market_query, evidence_text):
    if (read_markets is not True or market_kind != 'total'
            or period not in {'full_time', 'first_half', 'second_half'}
            or not isinstance(market_query, str) or not market_query.strip()
            or not isinstance(evidence_text, str) or not evidence_text.strip()):
        raise ToolError('一轮录入需要read_markets=true、来源明确的total/期间/market_query及非空原文证据')


def _source_period_parameters(source_periods):
    if source_periods is not None or explicit_market_intent.get():
        return {'source_periods':source_periods or []}
    return {}


def _unique_source_team_total(markets, fixture_id, period, side):
    if not isinstance(markets, dict) or side not in {'home', 'away'} or period != 'full_time':
        return None
    rows = markets.get('markets')
    coverage = markets.get('market_coverage')
    if (not isinstance(rows, list) or not isinstance(coverage, dict)
            or coverage.get('complete_for_filter') is not True
            or coverage.get('returned_quotes') != len(rows)
            or coverage.get('matched_quotes') != len(rows)):
        return None
    selected = [row for row in rows if isinstance(row, dict) and row.get('team_side') == side]
    if len(selected) != 1:
        return None
    row = selected[0]
    # Reuse the exact total gates on this verified native team-side scope.
    adapted = dict(markets, markets=[dict(row, market_kind='total')],
        market_filter=dict(markets.get('market_filter') or {}, market_kind='total'),
        market_coverage=dict(coverage, matched_quotes=1, returned_quotes=1))
    if (row.get('market_kind') != 'team_total'
            or markets.get('market_filter', {}).get('market_kind') != 'team_total'
            or _unique_source_total(adapted, fixture_id, 'total', period) is None):
        return None
    return row


def _unique_source_total(markets, fixture_id, market_kind, period):
    """Select nothing unless the native exact scope contains one eligible row."""
    if not isinstance(markets, dict):
        return None
    parent = markets.get('fixture')
    if not isinstance(parent, dict) or parent.get('id') != fixture_id:
        return None
    scope, coverage, quotes = (markets.get(key) for key in ('market_filter', 'market_coverage', 'markets'))
    if (not isinstance(scope, dict) or not isinstance(coverage, dict)
            or not isinstance(quotes, list) or len(quotes) != 1
            or coverage.get('complete_for_filter') is not True
            or type(coverage.get('total_quotes')) is not int or coverage['total_quotes'] < 1
            or type(coverage.get('matched_quotes')) is not int or coverage['matched_quotes'] != 1
            or type(coverage.get('returned_quotes')) is not int or coverage['returned_quotes'] != 1
            or scope.get('market_kind') != market_kind or market_kind != 'total'
            or scope.get('period') != period or period not in {'full_time', 'first_half', 'second_half'}
            or scope.get('selection') not in {'over', 'under'}):
        return None
    quote = quotes[0]
    if (not isinstance(quote, dict)
            or ('fixture_id' in quote and quote['fixture_id'] != fixture_id)
            or quote.get('market_kind') != market_kind or quote.get('period') != period
            or quote.get('selection') != scope['selection'] or quote.get('status') != 'active'
            or quote.get('can_record') is not True or not isinstance(quote.get('quote_id'), str)
            or not quote['quote_id']):
        return None
    try:
        line, offered = Decimal(str(scope.get('line'))), Decimal(str(quote.get('line')))
        if (not line.is_finite() or not offered.is_finite() or line < 0
                or line % Decimal('.25') or line != offered):
            return None
    except (InvalidOperation, ValueError):
        return None
    return quote


def _unique_source_handicap(markets, fixture_id, period, selection, selection_line):
    """Use the native selected-side line, never negate or reinterpret line."""
    if not isinstance(markets, dict) or selection not in {'home', 'away'} or selection_line is None:
        return None
    if not isinstance(markets.get('fixture'), dict) or markets['fixture'].get('id') != fixture_id:
        return None
    scope, coverage, rows = (markets.get(key) for key in ('market_filter', 'market_coverage', 'markets'))
    if (not isinstance(scope, dict) or not isinstance(coverage, dict) or not isinstance(rows, list)
            or scope.get('market_kind') != 'handicap' or scope.get('period') != period
            or scope.get('market_query') or scope.get('line') is not None
            or scope.get('selection') not in (None, selection)
            or coverage.get('complete_for_filter') is not True
            or type(coverage.get('matched_quotes')) is not int
            or coverage['matched_quotes'] != len(rows)
            or type(coverage.get('returned_quotes')) is not int
            or coverage.get('returned_quotes') != len(rows)):
        return None
    matches = []
    for row in rows:
        if (not isinstance(row, dict) or row.get('market_kind') != 'handicap'
                or row.get('period') != period or row.get('selection') != selection
                or row.get('fixture_id', fixture_id) != fixture_id):
            continue
        try:
            line = Decimal(str(row.get('selection_line')))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if line.is_finite() and line == selection_line:
            matches.append(row)
    if len(matches) != 1:
        return None
    quote = matches[0]
    return (quote if quote.get('status') == 'active' and quote.get('can_record') is True
            and isinstance(quote.get('quote_id'), str) and quote['quote_id'] else None)


async def _search_source_scopes(payload):
    """Follow only native bounded recall guidance, without a model roundtrip."""
    result = await _post('search_matches', payload)
    if (payload.get('cursor') or not payload.get('team_a') or not payload.get('team_b')
            or not payload.get('competition_query') or not payload.get('sport')
            or not payload.get('from_utc') or not payload.get('to_utc')):
        return result
    try:
        start = datetime.fromisoformat(payload['from_utc'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(payload['to_utc'].replace('Z', '+00:00'))
        if start.tzinfo is None or end.tzinfo is None or not timedelta(0) < end-start <= timedelta(days=7):
            return result
    except (TypeError, ValueError):
        return result
    receipts = []
    original = payload
    for _ in range(2):
        guidance = result.get('next_search_guidance') or {}
        if (result.get('search_complete') is not True or result.get('next_cursor') is not None
                or (result.get('resolution') or {}).get('resolved') is True):
            break
        action = guidance.get('action')
        if action == 'search_source_team_without_competition_filter':
            following = original | {'competition_query': None, 'cursor': None}
        elif action == 'search_complete_league_candidates':
            following = original | {'team_query': '', 'cursor': None}
        else:
            break
        if not following['team_query'] and any(r.get('league_scope') for r in receipts):
            break
        receipt = {key: result[key] for key in ('search_key','search_complete',
            'next_cursor','total_candidates','resolution') if key in result}
        receipt['league_scope'] = payload.get('team_query') == ''
        receipts.append(receipt)
        try:
            result = await _post('search_matches', following)
        except ToolError as exc:
            raise ToolError(json.dumps({'completed_receipt': result,
                'failed_operation': 'search_matches', 'error': str(exc)}, ensure_ascii=False)) from None
        payload = following
        if following['team_query'] == '':
            break
    if receipts:
        result = result | {'prior_source_search_scopes': receipts,
            'source_search_scope': {key: payload[key] for key in
                ('team_query','competition_query','sport','phase','from_utc','to_utc') if key in payload}}
        if (payload.get('team_query') == '' and result.get('search_complete') is True
                and result.get('next_cursor') is None
                and (result.get('resolution') or {}).get('resolved') is not True):
            result['bounded_source_search'] = {
                'complete': True, 'retry_same_scope': False,
                'reason': 'no_verified_source_pair_in_complete_scopes',
                'scope': 'original_sport_competition_and_time_only',
                'source_image_check': '若来源含图，重新逐字看原图双方及梯队；纠正读错字才可提交新原名，不按候选补名。原图仍相同则保留未解决，不重复同参数resolve或扩大日期。'}
    return result


async def _review_batch_item(review_id, attempt_id, item):
    result = None
    cursor = None
    candidates = []
    scope = {}
    # Native search owns the candidate snapshot and verifies every next_cursor.
    # An unusually large source-name scope remains explicitly incomplete.
    for page_index in range(20):
        try:
            page_result = await _search_source_scopes({
                'review_id': review_id, 'attempt_id': attempt_id,
                'team_query': item.team_a, 'team_a': item.team_a, 'team_b': item.team_b,
                'phase': 'all', 'competition_query': item.competition_query, 'sport': item.sport,
                'from_utc': item.from_utc, 'to_utc': item.to_utc, 'cursor': cursor, 'page_size': 100, **scope})
        except ToolError as exc:
            if result is None:
                raise
            raise ToolError(json.dumps({'completed_receipt': result,
                'failed_operation': 'search_matches', 'error': str(exc),
                'recovery': '仅同一活动任务沿回执next_cursor继续；未完整翻页不读盘、不重新首查。'},
                ensure_ascii=False)) from None
        result = page_result
        scope = result.get('source_search_scope') or scope
        page = result.get('items')
        if not isinstance(page, list) or len(page) > 100:
            return result | {'batch_unresolved': 'invalid_native_page'}
        candidates.extend(page)
        result = result | {'items': list(candidates), 'batch_candidate_pages': page_index + 1}
        cursor = result.get('next_cursor')
        if result.get('search_complete') is True and cursor is None:
            resolution = result.get('resolution') or {}
            if (resolution.get('resolved') is not True and item.source_phase is not None
                    and resolution.get('candidate_count') != 0):
                search_key = result.get('search_key')
                if isinstance(search_key, str) and re.fullmatch(r'[0-9a-f]{64}', search_key):
                    resolution = await _post('resolve_match', {
                        'review_id': review_id, 'attempt_id': attempt_id, 'search_key': search_key,
                        'team_a': item.team_a, 'team_b': item.team_b,
                        'competition': item.competition_query, 'source_phase': item.source_phase,
                        'from_utc': item.from_utc, 'to_utc': item.to_utc})
                    result = result | {'resolution': resolution}
            sides = resolution.get('source_team_sides') or {}
            if not isinstance(sides, dict):
                sides = {}
            selected = sides.get(item.selection_team)
            if not item.source_periods or item.period not in item.source_periods:
                return result | {'source_intent_recording': {
                    'attempted': False, 'reason': 'source_period_unknown'}}
            can_select = item.market_kind == 'total' or (
                item.market_kind in {'handicap', 'team_total'} and set(sides) == {'team_a', 'team_b'}
                and all(isinstance(side, str) for side in sides.values())
                and set(sides.values()) == {'home', 'away'})
            return await _with_bound_markets(result, resolution, review_id, attempt_id,
                item.market_kind, item.period, market_query=item.market_query,
                record_source_intent=can_select, evidence_text=item.evidence_text,
                source_periods=item.source_periods, source_selection=selected,
                source_selection_line=item.selection_line)
        if not isinstance(cursor, str) or not cursor:
            return result | {'batch_unresolved': 'invalid_or_incomplete_native_cursor'}
    return result | {'batch_unresolved': 'pagination_budget_exceeded'}


def _batch_result_view(result, item):
    """Present native receipts and source-specific rows, never a fake inventory.

    The native API has already persisted every complete search/market response.
    This view is deliberately different from a native list_match_markets result.
    """
    def compact_rows(rows):
        if not rows:
            return []
        if not all(isinstance(row, dict) and set(row) == set(rows[0]) for row in rows):
            return rows
        shared = {key: rows[0][key] for key in rows[0]
                  if all(row[key] == rows[0][key] for row in rows)}
        columns = sorted(set(rows[0]) - set(shared))
        return {'encoding': 'shared-column-rows-v1', 'shared': shared,
                'columns': columns, 'rows': [[row[key] for key in columns] for row in rows],
                'decoding': '每行先按columns对应，再合并shared，完整恢复一条原生记录；不得跨行混用编号或状态。'}

    def fixture_view(fixture):
        return {key: fixture[key] for key in (
            'id', 'sport', 'competition_name', 'home_team', 'away_team', 'kickoff_at',
            'phase', 'realtime_eligible', 'eligibility_reason', 'match_state', 'clock_minute',
            'home_score', 'away_score') if key in fixture}

    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False))

    if not isinstance(result, dict):
        raise ToolError('批量原生响应不是对象；结果未知，禁止盲重试')
    view = {key: result[key] for key in (
        'review_id', 'attempt_id', 'search_key', 'search_complete', 'next_cursor',
        'total_candidates', 'batch_candidate_pages', 'batch_unresolved',
        'source_intent_recording', 'record_result', 'source_search_scope',
        'prior_source_search_scopes', 'bounded_source_search') if key in result}
    presentation = {'kind': 'source-selection-view-v1',
                    'native_evidence': '完整原生检索、读盘与录入回执已由API持久化；此处是来源选择展示，不是完整库存。'}
    resolution = result.get('resolution')
    if isinstance(resolution, dict):
        view['resolution'] = dict(resolution)
        if isinstance(resolution.get('fixture'), dict):
            view['resolution']['fixture'] = fixture_view(resolution['fixture'])
        if isinstance(resolution.get('matching_candidates'), list):
            # All disambiguation candidates remain available, losslessly.
            view['resolution']['matching_candidates'] = compact_rows(resolution['matching_candidates'])
    candidates = result.get('items')
    if isinstance(candidates, list):
        shown = candidates[:3]
        view['items'] = [fixture_view(row) for row in shown if isinstance(row, dict)]
        presentation['candidates'] = {'native_returned_count': len(candidates),
            'displayed_count': len(view['items']), 'items_complete': len(candidates) == len(view['items']),
            'native_search_complete': result.get('search_complete') is True,
            'fields_projected': True, 'not_a_no_match_claim': True}
    if isinstance(result.get('next_search_guidance'), dict):
        view['next_search_guidance'] = result['next_search_guidance']
    markets = result.get('market_result')
    if isinstance(markets, dict):
        scope = markets.get('market_filter')
        scope = scope if isinstance(scope, dict) else {}
        sides = resolution.get('source_team_sides') if isinstance(resolution, dict) else None
        selection = sides.get(item.selection_team) if isinstance(sides, dict) else None
        wanted_line = item.selection_line
        known_scope = item.market_kind == 'handicap' and selection in {'home', 'away'} and wanted_line is not None
        if item.market_kind in {'total', 'team_total'}:
            selection = scope.get('selection')
            try:
                wanted_line = Decimal(str(scope.get('line')))
                known_scope = selection in {'over', 'under'} and wanted_line.is_finite()
                if item.market_kind == 'team_total':
                    known_scope = known_scope and isinstance(sides, dict) and sides.get(item.selection_team) in {'home', 'away'}
            except (InvalidOperation, ValueError, TypeError):
                known_scope = False
        rows = markets.get('markets')
        rows = rows if isinstance(rows, list) else []
        coverage = markets.get('market_coverage')
        known_scope = (known_scope and isinstance(coverage, dict)
            and coverage.get('complete_for_filter') is True
            and coverage.get('matched_quotes') == len(rows) and coverage.get('returned_quotes') == len(rows)
            and scope.get('market_kind') == item.market_kind and scope.get('period') == item.period)
        if item.market_kind == 'handicap':
            known_scope = known_scope and scope.get('selection') in (None, selection) and scope.get('line') is None
        selected = []
        for row in rows:
            if (not known_scope or not isinstance(row, dict)
                    or row.get('market_kind') != item.market_kind or row.get('period') != item.period
                    or row.get('selection') != selection):
                continue
            if item.market_kind == 'team_total' and row.get('team_side') != sides.get(item.selection_team):
                continue
            try:
                line = Decimal(str(row.get('selection_line')))
            except (InvalidOperation, ValueError, TypeError):
                continue
            if line.is_finite() and line == wanted_line:
                selected.append(row)
        view['market_result'] = {
            'native_market_filter': scope,
            'native_market_coverage': markets.get('market_coverage'),
            'native_counts': {'returned_quotes': len(rows), 'source_selection_quotes': len(selected)},
            'source_selection_quotes': compact_rows(selected),
            'presentation': {'kind': 'derived-source-selection', 'all_source_selection_states': known_scope,
                'selection': selection if known_scope else None,
                'selection_line': str(wanted_line) if known_scope else None,
                'market_kind': item.market_kind, 'period': item.period,
                'requires_manual_market_review': not known_scope,
                'not_native_complete_inventory': True},
        }
        if isinstance(markets.get('fixture'), dict):
            view['market_result']['fixture'] = fixture_view(markets['fixture'])
        if isinstance(markets.get('refresh'), dict):
            view['market_result']['refresh'] = {key: markets['refresh'][key]
                for key in ('attempted', 'accepted', 'reason') if key in markets['refresh']}
    view['presentation'] = presentation
    # Six native inventories can individually fit 90K while their envelope
    # cannot. Never make their successful/unknown receipts disappear into a
    # framework spill file. Explicitly withhold a pathological display rather
    # than returning a truncated inventory as complete.
    if size(view) > 14400:
        if 'items' in view:
            view['items'] = []
            presentation['candidates'].update(displayed_count=0, items_complete=False,
                                               withheld_for_inline_budget=True)
        if 'market_result' in view:
            market = view['market_result']
            market['source_selection_quotes'] = []
            market['presentation'].update(all_source_selection_states=False,
                requires_manual_market_review=True, withheld_for_inline_budget=True)
            market['native_counts']['displayed_source_selection_quotes'] = 0
        presentation['requires_manual_market_review'] = True
    return _accepted_batch_receipt(view)


def _accepted_batch_receipt(view):
    """Avoid repeating inventories after one exact native acceptance.

    All native evidence remains persisted by the API. Unresolved, rejected,
    incomplete or multi-option results retain the full source-selection view.
    This is a presentation change, never an alternative acceptance decision.
    """
    record = view.get('record_result')
    resolution = view.get('resolution')
    market = view.get('market_result')
    recording = view.get('source_intent_recording')
    if (not isinstance(record, dict) or not isinstance(record.get('accepted'), list)
            or len(record['accepted']) != 1 or record.get('rejected') != []
            or not isinstance(resolution, dict) or resolution.get('resolved') is not True
            or not isinstance(market, dict) or view.get('search_complete') is not True
            or view.get('next_cursor') is not None or not isinstance(recording, dict)
            or recording.get('attempted') is not True):
        return view
    display = market.get('presentation', {})
    if (not isinstance(display, dict) or display.get('all_source_selection_states') is not True
            or display.get('requires_manual_market_review') is not False):
        return view
    rows = market.get('source_selection_quotes')
    if isinstance(rows, dict) and rows.get('encoding') == 'shared-column-rows-v1':
        columns, shared, values = rows.get('columns'), rows.get('shared'), rows.get('rows')
        if (not isinstance(columns, list) or not all(isinstance(key, str) for key in columns)
                or len(set(columns)) != len(columns) or not isinstance(shared, dict)
                or not isinstance(values, list) or len(values) != 1
                or not isinstance(values[0], list) or len(values[0]) != len(columns)):
            return view
        rows = [dict(zip(columns, values[0])) | shared]
    if not isinstance(rows, list) or len(rows) != 1:
        return view
    quote, accepted = rows[0], record['accepted'][0]
    fixture = resolution.get('fixture', {})
    market_fixture = market.get('fixture')
    if not all(isinstance(value, dict) for value in (quote, accepted, fixture, market_fixture)):
        return view
    if (not quote.get('quote_id') or not accepted.get('signal_id')
            or accepted.get('index') != 0 or quote.get('status') != 'active'
            or quote.get('can_record') is not True or market_fixture.get('id') != fixture.get('id')
            or accepted.get('fixture_id') != fixture.get('id')
            or any(accepted.get(key) != quote.get(key) for key in ('period', 'selection'))):
        return view
    try:
        for key in ('line', 'selection_line'):
            left, right = Decimal(str(accepted.get(key))), Decimal(str(quote.get(key)))
            if not left.is_finite() or left != right:
                return view
    except (InvalidOperation, TypeError, ValueError):
        return view
    result = {key: value for key, value in view.items()
              if key not in {'items', 'market_result', 'resolution', 'presentation'}}
    result['resolution'] = {'resolved': True,
        'source_team_sides': resolution.get('source_team_sides'),
        'fixture': dict(fixture)}
    result['source_quote_receipt'] = {key: quote[key] for key in (
        'quote_id', 'market_kind', 'period', 'selection', 'line', 'selection_line', 'line_basis',
        'status', 'can_record') if key in quote}
    result['presentation'] = {'kind': 'accepted-source-receipt-v1',
        'native_evidence_persisted': True, 'record_result_unchanged': True,
        'not_market_inventory': True, 'repeat_query_required': False}
    return result


def _batch_error_view(error, item):
    """Turn a composed JSON error into readable receipts without double escaping."""
    try:
        failure = json.loads(str(error))
    except (ValueError, TypeError):
        failure = None
    if not isinstance(failure, dict):
        return {'error': str(error)[:160], 'unresolved': True, 'native_outcome_unknown': True,
                'retry_without_native_evidence': False}
    view = {key: failure[key] for key in (
        'failed_operation', 'market_read_succeeded', 'record_result_verified') if key in failure}
    view.update(error=str(failure.get('error', '原生操作未完成'))[:160], unresolved=True,
                native_outcome_unknown=failure.get('failed_operation') == 'record_expert_signals',
                retry_without_native_evidence=False)
    if isinstance(failure.get('completed_receipt'), dict):
        view['completed_receipt'] = _batch_result_view(failure['completed_receipt'], item)
    return view


@mcp.tool()
async def review_match_batch(review_id: str, attempt_id: str,
                             items: Annotated[list[SourceMatchIntent], Field(min_length=1, max_length=6)]) -> dict:
    """多场明确推荐首查一次提交，最多6个不同来源对阵、内部2路并发。逐项连续分页、唯一绑定、声明期间、读当前盘口并精确录入total/handicap/team_total。期间未知传period=null/source_periods=[]，只识别不读盘。带原文可读联赛。handicap用原文selection_team及所选方带符号selection_line；team_total用selection_team及market_query，不猜主客或价格。其它玩法只读盘。每项检查record_result，无回执不称成功。"""
    keys = [(item.sport.casefold(), frozenset((item.team_a.strip().casefold(), item.team_b.strip().casefold())))
            for item in items]
    if len(set(keys)) != len(keys):
        raise ToolError('批量只接受不同来源对阵；同场多项复用单场工具及完整来源期间，不重复检索')
    slots = asyncio.Semaphore(2)

    async def run(index, item):
        async with slots:
            try:
                result = await _review_batch_item(review_id, attempt_id, item)
                return {'index': index, 'result': _batch_result_view(result, item)}
            except ToolError as exc:
                # A failed independent item cannot erase another committed
                # receipt. Native composed errors preserve completed_receipt.
                return {'index': index, **_batch_error_view(exc, item)}
            except Exception as exc:
                return {'index': index, 'error_class': type(exc).__name__,
                        'unresolved': True, 'protocol_failure': True,
                        'native_outcome_unknown': True, 'retry_without_native_evidence': False}

    tasks = [asyncio.create_task(run(index, item)) for index, item in enumerate(items)]
    try:
        return {'items': await asyncio.gather(*tasks)}
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@mcp.tool()
async def search_matches(review_id: str, attempt_id: str, team_query: str | None = None,
                         phase: str = "all", competition_query: str | None = None,
                         from_utc: str | None = None, to_utc: str | None = None,
                         cursor: Annotated[str | None, Field(description="仅逐字复制本次search_matches返回的next_cursor；首查不传，禁止自行构造游标。")] = None,
                         page_size: int = 100,
                         sport: Annotated[str | None, Field(description="按来源实际体育项目传football或basketball等；原图足球联赛可识别时须传football，联赛简称仅在对应项目内归一。确实未知才省略，不以工具服务器名称猜项目。")] = None,
                         team_a: Annotated[str | None, Field(description="原文或原图明确可读的第一支球队名；双方已知时首次查询即与team_b成对传入，复用服务端唯一绑定。")] = None,
                         team_b: Annotated[str | None, Field(description="原文或原图明确可读的第二支球队名；双方已知时首次查询即与team_a成对传入，不从候选猜造原文球队。")] = None,
                         read_markets: Annotated[bool, Field(description="来源已明确玩法/期间时合并读盘；明确单项total/期间/方向/数字线时，须同时传record_source_intent=true、market_query和evidence_text，由原生完整唯一有效范围决定是否录入。")] = False,
                         market_kind: str | None = None,
                         period: str | None = None,
                         source_periods: SourcePeriods = None,
                         market_query: Annotated[str | None, Field(max_length=120,
                             description="原文明示的盘口查询，例如小3.5、大0.5/1；仅传给唯一绑定后的读盘，不参与球队检索，不比较倍率。")] = None,
                         record_source_intent: Annotated[bool, Field(description="来源明确双方、total玩法、期间、方向及线值时须传true，同时read_markets=true并提供market_query/evidence_text；仅完整唯一绑定且原生精确范围恰好一个有效选项时复用原生录入，返回record_result。未知期间保持false，默认不录入。")] = False,
                         evidence_text: Annotated[str | None, Field(max_length=3000,
                             description="与record_source_intent=true一起传的本项原文/原图选择证据；不得根据可售库存补造期间、方向或线值。")] = None) -> dict:
    """首次查询即传来源体育项目sport、双方team_a/team_b及原始时间范围。来源明确单项大小球、期间、方向与数字线时，同时传read_markets=true、market_kind=total、period、market_query、record_source_intent=true和evidence_text；工具在完整唯一有效范围内直接录入，返回record_result才算接受，不再重复提交。未知期间保持不录入。提供team_query，或同时提供原文明示的team_a/team_b并逐字复用team_a检索；显式team_query包括空串不被覆盖。原名或片段无双方对应候选时，可传空串展开同联赛：必须同时给competition_query、sport及正区间≤7天的from_utc/to_utc；competition_time_candidate只是待核验候选，不是身份绑定。phase=all检索全部阶段，连续分页至next_cursor为空。原文双方明确时可成对传team_a/team_b，仅完整检索结束后服务端唯一核验并返回resolution；resolved=true且realtime_eligible=true可直接读盘，否则用resolve_match继续核验。已知玩法/期间时read_markets=true可在唯一绑定后返回market_result，复用此读盘而不重复调用；期间使用full_time/first_half/second_half。不完整或不唯一时不读。严格队名零候选且日期≤7天时可用原文片段召回，source_fragment_candidate仅表示候选，不是球队同名证明。"""
    if record_source_intent:
        _require_source_record_scope(read_markets, market_kind, period, market_query, evidence_text)
    if team_query is None:
        if not team_a or not team_a.strip() or not team_b or not team_b.strip():
            raise ToolError('提供team_query，或同时提供原文明示且非空的team_a和team_b')
        team_query = team_a
    result = await _search_source_scopes({
        "review_id": review_id, "attempt_id": attempt_id, "team_query": team_query,
        "phase": phase, "competition_query": competition_query, "sport": sport,
        "team_a": team_a, "team_b": team_b,
        "from_utc": from_utc, "to_utc": to_utc, "cursor": cursor, "page_size": page_size,
    })
    if read_markets and result.get('search_complete') is True and result.get('next_cursor') is None:
        return await _with_bound_markets(result, result.get('resolution'), review_id, attempt_id,
                                         market_kind, period, market_query=market_query,
                                         record_source_intent=record_source_intent, evidence_text=evidence_text,
                                         source_periods=source_periods)
    return result


@mcp.tool()
async def resolve_match(review_id: str, attempt_id: str,
                        search_key: Annotated[str, Field(min_length=64, max_length=64, pattern='^[0-9a-f]{64}$',
                            description="逐字复制本次search_matches回执的search_key（64位小写十六进制标识）；绝不是球队描述或自行生成的哈希。先连续翻页至search_complete=true。")],
                        team_a: str, team_b: str, competition: str | None = None,
                        source_phase: Annotated[Literal['scheduled', 'live', 'finished', 'cancelled'] | None,
                            Field(description='仅原文或所选原图明示赛前/滚球/完场/取消时传；未知null，不按候选可售状态猜。仅在完整搜索内消歧，不替代盘口期间。')] = None,
                        from_utc: str | None = None, to_utc: str | None = None,
                        read_markets: bool = False, market_kind: str | None = None,
                        period: str | None = None,
                        source_periods: SourcePeriods = None,
                        market_query: Annotated[str | None, Field(max_length=120,
                            description="原文明示的盘口查询，例如小3.5、大0.5/1；仅传给唯一绑定后的读盘，不改变比赛消歧依据。")] = None,
                        record_source_intent: Annotated[bool, Field(description="来源明确total玩法、期间、方向及线值时传true并提供原文证据；仅已完成唯一绑定及精确唯一有效读盘后复用原生录入，record_result才是接受事实，默认false。")] = False,
                        evidence_text: Annotated[str | None, Field(max_length=3000,
                            description="本项来源推荐证据，与record_source_intent=true一起提供，不从库存反推。")] = None) -> dict:
    """全候选翻页结束后，用双方系统队名或常见别名、联赛和时间唯一消歧。来源已明确单项total/期间/方向/数字线时，同时传read_markets=true、market_kind/period/market_query、record_source_intent=true和evidence_text，在完整唯一有效读盘后复用原生录入；检查record_result，不重复提交已接受项。支持常见缩写、标点/重音差异和清晰队名前缀，多候选返回未解决。优先使用候选中的标准队名。其它玩法/期间明确时read_markets=true可在唯一活动绑定后返回market_result，复用读盘减少模型轮次；失败仍保留工具错误。"""
    if record_source_intent:
        _require_source_record_scope(read_markets, market_kind, period, market_query, evidence_text)
    result = await _post("resolve_match", {
        "review_id": review_id, "attempt_id": attempt_id, "search_key": search_key,
        "team_a": team_a, "team_b": team_b, "competition": competition,
        "source_phase": source_phase,
        "from_utc": from_utc, "to_utc": to_utc,
    })
    if read_markets:
        return await _with_bound_markets(result, result, review_id, attempt_id, market_kind, period,
                                         search_key=search_key, market_query=market_query,
                                         record_source_intent=record_source_intent, evidence_text=evidence_text,
                                         source_periods=source_periods)
    return result


@mcp.tool()
async def list_match_markets(review_id: str, attempt_id: str, fixture_id: str,
                             market_kind: str | None = None, period: str | None = None,
                             line: str | None = None, selection: str | None = None,
                             market_query: str | None = None,
                             source_periods: SourcePeriods = None,
                             record_source_intent: Annotated[bool, Field(description='明确来源total期间、方向与原线时可显式请求精确读盘后录入；需market_query/evidence_text及首读前source_periods，以record_result为准。未知期间保持false。')] = False,
                             evidence_text: Annotated[str | None, Field(max_length=3000)] = None) -> dict:
    """读取已绑定比赛的盘口。来源明确total/期间/原线/方向时，传record_source_intent=true、market_query、evidence_text及原source_periods；恢复读盘同样立即复用精确唯一有效项录入，以record_result为准。未知期间不录入，不主动轮询绕过限流。按类别、期间、方向或名称筛选，按selection_line匹配所选方让受让；line_basis=provider_home时line为原生主队线、客队selection_line符号相反，line筛选须用原生线，未确认口径先读玩法期间方向整组。先核对market_coverage：total_quotes=0且complete_for_filter=true是完整零库存，停止同场重复读取并保留未解决；仅有库存但筛选零命中或覆盖不足时放宽筛选，省略筛选返回完整盘口。赔率不参与意图匹配；can_record表示服务端校验通过。"""
    if record_source_intent:
        _require_source_record_scope(True, market_kind, period, market_query, evidence_text)
        if not source_periods or period not in source_periods:
            raise ToolError('精确读盘录入需要首读前已确认的来源期间清单；未知期间不录入')
    markets = await _read_source_markets({
        "review_id": review_id, "attempt_id": attempt_id, "fixture_id": fixture_id,
        "market_kind": market_kind, "period": period, "line": line,
        "selection": selection, "market_query": market_query,
        **_source_period_parameters(source_periods),
    })
    if not record_source_intent:
        return markets
    return await _record_bound_markets({'market_result': markets}, markets,
        {'id': fixture_id}, review_id, attempt_id, market_kind, period, evidence_text)


@mcp.tool()
async def record_expert_signals(review_id: str, attempt_id: str, signals: list[ExpertPick]) -> dict:
    """立即提交当前比赛已读的确切quote_id；系统核验同一盘口及比分上下文，按当前有效系统赔率记录。价格刷新不改变推荐意图；检查accepted/rejected及实际保存价格。rejection_guidance的retry_same_evidence=false表示同证据不可恢复：报告该项未解决，不反复换盘口或刷新，其他独立推荐继续核验。"""
    result = await _post("record_expert_signals", {"review_id": review_id, "attempt_id": attempt_id, "signals": [pick.model_dump() for pick in signals]})
    guidance = [{'index': item['index'], 'reason': item['reason'],
                 'retry_same_evidence': False, 'action': 'report_unresolved'}
                for item in result.get('rejected', []) if isinstance(item, dict)
                and type(item.get('index')) is int and item.get('reason') == 'source_period_intent_ambiguous']
    return result | {'rejection_guidance': guidance} if guidance else result


class BearerAuthentication:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        supplied = headers.get(b"authorization", b"").decode("latin-1")
        if not supplied.lower().startswith("bearer ") or not hmac.compare_digest(supplied[7:].strip(), API_TOKEN):
            await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"detail":"service authentication required"}'})
            return
        profiles = [value for name, value in scope.get('headers', [])
                    if name.lower() == b'x-football-tool-profile']
        token = compact_descriptions.set(profiles in (
            [b'parallel-compact-v1'], [b'parallel-table-v1'], [b'parallel-intent-v1'],
            [b'parallel-budget-v1']))
        table_token = compact_market_tables.set(profiles in (
            [b'market-table-v1'], [b'parallel-table-v1'],
            [b'market-intent-v1'], [b'parallel-intent-v1'],
            [b'market-budget-v1'], [b'parallel-budget-v1']))
        intent_token = explicit_market_intent.set(profiles in (
            [b'market-intent-v1'], [b'parallel-intent-v1'],
            [b'market-budget-v1'], [b'parallel-budget-v1']))
        budget_token = budget_descriptions.set(profiles in (
            [b'market-budget-v1'], [b'parallel-budget-v1']))
        try:
            await self.app(scope, receive, send)
        finally:
            compact_descriptions.reset(token)
            compact_market_tables.reset(table_token)
            explicit_market_intent.reset(intent_token)
            budget_descriptions.reset(budget_token)


async def main() -> None:
    import uvicorn

    application = create_application()
    try:
        await uvicorn.Server(uvicorn.Config(application, host=MCP_HOST, port=MCP_PORT, log_level="warning")).serve()
    finally:
        await client.aclose()


def create_application():
    return BearerAuthentication(mcp.streamable_http_app(
        host=MCP_HOST, stateless_http=True, json_response=True,
        transport_security=TransportSecuritySettings(
            allowed_hosts=['hermes-mcp:8090', '127.0.0.1:8090', 'localhost:8090'],
            allowed_origins=[],
        ),
    ))


if __name__ == "__main__":
    asyncio.run(main())
