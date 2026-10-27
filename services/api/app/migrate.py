"""独立结构升级入口；不启动 API 或读取模型、用户秘密。"""

import asyncio
import logging

import asyncpg

from .config import database_url
from .database import apply_migrations


async def run() -> None:
    conn = await asyncpg.connect(database_url(), timeout=10, command_timeout=65)
    try:
        versions = await apply_migrations(conn)
        logging.getLogger(__name__).info("schema migration completed applied_count=%d", len(versions))
    finally:
        await conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run())
    except Exception as exc:
        logging.getLogger(__name__).error("schema migration failed category=%s", type(exc).__name__)
        raise SystemExit(1) from None
