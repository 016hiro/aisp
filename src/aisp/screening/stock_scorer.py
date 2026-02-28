"""Multi-factor stock scoring within sector pools."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select

from aisp.config import get_settings
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import PoolType, StkDaily, StkSectorMap
from aisp.screening.sector_pools import PoolResult

logger = logging.getLogger(__name__)


@dataclass
class ScoredStock:
    """A scored stock with factor breakdown."""

    code: str
    name: str
    sector: str
    pool_type: PoolType
    total_score: float
    factor_scores: dict[str, float] = field(default_factory=dict)
    raw_data: dict = field(default_factory=dict)


class StockScorer:
    """Multi-factor stock scorer: fund flow, momentum, technical, quality."""

    def __init__(self):
        self.settings = get_settings()
        self.weights = self.settings.scoring

    async def score_all_pools(
        self, pool_results: list[PoolResult], trade_date: date
    ) -> list[ScoredStock]:
        """Score stocks across all active sector pools.

        Returns all scored stocks (top N per sector).
        """
        all_scored: list[ScoredStock] = []
        top_n = self.settings.pool.top_stocks_per_sector

        for pool_result in pool_results:
            for sector_name in pool_result.active_sectors:
                scored = await self._score_sector(
                    sector_name, pool_result.pool_type, trade_date
                )
                # Take top N per sector
                scored.sort(key=lambda s: s.total_score, reverse=True)
                all_scored.extend(scored[:top_n])

        logger.info("Scored %d stocks across all pools", len(all_scored))
        return all_scored

    async def _score_sector(
        self, sector_name: str, pool_type: PoolType, trade_date: date
    ) -> list[ScoredStock]:
        """Score all stocks in a sector."""
        engine = get_engine()
        session_factory = get_session_factory(engine)

        async with session_factory() as session:
            # Get active stocks in this sector
            sector_codes_q = await session.execute(
                select(StkSectorMap.code).where(
                    StkSectorMap.sector_name == sector_name,
                    StkSectorMap.is_active.is_(True),
                )
            )
            sector_codes = [row[0] for row in sector_codes_q.all()]

            if not sector_codes:
                logger.debug("No stocks found for sector %s", sector_name)
                await engine.dispose()
                return []

            # Get stock data for these codes on trade_date
            stocks_q = await session.execute(
                select(StkDaily).where(
                    StkDaily.trade_date == trade_date,
                    StkDaily.code.in_(sector_codes),
                    StkDaily.is_st.is_(False),  # Exclude ST
                    StkDaily.is_limit_up.is_(False),  # Exclude limit-up (can't buy next day)
                )
            )
            stocks = stocks_q.scalars().all()

        await engine.dispose()

        if not stocks:
            return []

        # Extract raw values for ranking
        stock_data = []
        for s in stocks:
            amount = s.amount if s.amount and s.amount > 0 else 1.0
            fund_ratio = (s.net_inflow / amount) if s.net_inflow is not None else None

            stock_data.append(
                {
                    "code": s.code,
                    "name": s.name,
                    "change_pct": s.change_pct,
                    "fund_ratio": fund_ratio,
                    "volume_ratio": s.volume_ratio,
                    "turnover_rate": s.turnover_rate,
                    "market_cap": s.market_cap,
                    "net_inflow": s.net_inflow,
                    "close": s.close,
                    "volume": s.volume,
                    "amount": s.amount,
                }
            )

        n = len(stock_data)
        if n == 0:
            return []

        # Compute percentile ranks for each factor
        fund_ratios = [d["fund_ratio"] for d in stock_data]
        change_pcts = [d["change_pct"] for d in stock_data]
        volume_ratios = [d["volume_ratio"] for d in stock_data]
        turnover_rates = [d["turnover_rate"] for d in stock_data]
        market_caps = [d["market_cap"] for d in stock_data]

        fund_ranks = _percentile_rank(fund_ratios, default=0.5)
        momentum_ranks = _percentile_rank(change_pcts)
        vr_ranks = _percentile_rank(volume_ratios)
        turnover_suitability = _turnover_suitability(turnover_rates)
        quality_ranks = _percentile_rank(
            [math.log(mc) if mc and mc > 0 else None for mc in market_caps]
        )

        scored: list[ScoredStock] = []
        for i, d in enumerate(stock_data):
            factor_fund = fund_ranks[i]
            factor_momentum = momentum_ranks[i]
            factor_technical = vr_ranks[i] * 0.6 + turnover_suitability[i] * 0.4
            factor_quality = quality_ranks[i]

            total = (
                self.weights.weight_fund * factor_fund
                + self.weights.weight_momentum * factor_momentum
                + self.weights.weight_technical * factor_technical
                + self.weights.weight_quality * factor_quality
            )

            scored.append(
                ScoredStock(
                    code=d["code"],
                    name=d["name"],
                    sector=sector_name,
                    pool_type=pool_type,
                    total_score=round(total, 4),
                    factor_scores={
                        "fund": round(factor_fund, 4),
                        "momentum": round(factor_momentum, 4),
                        "technical": round(factor_technical, 4),
                        "quality": round(factor_quality, 4),
                    },
                    raw_data=d,
                )
            )

        return scored


def _percentile_rank(values: list[float | None], default: float = 0.5) -> list[float]:
    """Compute percentile ranks (0-1) for a list of values.

    None values get the default rank.
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [default if values[0] is None else 0.5]

    # Create (index, value) pairs, filter out None
    indexed = [(i, v) for i, v in enumerate(values) if v is not None]

    if not indexed:
        return [default] * n

    # Sort by value
    indexed.sort(key=lambda x: x[1])

    # Assign ranks (0 to n-1), then normalize to 0-1
    result = [default] * n
    valid_n = len(indexed)
    for rank, (orig_idx, _) in enumerate(indexed):
        result[orig_idx] = rank / max(valid_n - 1, 1)

    return result


def _turnover_suitability(turnover_rates: list[float | None]) -> list[float]:
    """Score turnover rate suitability (moderate is best).

    Ideal turnover: 3-8%. Too low = illiquid, too high = speculative.
    Returns 0-1 score where 1 = ideal range.
    """
    result = []
    for tr in turnover_rates:
        if tr is None:
            result.append(0.5)
        elif 3.0 <= tr <= 8.0:
            result.append(1.0)
        elif tr < 3.0:
            result.append(max(0.0, tr / 3.0))
        else:  # > 8.0
            result.append(max(0.0, 1.0 - (tr - 8.0) / 20.0))
    return result
