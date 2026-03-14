"""Image deduplication via SHA256 hash."""

from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select

from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import ImageHash


def compute_hash(data: bytes) -> str:
    """Compute SHA256 hex digest for raw image bytes."""
    return hashlib.sha256(data).hexdigest()


async def check_duplicate(hash_value: str) -> ImageHash | None:
    """Return existing ImageHash row if this hash was already processed."""
    engine = get_engine()
    sf = get_session_factory(engine)
    async with sf() as session:
        result = await session.execute(
            select(ImageHash).where(ImageHash.hash == hash_value)
        )
        row = result.scalar_one_or_none()
    await engine.dispose()
    return row


async def record_hash(hash_value: str, source_type: str, summary: str) -> None:
    """Record a processed image hash in the database."""
    engine = get_engine()
    sf = get_session_factory(engine)
    async with sf() as session:
        session.add(ImageHash(
            hash=hash_value,
            source_type=source_type,
            result_summary=summary,
            processed_at=datetime.now(),
        ))
        await session.commit()
    await engine.dispose()
