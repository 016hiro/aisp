"""Async database engine and session management."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aisp.config import get_settings
from aisp.db.models import Base


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


async def init_db(url: str | None = None) -> None:
    """Create all tables."""
    engine = get_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await engine.dispose()
