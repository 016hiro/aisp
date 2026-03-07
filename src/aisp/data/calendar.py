"""A-share trading calendar management."""

from __future__ import annotations

import asyncio
import bisect
import logging
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from aisp.data import with_retry
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import TradingCalendar

logger = logging.getLogger(__name__)


@with_retry(max_retries=3)
async def _fetch_trade_dates() -> list[date]:
    """Fetch historical trading dates from AkShare."""
    import akshare as ak

    def _fetch():
        df = ak.tool_trade_date_hist_sina()
        return [d.date() if hasattr(d, "date") else d for d in df["trade_date"].tolist()]

    return await asyncio.to_thread(_fetch)


async def init_trading_calendar() -> int:
    """Initialize or update the trading calendar table.

    Returns the number of records upserted.
    """
    trade_dates = await _fetch_trade_dates()
    if not trade_dates:
        logger.warning("No trading dates returned from AkShare")
        return 0

    trade_date_set = set(trade_dates)
    sorted_dates = sorted(trade_dates)
    # Build index for O(1) lookup
    date_to_idx = {d: i for i, d in enumerate(sorted_dates)}

    # Generate calendar from earliest trading date to today + 365 days
    start = sorted_dates[0]
    end = max(date.today() + timedelta(days=365), sorted_dates[-1])

    records: list[dict] = []
    current = start
    while current <= end:
        is_trading = current in trade_date_set

        # Find prev/next trading date
        prev_td = None
        next_td = None

        if is_trading:
            idx = date_to_idx[current]
            if idx > 0:
                prev_td = sorted_dates[idx - 1]
            if idx < len(sorted_dates) - 1:
                next_td = sorted_dates[idx + 1]
        else:
            # Binary search for nearest prev trading date
            pos = bisect.bisect_left(sorted_dates, current)
            if pos > 0:
                prev_td = sorted_dates[pos - 1]
            if pos < len(sorted_dates):
                next_td = sorted_dates[pos]

        records.append(
            {
                "cal_date": current,
                "is_trading_day": is_trading,
                "prev_trading_date": prev_td,
                "next_trading_date": next_td,
            }
        )
        current += timedelta(days=1)

    # Batch upsert
    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        batch_size = 500
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            for rec in batch:
                stmt = (
                    sqlite_upsert(TradingCalendar)
                    .values(**rec)
                    .on_conflict_do_update(
                        index_elements=["cal_date"],
                        set_={
                            "is_trading_day": rec["is_trading_day"],
                            "prev_trading_date": rec["prev_trading_date"],
                            "next_trading_date": rec["next_trading_date"],
                        },
                    )
                )
                await session.execute(stmt)
            await session.flush()
        await session.commit()

    await engine.dispose()
    logger.info("Upserted %d calendar records", len(records))
    return len(records)


async def get_next_trading_date(d: date) -> date | None:
    """Get the next trading date after d."""
    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        result = await session.execute(
            select(TradingCalendar.next_trading_date).where(TradingCalendar.cal_date == d)
        )
        row = result.scalar_one_or_none()
    await engine.dispose()
    return row


async def get_prev_trading_date(d: date) -> date | None:
    """Get the previous trading date before d."""
    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        result = await session.execute(
            select(TradingCalendar.prev_trading_date).where(TradingCalendar.cal_date == d)
        )
        row = result.scalar_one_or_none()
    await engine.dispose()
    return row


async def is_trading_day(d: date) -> bool:
    """Check if a date is a trading day."""
    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        result = await session.execute(
            select(TradingCalendar.is_trading_day).where(TradingCalendar.cal_date == d)
        )
        row = result.scalar_one_or_none()
    await engine.dispose()
    return bool(row) if row is not None else False


async def get_trading_dates_between(start: date, end: date) -> list[date]:
    """Get all trading dates between start and end (inclusive)."""
    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        result = await session.execute(
            select(TradingCalendar.cal_date)
            .where(
                TradingCalendar.cal_date >= start,
                TradingCalendar.cal_date <= end,
                TradingCalendar.is_trading_day.is_(True),
            )
            .order_by(TradingCalendar.cal_date)
        )
        dates = [row[0] for row in result.all()]
    await engine.dispose()
    return dates
