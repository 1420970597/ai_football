from __future__ import annotations

import asyncio
import hmac
import json
import os
from contextvars import ContextVar
from typing import Annotated

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field

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
compact_market_tables: ContextVar[bool] = ContextVar('compact_market_tables', default=False)
explicit_market_intent: ContextVar[bool] = ContextVar('explicit_market_intent', default=False)
INTENT_ARGUMENT_DESCRIPTIONS = {
    'read_markets': '原文或图片已明确双方、玩法及期间的新增推荐，首次search_matches或消歧resolve_match传true，并同时传market_kind、period和market_query；只在完整搜索且唯一活动绑定后返回market_result。复用该结果，不重复list_match_markets；完整过滤结果中原期间/线/方向全部closed或suspended时直接说明原盘口不可用，禁止去掉筛选再读盘找替代线。未知意图保持false，不猜参数。',
    'market_kind': '原文明示的玩法：大小球用total，让球用handicap；与read_markets=true一起传，不从赔率猜玩法。其他玩法按工具已知原生类型核验。',
    'period': '按原文明示期间：全场full_time、上半场first_half、下半场second_half。已知时与read_markets=true一起传；不能把上半场换成全场，未知时不要猜。',
}


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
            if explicit_market_intent.get() and tool.name in {'search_matches', 'resolve_match'}:
                fields = ('read_markets', 'market_kind', 'period', 'market_query')
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
            result.append(tool.model_copy(update=updates) if updates else tool)
        return result

    async def call_tool(self, name: str, arguments: dict, context=None):
        result = await super().call_tool(name, arguments, context=context)
        return compact_tool_result(result)


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


async def _post(path: str, payload: dict) -> dict:
    global pending_market_requests
    if path not in {'list_match_markets', 'record_expert_signals'}:
        return await _request(path, payload)
    if pending_market_requests >= MAX_PENDING_MARKET_CALLS:
        raise ToolError("盘口复核队列繁忙；保留当前消息为待核验")
    pending_market_requests += 1
    try:
        return await _market_request(path, payload)
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
        raise ToolError(str(detail)[:240]) from None
    except httpx.HTTPError:
        raise ToolError("系统工具暂不可用") from None


async def _with_bound_markets(result: dict, resolution: dict | None,
                              review_id: str, attempt_id: str,
                              market_kind: str | None, period: str | None,
                              *, search_key: str | None = None,
                              market_query: str | None = None) -> dict:
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
        markets = await _post('list_match_markets', market_parameters)
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
    return {**result, 'market_result': markets}


@mcp.tool()
async def search_matches(review_id: str, attempt_id: str, team_query: str | None = None,
                         phase: str = "all", competition_query: str | None = None,
                         from_utc: str | None = None, to_utc: str | None = None,
                         cursor: Annotated[str | None, Field(description="仅逐字复制本次search_matches返回的next_cursor；首查不传，禁止自行构造游标。")] = None,
                         page_size: int = 100, sport: str | None = None,
                         team_a: Annotated[str | None, Field(description="原文或原图明确可读的第一支球队名；双方已知时首次查询即与team_b成对传入，复用服务端唯一绑定。")] = None,
                         team_b: Annotated[str | None, Field(description="原文或原图明确可读的第二支球队名；双方已知时首次查询即与team_a成对传入，不从候选猜造原文球队。")] = None,
                         read_markets: bool = False, market_kind: str | None = None,
                         period: str | None = None,
                         market_query: Annotated[str | None, Field(max_length=120,
                             description="原文明示的盘口查询，例如小3.5、大0.5/1；仅传给唯一绑定后的读盘，不参与球队检索，不比较倍率。")] = None) -> dict:
    """提供team_query，或同时提供原文明示的team_a/team_b并逐字复用team_a检索；显式team_query包括空串不被覆盖。原名或片段无双方对应候选时，可传空串展开同联赛：必须同时给competition_query、sport及正区间≤7天的from_utc/to_utc；competition_time_candidate只是待核验候选，不是身份绑定。phase=all检索全部阶段，连续分页至next_cursor为空。原文双方明确时可成对传team_a/team_b，仅完整检索结束后服务端唯一核验并返回resolution；resolved=true且realtime_eligible=true可直接读盘，否则用resolve_match继续核验。已知玩法/期间时read_markets=true可在唯一绑定后返回market_result，复用此读盘而不重复调用；期间使用full_time/first_half/second_half。不完整或不唯一时不读。严格队名零候选且日期≤7天时可用原文片段召回，source_fragment_candidate仅表示候选，不是球队同名证明。"""
    if team_query is None:
        if not team_a or not team_a.strip() or not team_b or not team_b.strip():
            raise ToolError('提供team_query，或同时提供原文明示且非空的team_a和team_b')
        team_query = team_a
    result = await _post("search_matches", {
        "review_id": review_id, "attempt_id": attempt_id, "team_query": team_query,
        "phase": phase, "competition_query": competition_query, "sport": sport,
        "team_a": team_a, "team_b": team_b,
        "from_utc": from_utc, "to_utc": to_utc, "cursor": cursor, "page_size": page_size,
    })
    if read_markets and result.get('search_complete') is True and result.get('next_cursor') is None:
        return await _with_bound_markets(result, result.get('resolution'), review_id, attempt_id,
                                         market_kind, period, market_query=market_query)
    return result


@mcp.tool()
async def resolve_match(review_id: str, attempt_id: str,
                        search_key: Annotated[str, Field(min_length=64, max_length=64, pattern='^[0-9a-f]{64}$',
                            description="逐字复制本次search_matches回执的search_key（64位小写十六进制标识）；绝不是球队描述或自行生成的哈希。先连续翻页至search_complete=true。")],
                        team_a: str, team_b: str, competition: str | None = None,
                        from_utc: str | None = None, to_utc: str | None = None,
                        read_markets: bool = False, market_kind: str | None = None,
                        period: str | None = None,
                        market_query: Annotated[str | None, Field(max_length=120,
                            description="原文明示的盘口查询，例如小3.5、大0.5/1；仅传给唯一绑定后的读盘，不改变比赛消歧依据。")] = None) -> dict:
    """全候选翻页结束后，用双方系统队名或常见别名、联赛和时间唯一消歧；支持常见缩写、标点/重音差异和清晰队名前缀，多候选返回未解决。优先使用候选中的标准队名。玩法/期间明确时read_markets=true可在唯一活动绑定后返回market_result，复用读盘减少模型轮次；失败仍保留工具错误。"""
    result = await _post("resolve_match", {
        "review_id": review_id, "attempt_id": attempt_id, "search_key": search_key,
        "team_a": team_a, "team_b": team_b, "competition": competition,
        "from_utc": from_utc, "to_utc": to_utc,
    })
    if read_markets:
        return await _with_bound_markets(result, result, review_id, attempt_id, market_kind, period,
                                         search_key=search_key, market_query=market_query)
    return result


@mcp.tool()
async def list_match_markets(review_id: str, attempt_id: str, fixture_id: str,
                             market_kind: str | None = None, period: str | None = None,
                             line: str | None = None, selection: str | None = None,
                             market_query: str | None = None) -> dict:
    """读取已绑定比赛的盘口。按类别、期间、方向或名称筛选，按selection_line匹配所选方让受让；line_basis=provider_home时line为原生主队线、客队selection_line符号相反，line筛选须用原生线，未确认口径先读玩法期间方向整组。先核对market_coverage：total_quotes=0且complete_for_filter=true是完整零库存，停止同场重复读取并保留未解决；仅有库存但筛选零命中或覆盖不足时放宽筛选，省略筛选返回完整盘口。赔率不参与意图匹配；can_record表示服务端校验通过。"""
    return await _post("list_match_markets", {
        "review_id": review_id, "attempt_id": attempt_id, "fixture_id": fixture_id,
        "market_kind": market_kind, "period": period, "line": line,
        "selection": selection, "market_query": market_query,
    })


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
            [b'parallel-compact-v1'], [b'parallel-table-v1'], [b'parallel-intent-v1']))
        table_token = compact_market_tables.set(profiles in (
            [b'market-table-v1'], [b'parallel-table-v1'],
            [b'market-intent-v1'], [b'parallel-intent-v1']))
        intent_token = explicit_market_intent.set(profiles in (
            [b'market-intent-v1'], [b'parallel-intent-v1']))
        try:
            await self.app(scope, receive, send)
        finally:
            compact_descriptions.reset(token)
            compact_market_tables.reset(table_token)
            explicit_market_intent.reset(intent_token)


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
