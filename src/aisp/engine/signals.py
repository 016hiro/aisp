"""Signal generation, storage, and exit logic."""

from __future__ import annotations

import logging
from datetime import date

from rich.console import Console
from rich.table import Table
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import (
    DailySignals,
    Direction,
    PoolType,
    SectorPoolState,
    StkDaily,
)

logger = logging.getLogger(__name__)
console = Console()


async def generate_signals(signals_data: list[dict], trade_date: date) -> int:
    """Store generated signals into daily_signals table.

    Returns number of signals stored.
    """
    if not signals_data:
        return 0

    engine = get_engine()
    session_factory = get_session_factory(engine)

    async with session_factory() as session:
        for sig in signals_data:
            pool_type = sig.get("pool_type")
            direction = sig.get("direction")

            # Convert enum values if needed
            if isinstance(pool_type, PoolType):
                pool_type_val = pool_type.value
            elif pool_type:
                pool_type_val = pool_type
            else:
                pool_type_val = None

            if isinstance(direction, Direction):
                direction_val = direction.value
            elif direction:
                direction_val = direction
            else:
                direction_val = Direction.WATCH.value

            rec = {
                "trade_date": trade_date,
                "code": sig["code"],
                "name": sig["name"],
                "sector": sig["sector"],
                "pool_type": pool_type_val,
                "direction": direction_val,
                "score": sig["score"],
                "factor_scores": sig.get("factor_scores"),
                "confidence": sig.get("confidence", 0.5),
                "reasoning": sig.get("reasoning", ""),
            }

            stmt = (
                sqlite_upsert(DailySignals)
                .values(**rec)
                .on_conflict_do_update(
                    index_elements=["trade_date", "code"],
                    set_={
                        k: v
                        for k, v in rec.items()
                        if k not in ("trade_date", "code")
                    },
                )
            )
            await session.execute(stmt)

        await session.commit()
    await engine.dispose()

    logger.info("Stored %d signals for %s", len(signals_data), trade_date)
    return len(signals_data)


def _check_hard_stop(entry_price: float, current_price: float, is_st: bool = False) -> bool:
    """Check hard stop-loss: -5% for ST, -10% for normal stocks."""
    threshold = -0.05 if is_st else -0.10
    pct_change = (current_price - entry_price) / entry_price
    return pct_change <= threshold


def _check_trailing_stop(
    entry_price: float, high_since_entry: float, current_price: float
) -> bool:
    """Check trailing stop: 50% retracement from high."""
    if high_since_entry <= entry_price:
        return False
    profit_from_high = high_since_entry - entry_price
    current_profit = current_price - entry_price
    return bool(profit_from_high > 0 and current_profit < profit_from_high * 0.5)


async def check_exit_signals(trade_date: date) -> list[dict]:
    """Check exit conditions for active BUY signals (V1: output only, no execution).

    Returns list of exit signal recommendations.
    """
    engine = get_engine()
    session_factory = get_session_factory(engine)
    exits = []

    async with session_factory() as session:
        # Get active BUY signals
        result = await session.execute(
            select(DailySignals).where(
                DailySignals.direction == Direction.BUY,
                DailySignals.trade_date <= trade_date,
            )
        )
        buy_signals = result.scalars().all()

        for signal in buy_signals:
            # Get current price
            price_result = await session.execute(
                select(StkDaily).where(
                    StkDaily.code == signal.code,
                    StkDaily.trade_date == trade_date,
                )
            )
            current = price_result.scalar_one_or_none()
            if not current:
                continue

            # For V1, use signal date's close as entry
            entry_result = await session.execute(
                select(StkDaily.close).where(
                    StkDaily.code == signal.code,
                    StkDaily.trade_date == signal.trade_date,
                )
            )
            entry_close = entry_result.scalar_one_or_none()
            if not entry_close:
                continue

            reasons = []
            if _check_hard_stop(entry_close, current.close, current.is_st):
                reasons.append("hard_stop_loss")
            if _check_trailing_stop(entry_close, current.high, current.close):
                reasons.append("trailing_stop")

            if reasons:
                exits.append(
                    {
                        "code": signal.code,
                        "name": signal.name,
                        "entry_date": signal.trade_date,
                        "entry_price": entry_close,
                        "current_price": current.close,
                        "reasons": reasons,
                    }
                )

    await engine.dispose()
    return exits


async def show_status() -> None:
    """Display current pool state and active signals."""
    engine = get_engine()
    session_factory = get_session_factory(engine)

    async with session_factory() as session:
        # Pool status
        pool_result = await session.execute(
            select(SectorPoolState).where(SectorPoolState.is_active.is_(True))
        )
        pools = pool_result.scalars().all()

        pool_table = Table(title="Active Sector Pools")
        pool_table.add_column("Pool Type", style="cyan")
        pool_table.add_column("Sector", style="green")
        pool_table.add_column("Entry Date")

        for p in pools:
            pool_table.add_row(p.pool_type.value, p.sector_name, str(p.entry_date))

        console.print(pool_table)

        # Recent signals
        signals_result = await session.execute(
            select(DailySignals)
            .order_by(DailySignals.trade_date.desc())
            .limit(20)
        )
        signals = signals_result.scalars().all()

        signal_table = Table(title="Recent Signals")
        signal_table.add_column("Date", style="cyan")
        signal_table.add_column("Code")
        signal_table.add_column("Name", style="green")
        signal_table.add_column("Direction", style="bold")
        signal_table.add_column("Score")
        signal_table.add_column("Confidence")
        signal_table.add_column("Sector")

        for s in signals:
            dir_style = {
                Direction.BUY: "bold green",
                Direction.SELL: "bold red",
                Direction.HOLD: "yellow",
                Direction.WATCH: "dim",
            }.get(s.direction, "")

            signal_table.add_row(
                str(s.trade_date),
                s.code,
                s.name,
                f"[{dir_style}]{s.direction.value}[/{dir_style}]",
                f"{s.score:.4f}",
                f"{s.confidence:.2f}",
                s.sector,
            )

        console.print(signal_table)

    await engine.dispose()
