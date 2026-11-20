"""Database-local process exposing only the four existing review tools."""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import database
from .config import load_settings
from .http_runtime import register_error_handlers, request_context
from .routes import reviews
from .runtime_settings import runtime_quote_max_age


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = load_settings(role='mcp')
    await database.open_pool(app.state.settings, min_size=1, max_size=8, connect_timeout=5)
    try:
        yield
    finally:
        await database.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(title='Football Review Tools', version='2.0.0', docs_url=None,
                  redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.middleware('http')(request_context)
    register_error_handlers(app)
    app.include_router(reviews.router)

    @app.get('/healthz')
    async def healthz():
        return {'status': 'ok', 'version': '2.0.0', 'role': 'mcp'}

    @app.get('/readyz')
    async def readyz():
        await database.current_pool().fetchval('SELECT 1', timeout=2)
        await runtime_quote_max_age(app.state.settings)
        return {'status': 'ready', 'role': 'mcp'}

    return app


app = create_app()
