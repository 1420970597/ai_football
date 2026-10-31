from __future__ import annotations

import asyncio
import hmac
import os

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
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
mcp = MCPServer("football-expert-platform")


class ExpertPick(BaseModel):
    fixture_id: str = Field(min_length=1, max_length=160)
    quote_id: str = Field(min_length=1, max_length=80)
    evidence_text: str = Field(min_length=1, max_length=3000)
    confidence: float = Field(default=0.5, ge=0, le=1)


async def _post(path: str, payload: dict) -> dict:
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


@mcp.tool()
async def search_matches(review_id: str, attempt_id: str, team_query: str,
                         phase: str = "all", competition_query: str | None = None,
                         from_utc: str | None = None, to_utc: str | None = None,
                         cursor: str | None = None, page_size: int = 100, sport: str | None = None) -> dict:
    """phase=all检索赛前、滚球、完场及取消候选；支持常见足球简称与重音/标点变体。每页最多100场，连续使用next_cursor直到为空。仅realtime_eligible=true的唯一比赛可读取实时盘口并录入推荐。"""
    return await _post("search_matches", {
        "review_id": review_id, "attempt_id": attempt_id, "team_query": team_query,
        "phase": phase, "competition_query": competition_query, "sport": sport,
        "from_utc": from_utc, "to_utc": to_utc, "cursor": cursor, "page_size": page_size,
    })


@mcp.tool()
async def resolve_match(review_id: str, attempt_id: str, search_key: str,
                        team_a: str, team_b: str, competition: str | None = None,
                        from_utc: str | None = None, to_utc: str | None = None) -> dict:
    """全候选翻页结束后，用双方系统队名或常见别名、联赛和时间唯一消歧；支持常见缩写、标点/重音差异和清晰队名前缀，多候选返回未解决。优先使用候选中的标准队名。"""
    return await _post("resolve_match", {
        "review_id": review_id, "attempt_id": attempt_id, "search_key": search_key,
        "team_a": team_a, "team_b": team_b, "competition": competition,
        "from_utc": from_utc, "to_utc": to_utc,
    })


@mcp.tool()
async def list_match_markets(review_id: str, attempt_id: str, fixture_id: str) -> dict:
    """读取已唯一消歧比赛的全部系统盘口；can_record表示源时间有效或精确当前盘口复核有效。"""
    return await _post("list_match_markets", {"review_id": review_id, "attempt_id": attempt_id, "fixture_id": fixture_id})


@mcp.tool()
async def record_expert_signals(review_id: str, attempt_id: str, signals: list[ExpertPick]) -> dict:
    """立即提交当前比赛已读的确切quote_id；必要时系统重新复核原盘，未变价才记录，检查accepted/rejected。"""
    return await _post("record_expert_signals", {"review_id": review_id, "attempt_id": attempt_id, "signals": [pick.model_dump() for pick in signals]})


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
        await self.app(scope, receive, send)


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
