"""Async database engine and session management."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aisp.config import get_settings
from aisp.db.models import Base

logger = logging.getLogger(__name__)

_STK_DAILY_V2_COLUMNS = [
    ("main_net", "FLOAT"),
    ("main_pct", "FLOAT"),
    ("super_large_net", "FLOAT"),
    ("super_large_pct", "FLOAT"),
    ("large_net", "FLOAT"),
    ("large_pct", "FLOAT"),
    ("medium_net", "FLOAT"),
    ("medium_pct", "FLOAT"),
    ("small_net", "FLOAT"),
    ("small_pct", "FLOAT"),
    ("pe_ttm", "FLOAT"),
    ("pb_mrq", "FLOAT"),
]


def _ensure_db_dir(url: str) -> None:
    """Create parent directory for SQLite file if needed."""
    if "sqlite" in url:
        # Extract path from URL like sqlite+aiosqlite:///data/aisp.db
        db_path = url.split("///")[-1]
        if db_path:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)


def get_engine(url: str | None = None):
    db_url = url or get_settings().db.url
    _ensure_db_dir(db_url)
    return create_async_engine(db_url, echo=False)


def get_session_factory(engine=None) -> async_sessionmaker[AsyncSession]:
    if engine is None:
        engine = get_engine()
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def _migrate_stk_daily_v2(conn) -> None:
    """Add new columns to stk_daily if missing (ALTER TABLE for existing DBs)."""
    result = await conn.execute(text("PRAGMA table_info(stk_daily)"))
    existing = {row[1] for row in result.fetchall()}

    for col_name, col_type in _STK_DAILY_V2_COLUMNS:
        if col_name not in existing:
            await conn.execute(
                text(f"ALTER TABLE stk_daily ADD COLUMN {col_name} {col_type}")
            )
            logger.info("Added column stk_daily.%s", col_name)


async def init_db(url: str | None = None) -> None:
    """Create all tables and run migrations."""
    engine = get_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_stk_daily_v2(conn)
    await engine.dispose()
