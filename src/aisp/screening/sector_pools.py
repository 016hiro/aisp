"""Three-pool sector management: Core, Momentum, Opportunity."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from aisp.config import get_settings
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import (
    PoolType,
    SectorDaily,
    SectorPoolHistory,
    SectorPoolState,
    TradingCalendar,
)

logger = logging.getLogger(__name__)


@dataclass
class PoolResult:
    """Result of pool update for a single pool type."""

    pool_type: PoolType
    active_sectors: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


class SectorPoolManager:
    """Manages the three sector pools: Core, Momentum, Opportunity."""

    def __init__(self):
        self.settings = get_settings()
        self.pool_config = self.settings.pool
        # Load core sectors from config/sectors.toml if not set via env
        if not self.pool_config.core_sectors:
            from aisp.data.symbols import load_core_sectors

            self.pool_config.core_sectors = load_core_sectors()

    async def update_pools(self, trade_date: date) -> list[PoolResult]:
        """Update all three pools for the given trade date.

        Returns list of PoolResult with current active sectors per pool.
        """
        results = []

        # 1. Core pool (static, from config)
        core_result = await self._update_core_pool(trade_date)
        results.append(core_result)

        # 2. Momentum pool (top N by daily change)
        momentum_result = await self._update_momentum_pool(trade_date)
        results.append(momentum_result)

        # 3. Opportunity pool (top N losers with MA60 uptrend + low volume)
        opportunity_result = await self._update_opportunity_pool(trade_date)
        results.append(opportunity_result)

        return results

    async def _update_core_pool(self, trade_date: date) -> PoolResult:
        """Core pool: configured sectors, never removed."""
        result = PoolResult(pool_type=PoolType.CORE)
        engine = get_engine()
        session_factory = get_session_factory(engine)

        async with session_factory() as session:
            for sector_name in self.pool_config.core_sectors:
                # Upsert into pool state
                stmt = (
                    sqlite_upsert(SectorPoolState)
                    .values(
                        sector_name=sector_name,
                        pool_type=PoolType.CORE,
                        entry_date=trade_date,
                        is_active=True,
                    )
                    .on_conflict_do_update(
                        index_elements=["sector_name", "pool_type"],
                        set_={"is_active": True},
                    )
                )
                await session.execute(stmt)

                # Log history
                hist_stmt = (
                    sqlite_upsert(SectorPoolHistory)
                    .values(
                        trade_date=trade_date,
                        sector_name=sector_name,
                        pool_type=PoolType.CORE,
                        is_qualified=True,
                        metric_value=None,
                        rank=None,
                    )
                    .on_conflict_do_update(
                        index_elements=["trade_date", "sector_name", "pool_type"],
                        set_={"is_qualified": True},
                    )
                )
                await session.execute(hist_stmt)

                result.active_sectors.append(sector_name)

            await session.commit()
        await engine.dispose()

        logger.info("Core pool: %d sectors", len(result.active_sectors))
        return result

    async def _update_momentum_pool(self, trade_date: date) -> PoolResult:
        """Momentum pool: top N sectors by daily change.

        Exit rule: If a sector is not in top N for `momentum_exit_days`
        consecutive trading days, remove it.
        """
        result = PoolResult(pool_type=PoolType.MOMENTUM)
        top_n = self.pool_config.momentum_top_n
        exit_days = self.pool_config.momentum_exit_days

        engine = get_engine()
        session_factory = get_session_factory(engine)

        async with session_factory() as session:
            # Get top N sectors by change_pct today
            sectors_query = await session.execute(
                select(SectorDaily.sector_name, SectorDaily.change_pct)
                .where(SectorDaily.trade_date == trade_date)
                .order_by(SectorDaily.change_pct.desc())
                .limit(top_n)
            )
            top_sectors = sectors_query.all()
            qualified_names = {row[0] for row in top_sectors}

            # Log qualification for ALL sectors today
            all_sectors_query = await session.execute(
                select(SectorDaily.sector_name, SectorDaily.change_pct)
                .where(SectorDaily.trade_date == trade_date)
                .order_by(SectorDaily.change_pct.desc())
            )
            all_sectors = all_sectors_query.all()

            for rank, (sector_name, change_pct) in enumerate(all_sectors, 1):
                is_qualified = sector_name in qualified_names
                hist_stmt = (
                    sqlite_upsert(SectorPoolHistory)
                    .values(
                        trade_date=trade_date,
                        sector_name=sector_name,
                        pool_type=PoolType.MOMENTUM,
                        is_qualified=is_qualified,
                        metric_value=change_pct,
                        rank=rank,
                    )
                    .on_conflict_do_update(
                        index_elements=["trade_date", "sector_name", "pool_type"],
                        set_={
                            "is_qualified": is_qualified,
                            "metric_value": change_pct,
                            "rank": rank,
                        },
                    )
                )
                await session.execute(hist_stmt)

            # Get currently active momentum sectors
            active_query = await session.execute(
                select(SectorPoolState.sector_name).where(
                    SectorPoolState.pool_type == PoolType.MOMENTUM,
                    SectorPoolState.is_active.is_(True),
                )
            )
            current_active = {row[0] for row in active_query.all()}

            # Add new qualifiers
            for sector_name in qualified_names:
                if sector_name not in current_active:
                    stmt = (
                        sqlite_upsert(SectorPoolState)
                        .values(
                            sector_name=sector_name,
                            pool_type=PoolType.MOMENTUM,
                            entry_date=trade_date,
                            is_active=True,
                        )
                        .on_conflict_do_update(
                            index_elements=["sector_name", "pool_type"],
                            set_={"is_active": True, "entry_date": trade_date, "exit_date": None},
                        )
                    )
                    await session.execute(stmt)
                    result.added.append(sector_name)
                result.active_sectors.append(sector_name)

            # Check exit rule for sectors NOT in today's top N
            for sector_name in current_active - qualified_names:
                consecutive_unqualified = await self._count_consecutive_unqualified(
                    session, sector_name, PoolType.MOMENTUM, trade_date
                )

                if consecutive_unqualified >= exit_days:
                    # Remove from pool
                    await session.execute(
                        update(SectorPoolState)
                        .where(
                            SectorPoolState.sector_name == sector_name,
                            SectorPoolState.pool_type == PoolType.MOMENTUM,
                        )
                        .values(is_active=False, exit_date=trade_date)
                    )
                    result.removed.append(sector_name)
                    logger.info(
                        "Momentum pool: removed %s (unqualified %d days)",
                        sector_name,
                        consecutive_unqualified,
                    )
                else:
                    result.active_sectors.append(sector_name)

            await session.commit()
        await engine.dispose()

        logger.info(
            "Momentum pool: %d active, +%d added, -%d removed",
            len(result.active_sectors),
            len(result.added),
            len(result.removed),
        )
        return result

    async def _update_opportunity_pool(self, trade_date: date) -> PoolResult:
        """Opportunity pool: top N losers with MA60 uptrend + low volume.

        Sectors where price is dropping but long-term trend is up (potential reversal).
        Observe for `opportunity_observe_days` before activating.
        """
        result = PoolResult(pool_type=PoolType.OPPORTUNITY)
        top_n = self.pool_config.opportunity_top_n

        engine = get_engine()
        session_factory = get_session_factory(engine)

        async with session_factory() as session:
            # Get sectors with data today. Filter: worst performers with MA60 uptrend
            sectors_query = await session.execute(
                select(
                    SectorDaily.sector_name,
                    SectorDaily.change_pct,
                    SectorDaily.volume,
                    SectorDaily.ma60,
                    SectorDaily.close,
                )
                .where(SectorDaily.trade_date == trade_date)
                .order_by(SectorDaily.change_pct.asc())  # Worst first
            )
            all_sectors = sectors_query.all()

            # Filter: MA60 exists and close > MA60 (uptrend), then take worst performers
            qualified: list[tuple[str, float, int]] = []
            for rank, (sector_name, change_pct, _volume, ma60, close) in enumerate(
                all_sectors, 1
            ):
                if ma60 is not None and close > ma60 and change_pct < 0:
                    qualified.append((sector_name, change_pct, rank))

            # Take top_n worst performers that meet criteria
            opportunity_sectors = qualified[:top_n]
            qualified_names = {s[0] for s in opportunity_sectors}

            # Log history
            for sector_name, change_pct, rank in opportunity_sectors:
                hist_stmt = (
                    sqlite_upsert(SectorPoolHistory)
                    .values(
                        trade_date=trade_date,
                        sector_name=sector_name,
                        pool_type=PoolType.OPPORTUNITY,
                        is_qualified=True,
                        metric_value=change_pct,
                        rank=rank,
                    )
                    .on_conflict_do_update(
                        index_elements=["trade_date", "sector_name", "pool_type"],
                        set_={
                            "is_qualified": True,
                            "metric_value": change_pct,
                            "rank": rank,
                        },
                    )
                )
                await session.execute(hist_stmt)

            # Update pool state
            for sector_name in qualified_names:
                stmt = (
                    sqlite_upsert(SectorPoolState)
                    .values(
                        sector_name=sector_name,
                        pool_type=PoolType.OPPORTUNITY,
                        entry_date=trade_date,
                        is_active=True,
                    )
                    .on_conflict_do_update(
                        index_elements=["sector_name", "pool_type"],
                        set_={"is_active": True, "entry_date": trade_date, "exit_date": None},
                    )
                )
                await session.execute(stmt)
                result.active_sectors.append(sector_name)
                result.added.append(sector_name)

            # Remove opportunity sectors not qualified for observe_days
            active_query = await session.execute(
                select(SectorPoolState.sector_name).where(
                    SectorPoolState.pool_type == PoolType.OPPORTUNITY,
                    SectorPoolState.is_active.is_(True),
                )
            )
            current_active = {row[0] for row in active_query.all()}

            observe_days = self.pool_config.opportunity_observe_days
            for sector_name in current_active - qualified_names:
                consecutive_unqualified = await self._count_consecutive_unqualified(
                    session, sector_name, PoolType.OPPORTUNITY, trade_date
                )
                if consecutive_unqualified >= observe_days:
                    await session.execute(
                        update(SectorPoolState)
                        .where(
                            SectorPoolState.sector_name == sector_name,
                            SectorPoolState.pool_type == PoolType.OPPORTUNITY,
                        )
                        .values(is_active=False, exit_date=trade_date)
                    )
                    result.removed.append(sector_name)
                else:
                    result.active_sectors.append(sector_name)

            await session.commit()
        await engine.dispose()

        logger.info(
            "Opportunity pool: %d active, +%d added, -%d removed",
            len(result.active_sectors),
            len(result.added),
            len(result.removed),
        )
        return result

    async def _count_consecutive_unqualified(
        self, session, sector_name: str, pool_type: PoolType, as_of: date
    ) -> int:
        """Count consecutive trading days a sector has been unqualified.

        Derives the count from sector_pool_history: find the most recent
        is_qualified=True date and count trading days since then.
        """
        # Find the most recent qualified date
        result = await session.execute(
            select(SectorPoolHistory.trade_date)
            .where(
                SectorPoolHistory.sector_name == sector_name,
                SectorPoolHistory.pool_type == pool_type,
                SectorPoolHistory.is_qualified.is_(True),
                SectorPoolHistory.trade_date <= as_of,
            )
            .order_by(SectorPoolHistory.trade_date.desc())
            .limit(1)
        )
        last_qualified = result.scalar_one_or_none()

        if last_qualified is None:
            # Never qualified — count all history days
            count_result = await session.execute(
                select(SectorPoolHistory.trade_date)
                .where(
                    SectorPoolHistory.sector_name == sector_name,
                    SectorPoolHistory.pool_type == pool_type,
                    SectorPoolHistory.is_qualified.is_(False),
                    SectorPoolHistory.trade_date <= as_of,
                )
            )
            return len(count_result.all())

        # Count trading days between last_qualified and as_of
        cal_result = await session.execute(
            select(TradingCalendar.cal_date).where(
                TradingCalendar.cal_date > last_qualified,
                TradingCalendar.cal_date <= as_of,
                TradingCalendar.is_trading_day.is_(True),
            )
        )
        return len(cal_result.all())

    async def get_active_pools(self) -> dict[PoolType, list[str]]:
        """Get all currently active sectors grouped by pool type."""
        engine = get_engine()
        session_factory = get_session_factory(engine)
        async with session_factory() as session:
            result = await session.execute(
                select(SectorPoolState.pool_type, SectorPoolState.sector_name).where(
                    SectorPoolState.is_active.is_(True)
                )
            )
            pools: dict[PoolType, list[str]] = {
                PoolType.CORE: [],
                PoolType.MOMENTUM: [],
                PoolType.OPPORTUNITY: [],
            }
            for pool_type, sector_name in result.all():
                pools[pool_type].append(sector_name)
        await engine.dispose()
        return pools
