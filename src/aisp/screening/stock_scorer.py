"""Multi-factor stock scoring within sector pools — 8-factor elastic-weight model."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select

from aisp.config import get_settings
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import (
    PoolType,
    SectorDaily,
    SectorPoolState,
    Sentiment,
    StkComments,
    StkDaily,
    StkLhb,
    StkMargin,
    StkProfile,
    StkSectorMap,
)
from aisp.screening.factor_engine import FactorResult, VetoRule, score_stock
from aisp.screening.indicators import compute_technical_score
from aisp.screening.sector_pools import PoolResult

logger = logging.getLogger(__name__)


@dataclass
class GlobalContext:
    """Global market context passed into the scorer."""

    btc_risk_score: float | None = None
    asset_changes: dict[str, float] = field(default_factory=dict)


@dataclass
class ScoredStock:
    """A scored stock with factor breakdown."""

    code: str
    name: str
    sector: str
    pool_type: PoolType
    total_score: float
    factor_scores: dict[str, float] = field(default_factory=dict)
    dynamic_weights: dict[str, float] = field(default_factory=dict)
    veto: str | None = None
    raw_data: dict = field(default_factory=dict)
    wyckoff_phase: str | None = None
    wyckoff_multiplier: float = 1.0


class StockScorer:
    """8-factor elastic-weight stock scorer."""

    def __init__(self):
        self.settings = get_settings()
        self.weights = self.settings.scoring

    def _get_base_weights(self) -> dict[str, float]:
        return {
            "fund": self.weights.weight_fund,
            "momentum": self.weights.weight_momentum,
            "technical": self.weights.weight_technical,
            "quality": self.weights.weight_quality,
            "indicators": self.weights.weight_indicators,
            "macro": self.weights.weight_macro,
            "sentiment": self.weights.weight_sentiment,
            "sector": self.weights.weight_sector,
        }

    def _get_veto_rules(self) -> list[VetoRule]:
        vc = self.settings.veto
        return [
            VetoRule("macro", vc.macro_floor, "below", "宏观环境极端悲观，禁止买入"),
            VetoRule("sentiment", vc.sentiment_floor, "below", "市场情绪极度恐慌，禁止买入"),
            VetoRule("sentiment", vc.sentiment_ceiling, "above", "市场情绪过度狂热，警告追高风险"),
        ]

    async def score_all_pools(
        self,
        pool_results: list[PoolResult],
        trade_date: date,
        *,
        global_context: GlobalContext | None = None,
    ) -> list[ScoredStock]:
        """Batch mode: score stocks across all active sector pools, take top N per sector."""
        all_scored: list[ScoredStock] = []
        top_n = self.settings.pool.top_stocks_per_sector

        for pool_result in pool_results:
            for sector_name in pool_result.active_sectors:
                scored = await self._score_sector(
                    sector_name, pool_result.pool_type, trade_date, global_context
                )
                scored.sort(key=lambda s: s.total_score, reverse=True)
                all_scored.extend(scored[:top_n])

        logger.info("Scored %d stocks across all pools", len(all_scored))
        return all_scored

    async def score_by_codes(
        self,
        codes: list[str],
        trade_date: date,
        *,
        global_context: GlobalContext | None = None,
    ) -> list[ScoredStock]:
        """Targeted mode: score specific stocks by code.

        Flow: code → find sector(s) → get sector-level context → score within sector → return.
        No top-N filtering — all requested stocks are returned if they have data.
        """
        engine = get_engine()
        session_factory = get_session_factory(engine)

        # Step 1: find each code's sector(s) and pool_type
        code_sectors: dict[str, list[tuple[str, PoolType]]] = {}
        async with session_factory() as session:
            for code in codes:
                sector_q = await session.execute(
                    select(StkSectorMap.sector_name).where(
                        StkSectorMap.code == code,
                        StkSectorMap.is_active.is_(True),
                    )
                )
                sector_names = [row[0] for row in sector_q.all()]

                # Determine pool type for each sector
                pool_state_q = await session.execute(
                    select(SectorPoolState.sector_name, SectorPoolState.pool_type).where(
                        SectorPoolState.sector_name.in_(sector_names),
                        SectorPoolState.is_active.is_(True),
                    )
                )
                pool_map = {row[0]: PoolType(row[1]) for row in pool_state_q.all()}

                for sn in sector_names:
                    pt = pool_map.get(sn, PoolType.CORE)  # default to core if not in any pool
                    code_sectors.setdefault(code, []).append((sn, pt))

        await engine.dispose()

        if not code_sectors:
            logger.warning("No sector mapping found for codes: %s", codes)
            return []

        # Step 2: for each unique sector, run _score_sector and pick target codes
        scored_sectors: dict[str, tuple[PoolType, list[ScoredStock]]] = {}
        all_results: list[ScoredStock] = []
        target_set = set(codes)

        for _code, sector_list in code_sectors.items():
            for sector_name, pool_type in sector_list:
                if sector_name not in scored_sectors:
                    scored = await self._score_sector(
                        sector_name, pool_type, trade_date, global_context
                    )
                    scored_sectors[sector_name] = (pool_type, scored)

                _, sector_scored = scored_sectors[sector_name]
                for s in sector_scored:
                    if s.code in target_set and s not in all_results:
                        all_results.append(s)

        logger.info("Scored %d target stocks by code", len(all_results))
        return all_results

    async def _score_sector(
        self,
        sector_name: str,
        pool_type: PoolType,
        trade_date: date,
        global_context: GlobalContext | None,
    ) -> list[ScoredStock]:
        """Score all stocks in a sector using 8 factors."""
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
                    StkDaily.is_st.is_(False),
                    StkDaily.is_limit_up.is_(False),
                )
            )
            stocks = stocks_q.scalars().all()

            if not stocks:
                await engine.dispose()
                return []

            stock_codes = [s.code for s in stocks]

            # ── Historical OHLCV for technical indicators + Wyckoff ──
            lookback = self.settings.scoring.indicator_lookback_days
            cutoff = trade_date - timedelta(days=int(lookback * 1.5))
            hist_q = await session.execute(
                select(
                    StkDaily.code, StkDaily.trade_date,
                    StkDaily.open, StkDaily.high, StkDaily.low,
                    StkDaily.close, StkDaily.volume,
                )
                .where(
                    StkDaily.code.in_(stock_codes),
                    StkDaily.trade_date >= cutoff,
                    StkDaily.trade_date <= trade_date,
                )
                .order_by(StkDaily.trade_date)
            )
            closes_by_code: dict[str, list[float]] = {}
            from aisp.screening.wyckoff import OHLCV
            ohlcv_by_code: dict[str, list[OHLCV]] = {}
            for code, _, open_, high, low, close, volume in hist_q.all():
                closes_by_code.setdefault(code, []).append(float(close))
                ohlcv_by_code.setdefault(code, []).append(
                    OHLCV(float(open_), float(high), float(low), float(close), float(volume))
                )

            # ── Sentiment data (7 days) ──
            from datetime import datetime

            since = trade_date - timedelta(days=7)
            sent_q = await session.execute(
                select(StkComments.code, StkComments.sentiment)
                .where(
                    StkComments.code.in_(stock_codes),
                    StkComments.published_at >= datetime.combine(since, datetime.min.time()),
                    StkComments.sentiment.notin_([Sentiment.PENDING, Sentiment.NOISE]),
                )
            )
            sent_by_code: dict[str, list[Sentiment]] = {}
            for code, sentiment in sent_q.all():
                sent_by_code.setdefault(code, []).append(sentiment)

            # ── Sector momentum ──
            sector_daily_q = await session.execute(
                select(SectorDaily).where(
                    SectorDaily.sector_name == sector_name,
                    SectorDaily.trade_date == trade_date,
                )
            )
            sector_daily = sector_daily_q.scalar_one_or_none()

            # ── Stock profiles ──
            profile_q = await session.execute(
                select(StkProfile).where(StkProfile.code.in_(stock_codes))
            )
            profile_map = {p.code: p for p in profile_q.scalars().all()}

            # ── LHB (today) ──
            lhb_q = await session.execute(
                select(StkLhb).where(
                    StkLhb.trade_date == trade_date,
                    StkLhb.code.in_(stock_codes),
                )
            )
            lhb_map = {row.code: row for row in lhb_q.scalars().all()}

            # ── Margin (today) ──
            margin_q = await session.execute(
                select(StkMargin).where(
                    StkMargin.trade_date == trade_date,
                    StkMargin.code.in_(stock_codes),
                )
            )
            margin_map = {m.code: m for m in margin_q.scalars().all()}

            # ── Turnover history for position info ──
            turnover_q = await session.execute(
                select(StkDaily.code, StkDaily.trade_date, StkDaily.turnover_rate)
                .where(
                    StkDaily.code.in_(stock_codes),
                    StkDaily.trade_date >= cutoff,
                    StkDaily.trade_date <= trade_date,
                )
                .order_by(StkDaily.trade_date)
            )
            turnover_by_code: dict[str, list[float]] = {}
            for code, _, tr in turnover_q.all():
                if tr is not None:
                    turnover_by_code.setdefault(code, []).append(float(tr))

        await engine.dispose()

        # Build raw data for ranking
        stock_data = []
        for s in stocks:
            amount = s.amount if s.amount and s.amount > 0 else 1.0
            fund_ratio = (s.net_inflow / amount) if s.net_inflow is not None else None

            stock_data.append({
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
                # Fund flow breakdown (for prompt enrichment, not scoring)
                "main_net": s.main_net,
                "main_pct": s.main_pct,
                "super_large_net": s.super_large_net,
                "super_large_pct": s.super_large_pct,
                "large_net": s.large_net,
                "large_pct": s.large_pct,
                "medium_net": s.medium_net,
                "medium_pct": s.medium_pct,
                "small_net": s.small_net,
                "small_pct": s.small_pct,
                "pe_ttm": s.pe_ttm,
                "pb_mrq": s.pb_mrq,
            })

        n = len(stock_data)
        if n == 0:
            return []

        # ── Percentile ranks for original 4 factors ──
        fund_ratios = [d["fund_ratio"] for d in stock_data]
        change_pcts = [d["change_pct"] for d in stock_data]
        volume_ratios = [d["volume_ratio"] for d in stock_data]
        turnover_rates = [d["turnover_rate"] for d in stock_data]
        market_caps = [d["market_cap"] for d in stock_data]

        fund_ranks = _percentile_rank(fund_ratios, default=0.5)
        momentum_ranks = _percentile_rank(change_pcts)
        vr_ranks = _percentile_rank(volume_ratios)
        turnover_suit = _turnover_suitability(turnover_rates)
        quality_ranks = _percentile_rank(
            [math.log(mc) if mc and mc > 0 else None for mc in market_caps]
        )

        # ── Shared scores: sector momentum + macro ──
        sector_score = _compute_sector_momentum(sector_daily)
        macro_score = _compute_macro_score(global_context, sector_name, self.settings)

        scored: list[ScoredStock] = []
        for i, d in enumerate(stock_data):
            factor_fund = fund_ranks[i]
            factor_momentum = momentum_ranks[i]
            factor_technical = vr_ranks[i] * 0.6 + turnover_suit[i] * 0.4
            factor_quality = quality_ranks[i]

            # Technical indicators factor
            closes = closes_by_code.get(d["code"], [])
            ma_values: dict[int, float | None] = {}
            if sector_daily:
                ma_values = {5: sector_daily.ma5, 10: sector_daily.ma10, 20: sector_daily.ma20, 60: sector_daily.ma60}
            factor_indicators = compute_technical_score(closes, ma_values)

            # Raw indicator values for LLM prompt enrichment
            from aisp.screening.indicators import compute_macd, compute_rsi

            raw_rsi = compute_rsi(closes)
            raw_macd = compute_macd(closes)
            raw_indicators = {
                "rsi6": round(raw_rsi, 1) if raw_rsi is not None else None,
                "macd": round(raw_macd["macd"], 4) if raw_macd else None,
                "macd_signal": round(raw_macd["signal"], 4) if raw_macd else None,
                "macd_hist": round(raw_macd["histogram"], 4) if raw_macd else None,
            }

            # Sentiment factor
            factor_sentiment = _compute_sentiment_score(sent_by_code.get(d["code"], []))

            scores = {
                "fund": round(factor_fund, 4),
                "momentum": round(factor_momentum, 4),
                "technical": round(factor_technical, 4),
                "quality": round(factor_quality, 4),
                "indicators": round(factor_indicators, 4),
                "macro": round(macro_score, 4),
                "sentiment": round(factor_sentiment, 4),
                "sector": round(sector_score, 4),
            }

            result: FactorResult = score_stock(
                scores=scores,
                base_weights=self._get_base_weights(),
                elasticity=self.weights.elasticity,
                veto_rules=self._get_veto_rules(),
            )

            # ── Wyckoff calibration ──
            from aisp.screening.wyckoff import detect_phase as wyckoff_detect

            wyckoff_result = None
            wyckoff_cfg = self.settings.wyckoff
            if wyckoff_cfg.enabled and result.total_score > 0:
                bars = ohlcv_by_code.get(d["code"], [])
                if len(bars) >= wyckoff_cfg.min_bars:
                    wyckoff_result = wyckoff_detect(bars, wyckoff_cfg)

            calibrated_score = result.total_score
            veto = result.veto
            if wyckoff_result and wyckoff_result.multiplier != 1.0:
                calibrated_score = round(result.total_score * wyckoff_result.multiplier, 4)
                if wyckoff_result.multiplier == 0.0:
                    veto = f"威科夫{wyckoff_result.phase.value}阶段否决"
                    calibrated_score = 0.0

            # ── Breakout detection ──
            from aisp.screening.breakout import detect_breakouts

            breakout_cfg = self.settings.breakout
            breakout_signals = []
            breakout_strong = []
            if breakout_cfg.enabled and calibrated_score > 0:
                bars = ohlcv_by_code.get(d["code"], [])
                if len(bars) >= 20:
                    breakout_signals = detect_breakouts(
                        bars, breakout_cfg, wyckoff_result=wyckoff_result
                    )

                # Weak signals → multiplier adjustment (pick largest absolute adj)
                breakout_adj = 0.0
                for s in breakout_signals:
                    if s.strength == "weak" and abs(s.multiplier_adj) > abs(breakout_adj):
                        breakout_adj = s.multiplier_adj
                if breakout_adj != 0:
                    calibrated_score = round(calibrated_score * (1 + breakout_adj), 4)

                # Strong signals → store for LLM prompt injection
                breakout_strong = [
                    {
                        "signal_type": s.signal_type,
                        "description": s.description,
                        "strength_score": s.strength_score,
                        "level": s.level,
                        "volume_ratio": s.volume_ratio,
                    }
                    for s in breakout_signals
                    if s.strength == "strong"
                ]

            # ── Trading plan generation ──
            from aisp.screening.trading_plan import (
                compute_trading_plan,
                trading_plan_to_dict,
            )

            trading_plan_dict = None
            tp_config = self.settings.trading_plan
            if tp_config.enabled:
                prelim_dir = (
                    "buy" if calibrated_score >= 0.6
                    else ("sell" if calibrated_score <= 0.35 else "hold")
                )
                tp = compute_trading_plan(
                    code=d["code"],
                    closes=closes,
                    bars=ohlcv_by_code.get(d["code"], []),
                    wyckoff_data=_wyckoff_to_dict(wyckoff_result),
                    breakout_data=breakout_strong or None,
                    is_st=False,
                    is_limit_up=False,
                    is_limit_down=False,
                    direction=prelim_dir,
                    config=tp_config,
                )
                if tp:
                    trading_plan_dict = trading_plan_to_dict(tp)

            # ── Trend detection (for direction override) ──
            trend_info = _compute_trend(closes)

            # Recent OHLCV for LLM K-line display (last 15 bars)
            bars = ohlcv_by_code.get(d["code"], [])
            recent_bars = bars[-15:] if len(bars) >= 15 else bars
            recent_ohlcv = [
                {
                    "o": round(b.open, 2),
                    "h": round(b.high, 2),
                    "l": round(b.low, 2),
                    "c": round(b.close, 2),
                    "v": int(b.volume),
                }
                for b in recent_bars
            ]

            # ── Volume ratio fallback (compute from lookback if DB is NULL) ──
            if d.get("volume_ratio") is None:
                bars_for_vr = ohlcv_by_code.get(d["code"], [])
                if len(bars_for_vr) >= 6:
                    today_vol = bars_for_vr[-1].volume
                    avg_vol = sum(b.volume for b in bars_for_vr[-6:-1]) / 5
                    if avg_vol > 0:
                        d["volume_ratio"] = round(today_vol / avg_vol, 4)

            # ── Position info (price location context) ──
            turnovers = turnover_by_code.get(d["code"], [])
            position_info = _compute_position_info(closes, turnovers)

            scored.append(ScoredStock(
                code=d["code"],
                name=d["name"],
                sector=sector_name,
                pool_type=pool_type,
                total_score=calibrated_score,
                factor_scores=result.scores,
                dynamic_weights={k: round(v, 4) for k, v in result.dynamic_weights.items()},
                veto=veto,
                raw_data={
                    **d,
                    "_wyckoff": _wyckoff_to_dict(wyckoff_result),
                    "_breakout": breakout_strong or None,
                    "_trading_plan": trading_plan_dict,
                    "_trend": trend_info,
                    "_recent_ohlcv": recent_ohlcv,
                    "_raw_indicators": raw_indicators,
                    "_position_info": position_info,
                    "_profile": _profile_to_dict(profile_map.get(d["code"])),
                    "_lhb": _lhb_to_dict(lhb_map.get(d["code"])),
                    "_margin": _margin_to_dict(margin_map.get(d["code"])),
                },
                wyckoff_phase=wyckoff_result.phase.value if wyckoff_result else None,
                wyckoff_multiplier=wyckoff_result.multiplier if wyckoff_result else 1.0,
            ))

        return scored


def _compute_sector_momentum(sector_daily: SectorDaily | None) -> float:
    """Compute sector momentum score from breadth, MA trend, and fund flow."""
    if sector_daily is None:
        return 0.5

    # Breadth: up_count / stock_count
    breadth = 0.5
    if sector_daily.stock_count > 0:
        breadth = sector_daily.up_count / sector_daily.stock_count

    # MA trend: close > MA20 → 1.0
    ma_trend = 0.5
    if sector_daily.ma20 is not None:
        ma_trend = 1.0 if sector_daily.close > sector_daily.ma20 else 0.0

    # Fund flow normalized (simple sign-based for now)
    flow_score = 0.5
    if sector_daily.net_inflow is not None and sector_daily.amount and sector_daily.amount > 0:
        ratio = sector_daily.net_inflow / sector_daily.amount
        flow_score = max(0.0, min(1.0, ratio * 10 + 0.5))

    return breadth * 0.4 + ma_trend * 0.3 + flow_score * 0.3


def _compute_macro_score(
    global_context: GlobalContext | None, sector_name: str, settings
) -> float:
    """Compute macro linkage score from global context."""
    if global_context is None:
        return 0.5

    mapping = settings.asset_linkage.mapping
    linked_assets = []
    for symbol, sectors in mapping.items():
        if sector_name in sectors:
            linked_assets.append(symbol)

    if not linked_assets and global_context.btc_risk_score is None:
        return 0.5

    # Linked asset average change normalized
    asset_score = 0.5
    if linked_assets:
        changes = [global_context.asset_changes.get(sym, 0.0) for sym in linked_assets]
        avg_change = sum(changes) / len(changes) if changes else 0.0
        # Normalize: [-5%, +5%] → [0, 1]
        asset_score = max(0.0, min(1.0, avg_change / 10.0 + 0.5))

    # BTC risk score component
    btc_score = global_context.btc_risk_score if global_context.btc_risk_score is not None else 0.5

    if linked_assets:
        return asset_score * 0.6 + btc_score * 0.4
    return btc_score


def _compute_sentiment_score(sentiments: list) -> float:
    """Compute sentiment score from recent comments."""
    if not sentiments:
        return 0.5

    total = len(sentiments)
    weights = {
        Sentiment.EUPHORIC: 1.5,
        Sentiment.BULLISH: 1.0,
        Sentiment.NEUTRAL: 0.0,
        Sentiment.BEARISH: -1.0,
        Sentiment.PANIC: -1.5,
    }

    raw = sum(weights.get(s, 0.0) for s in sentiments) / total
    # Map roughly [-1.5, 1.5] → [0, 1]
    return max(0.0, min(1.0, raw / 3.0 + 0.5))


def _percentile_rank(values: list[float | None], default: float = 0.5) -> list[float]:
    """Compute percentile ranks (0-1) for a list of values.

    None values get the default rank.
    """
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        return [default if values[0] is None else 0.5]

    indexed = [(i, v) for i, v in enumerate(values) if v is not None]

    if not indexed:
        return [default] * n

    indexed.sort(key=lambda x: x[1])

    result = [default] * n
    valid_n = len(indexed)
    for rank, (orig_idx, _) in enumerate(indexed):
        result[orig_idx] = rank / max(valid_n - 1, 1)

    return result


def _compute_trend(closes: list[float]) -> dict:
    """Detect recent downtrend from closing prices.

    Returns dict with:
      consecutive_down: number of consecutive declining days (from most recent)
      cumulative_pct: total decline over the consecutive period (negative = decline)
      ma5_below_ma20: True if MA5 < MA20 (short-term bearish)
    """
    if len(closes) < 3:
        return {"consecutive_down": 0, "cumulative_pct": 0.0, "ma5_below_ma20": False}

    # Count consecutive declining closes from most recent
    consecutive_down = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            consecutive_down += 1
        else:
            break

    # Cumulative decline over the consecutive period
    cumulative_pct = 0.0
    if consecutive_down > 0 and len(closes) > consecutive_down:
        start_price = closes[-(consecutive_down + 1)]
        end_price = closes[-1]
        cumulative_pct = round((end_price - start_price) / start_price * 100, 2)

    # MA5 vs MA20
    ma5_below_ma20 = False
    if len(closes) >= 20:
        ma5 = sum(closes[-5:]) / 5
        ma20 = sum(closes[-20:]) / 20
        ma5_below_ma20 = ma5 < ma20

    return {
        "consecutive_down": consecutive_down,
        "cumulative_pct": cumulative_pct,
        "ma5_below_ma20": ma5_below_ma20,
    }


def _wyckoff_to_dict(result) -> dict | None:
    """Convert WyckoffResult to a plain dict for raw_data storage."""
    if result is None:
        return None
    return {
        "phase": result.phase.value,
        "confidence": result.confidence,
        "events": [e.value for e in result.detected_events],
        "multiplier": result.multiplier,
        "detail": result.detail,
        "support": result.support,
        "resistance": result.resistance,
    }


def _turnover_suitability(turnover_rates: list[float | None]) -> list[float]:
    """Score turnover rate suitability (moderate is best).

    Ideal turnover: 3-8%. Too low = illiquid, too high = speculative.
    """
    result = []
    for tr in turnover_rates:
        if tr is None:
            result.append(0.5)
        elif 3.0 <= tr <= 8.0:
            result.append(1.0)
        elif tr < 3.0:
            result.append(max(0.0, tr / 3.0))
        else:
            result.append(max(0.0, 1.0 - (tr - 8.0) / 20.0))
    return result


def _compute_position_info(closes: list[float], turnovers: list[float]) -> dict:
    """Compute price position context from closing prices and turnover rates.

    Returns:
        dist_60d_high_pct: distance from 60-day high (negative = below)
        dist_60d_low_pct: distance from 60-day low (positive = above)
        dist_120d_high_pct / dist_120d_low_pct: same for 120-day
        ytd_pct: YTD return (approximation using available data)
        turnover_5d / 10d / 20d: cumulative turnover over recent periods
    """
    if len(closes) < 3:
        return {}

    current = closes[-1]
    info: dict = {}

    # 60-day high/low distance
    recent_60 = closes[-60:] if len(closes) >= 60 else closes
    high_60 = max(recent_60)
    low_60 = min(recent_60)
    if high_60 > 0:
        info["dist_60d_high_pct"] = round((current - high_60) / high_60 * 100, 1)
    if low_60 > 0:
        info["dist_60d_low_pct"] = round((current - low_60) / low_60 * 100, 1)

    # 120-day high/low distance
    if len(closes) >= 120:
        recent_120 = closes[-120:]
        high_120 = max(recent_120)
        low_120 = min(recent_120)
        if high_120 > 0:
            info["dist_120d_high_pct"] = round((current - high_120) / high_120 * 100, 1)
        if low_120 > 0:
            info["dist_120d_low_pct"] = round((current - low_120) / low_120 * 100, 1)

    # YTD approximation (use earliest available close)
    if len(closes) >= 20:
        ytd_base = closes[0]
        if ytd_base > 0:
            info["ytd_pct"] = round((current - ytd_base) / ytd_base * 100, 1)

    # Cumulative turnover
    if turnovers:
        if len(turnovers) >= 5:
            info["turnover_5d"] = round(sum(turnovers[-5:]), 2)
        if len(turnovers) >= 10:
            info["turnover_10d"] = round(sum(turnovers[-10:]), 2)
        if len(turnovers) >= 20:
            info["turnover_20d"] = round(sum(turnovers[-20:]), 2)

    return info


def _profile_to_dict(profile) -> dict | None:
    """Convert StkProfile to a plain dict."""
    if profile is None:
        return None
    return {
        "board_type": profile.board_type,
        "total_shares": profile.total_shares,
        "liq_shares": profile.liq_shares,
        "listing_date": profile.listing_date.isoformat() if profile.listing_date else None,
    }


def _lhb_to_dict(lhb) -> dict | None:
    """Convert StkLhb to a plain dict."""
    if lhb is None:
        return None
    return {
        "reason": lhb.reason,
        "net_buy": lhb.net_buy,
        "buy_amount": lhb.buy_amount,
        "sell_amount": lhb.sell_amount,
    }


def _margin_to_dict(margin) -> dict | None:
    """Convert StkMargin to a plain dict."""
    if margin is None:
        return None
    return {
        "rzye": margin.rzye,
        "rzjme": margin.rzjme,
        "rqyl": margin.rqyl,
        "rzrqye": margin.rzrqye,
    }
