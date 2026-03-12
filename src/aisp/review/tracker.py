"""Signal performance tracking — T+1 evaluation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from aisp.data.calendar import get_next_trading_date
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import (
    BULLISH_DIRECTIONS,
    DailySignals,
    Evaluation,
    SignalPerformance,
    StkDaily,
)

logger = logging.getLogger(__name__)


@dataclass
class PerformanceStats:
    """Rolling performance statistics."""

    total_signals: int = 0
    evaluated: int = 0
    correct: int = 0
    wrong: int = 0
    neutral: int = 0
    pending: int = 0
    accuracy: float = 0.0
    avg_return: float = 0.0


class PerformanceTracker:
    """Track and evaluate signal performance on T+1 basis."""

    async def evaluate_signals(self, trade_date: date) -> int:
        """Evaluate BUY signals from previous trading days.

        For each BUY signal:
        - Entry price = signal day close price
        - T+1: next trading day's OHLC
        - Evaluation: correct if next_change_pct > 0, wrong if < -3%, neutral otherwise

        Returns number of signals evaluated.
        """
        engine = get_engine()
        session_factory = get_session_factory(engine)
        evaluated_count = 0

        async with session_factory() as session:
            # Find bullish signals that haven't been evaluated yet
            result = await session.execute(
                select(DailySignals).where(
                    DailySignals.direction.in_(
                        [d.value for d in BULLISH_DIRECTIONS]
                    ),
                    DailySignals.trade_date < trade_date,
                    ~DailySignals.id.in_(
                        select(SignalPerformance.signal_id).where(
                            SignalPerformance.evaluation != Evaluation.PENDING
                        )
                    ),
                )
            )
            signals = result.scalars().all()

            for signal in signals:
                try:
                    # Get next trading date after signal date
                    next_td = await get_next_trading_date(signal.trade_date)
                    if not next_td or next_td > trade_date:
                        continue  # Not yet evaluable

                    # Get entry price (signal day close)
                    entry_result = await session.execute(
                        select(StkDaily.close).where(
                            StkDaily.code == signal.code,
                            StkDaily.trade_date == signal.trade_date,
                        )
                    )
                    entry_price = entry_result.scalar_one_or_none()
                    if not entry_price:
                        continue

                    # Get next day OHLC
                    next_result = await session.execute(
                        select(StkDaily).where(
                            StkDaily.code == signal.code,
                            StkDaily.trade_date == next_td,
                        )
                    )
                    next_day = next_result.scalar_one_or_none()
                    if not next_day:
                        continue

                    # Calculate change from entry (open as actual entry for T+1)
                    next_change_pct = (
                        (next_day.close - next_day.open) / next_day.open * 100
                        if next_day.open > 0
                        else 0
                    )

                    # Evaluate
                    if next_change_pct > 0:
                        evaluation = Evaluation.CORRECT
                    elif next_change_pct < -3:
                        evaluation = Evaluation.WRONG
                    else:
                        evaluation = Evaluation.NEUTRAL

                    perf_rec = {
                        "signal_id": signal.id,
                        "signal_date": signal.trade_date,
                        "eval_date": next_td,
                        "code": signal.code,
                        "entry_price": entry_price,
                        "next_open": next_day.open,
                        "next_high": next_day.high,
                        "next_low": next_day.low,
                        "next_close": next_day.close,
                        "next_change_pct": round(next_change_pct, 4),
                        "evaluation": evaluation.value,
                        "evaluated_at": datetime.now(),
                    }

                    stmt = (
                        sqlite_upsert(SignalPerformance)
                        .values(**perf_rec)
                        .on_conflict_do_nothing()
                    )
                    await session.execute(stmt)
                    evaluated_count += 1

                except Exception:
                    logger.exception("Failed to evaluate signal %d", signal.id)

            await session.commit()
        await engine.dispose()

        logger.info("Evaluated %d signals for %s", evaluated_count, trade_date)
        return evaluated_count

    async def get_stats(self, lookback_days: int = 30) -> PerformanceStats:
        """Get rolling performance statistics."""
        engine = get_engine()
        session_factory = get_session_factory(engine)
        stats = PerformanceStats()

        async with session_factory() as session:
            # Total signals (bullish directions)
            total_result = await session.execute(
                select(func.count()).select_from(DailySignals).where(
                    DailySignals.direction.in_(
                        [d.value for d in BULLISH_DIRECTIONS]
                    )
                )
            )
            stats.total_signals = total_result.scalar() or 0

            # Evaluation counts
            for eval_type in [Evaluation.CORRECT, Evaluation.WRONG, Evaluation.NEUTRAL, Evaluation.PENDING]:
                count_result = await session.execute(
                    select(func.count())
                    .select_from(SignalPerformance)
                    .where(SignalPerformance.evaluation == eval_type)
                )
                count = count_result.scalar() or 0

                if eval_type == Evaluation.CORRECT:
                    stats.correct = count
                elif eval_type == Evaluation.WRONG:
                    stats.wrong = count
                elif eval_type == Evaluation.NEUTRAL:
                    stats.neutral = count
                elif eval_type == Evaluation.PENDING:
                    stats.pending = count

            stats.evaluated = stats.correct + stats.wrong + stats.neutral
            if stats.evaluated > 0:
                stats.accuracy = stats.correct / stats.evaluated

            # Average return
            avg_result = await session.execute(
                select(func.avg(SignalPerformance.next_change_pct)).where(
                    SignalPerformance.evaluation != Evaluation.PENDING
                )
            )
            stats.avg_return = avg_result.scalar() or 0.0

        await engine.dispose()
        return stats
