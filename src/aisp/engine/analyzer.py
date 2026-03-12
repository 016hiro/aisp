"""LLM analysis orchestration: sentiment classification + stock analysis."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime

from sqlalchemy import select

from aisp.config import get_settings
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import (
    Direction,
    GlobalDaily,
    SectorDaily,
    Sentiment,
    StkComments,
)
from aisp.engine.llm_client import LLMClient
from aisp.engine.prompts import format_sentiment_classification, format_stock_analysis
from aisp.engine.signals import generate_signals
from aisp.screening.sector_pools import SectorPoolManager
from aisp.screening.stock_scorer import GlobalContext, StockScorer

logger = logging.getLogger(__name__)

SENTIMENT_BATCH_SIZE = 10


async def classify_sentiment(trade_date: date) -> int:
    """Classify pending comments' sentiment using LLM.

    Returns number of comments classified.
    """
    engine = get_engine()
    session_factory = get_session_factory(engine)
    client = LLMClient()
    classified = 0

    try:
        async with session_factory() as session:
            # Get pending comments
            result = await session.execute(
                select(StkComments)
                .where(StkComments.sentiment == Sentiment.PENDING)
                .order_by(StkComments.fetched_at.desc())
                .limit(100)
            )
            pending = result.scalars().all()

            if not pending:
                logger.info("No pending comments to classify")
                return 0

            # Process in batches
            for i in range(0, len(pending), SENTIMENT_BATCH_SIZE):
                batch = pending[i : i + SENTIMENT_BATCH_SIZE]

                comments_text = "\n".join(
                    f"[{j + 1}] 股票:{c.code} | 标题:{c.title or '无'} | 内容:{c.content[:200]}"
                    for j, c in enumerate(batch)
                )

                try:
                    prompt = format_sentiment_classification(comments_text=comments_text)
                    messages = [{"role": "user", "content": prompt}]
                    results = await client.analyze_json(
                        messages,
                        model=client.sentiment_model,
                    )

                    if isinstance(results, list):
                        for item in results:
                            idx = item.get("id", 0) - 1
                            if 0 <= idx < len(batch):
                                sent_str = item.get("sentiment", "neutral")
                                try:
                                    sentiment = Sentiment(sent_str)
                                except ValueError:
                                    sentiment = Sentiment.NEUTRAL

                                batch[idx].sentiment = sentiment
                                batch[idx].sentiment_score = item.get("score")
                                batch[idx].sentiment_reason = item.get("reason")
                                batch[idx].analyzed_at = datetime.now()
                                classified += 1

                except Exception:
                    logger.exception("Failed to classify sentiment batch")

            await session.commit()
    finally:
        await client.close()
        await engine.dispose()

    logger.info("Classified %d comments", classified)
    return classified


async def _get_sector_context(session, sector_name: str, trade_date: date) -> str:
    """Build sector context string for LLM prompt."""
    result = await session.execute(
        select(SectorDaily).where(
            SectorDaily.sector_name == sector_name,
            SectorDaily.trade_date == trade_date,
        )
    )
    sector = result.scalar_one_or_none()
    if not sector:
        return "板块数据不可用"

    return (
        f"板块: {sector_name}\n"
        f"涨跌幅: {sector.change_pct}%\n"
        f"上涨/下跌家数: {sector.up_count}/{sector.down_count}\n"
        f"主力净流入: {sector.net_inflow or '无数据'}\n"
        f"MA5/10/20/60: {sector.ma5}/{sector.ma10}/{sector.ma20}/{sector.ma60}"
    )


async def _get_global_context(session, trade_date: date) -> str:
    """Build global market context string."""
    result = await session.execute(
        select(GlobalDaily)
        .where(GlobalDaily.trade_date <= trade_date)
        .order_by(GlobalDaily.trade_date.desc())
        .limit(20)
    )
    globals_ = result.scalars().all()
    if not globals_:
        return "全球市场数据不可用"

    lines = []
    for g in globals_:
        lines.append(f"{g.name}({g.symbol}): {g.close} ({g.change_pct:+.2f}%)")
    return "\n".join(lines[:10])


async def _get_sentiment_context(session, code: str, trade_date: date) -> str:
    """Build sentiment context for a stock."""
    from datetime import timedelta

    since = trade_date - timedelta(days=7)
    result = await session.execute(
        select(StkComments)
        .where(
            StkComments.code == code,
            StkComments.published_at >= datetime.combine(since, datetime.min.time()),
            StkComments.sentiment != Sentiment.PENDING,
            StkComments.sentiment != Sentiment.NOISE,
        )
        .order_by(StkComments.published_at.desc())
        .limit(5)
    )
    comments = result.scalars().all()
    if not comments:
        return "近期无相关舆情"

    lines = []
    for c in comments:
        lines.append(f"[{c.sentiment.value}] {c.title or c.content[:50]}")
    return "\n".join(lines)


async def run_analysis(
    trade_date: date, *, btc_metrics=None, codes: list[str] | None = None
) -> int:
    """Run full analysis pipeline: sentiment -> screening -> LLM analysis -> signals.

    Args:
        btc_metrics: Optional BtcRiskMetrics for global context enrichment.
        codes: Optional list of stock codes to analyze. If provided, only these
               stocks will be sent to LLM (screening still runs fully for ranking).

    Returns number of signals generated.
    """
    get_settings()

    # 1. Classify pending sentiments
    await classify_sentiment(trade_date)

    # 2. Build GlobalContext for macro factor
    engine = get_engine()
    session_factory = get_session_factory(engine)

    async with session_factory() as session:
        global_daily_q = await session.execute(
            select(GlobalDaily).where(GlobalDaily.trade_date == trade_date)
        )
        global_rows = global_daily_q.scalars().all()

    asset_changes = {g.symbol: g.change_pct for g in global_rows}
    global_ctx = GlobalContext(
        btc_risk_score=btc_metrics.risk_score if btc_metrics else None,
        asset_changes=asset_changes,
    )

    # 3. Score stocks — targeted mode vs batch mode
    scorer = StockScorer()
    if codes:
        # Targeted: code → find sector → score within sector, no top-N filtering
        scored_stocks = await scorer.score_by_codes(codes, trade_date, global_context=global_ctx)
        logger.info("Targeted analysis: %d stocks for codes %s", len(scored_stocks), codes)
    else:
        # Batch: update pools → score all pools → top N per sector
        pool_mgr = SectorPoolManager()
        pools = await pool_mgr.update_pools(trade_date)
        scored_stocks = await scorer.score_all_pools(pools, trade_date, global_context=global_ctx)

    if not scored_stocks:
        logger.warning("No stocks scored, skipping LLM analysis")
        await engine.dispose()
        return 0

    # Deduplicate: keep only the highest-scoring entry per code
    # (score_by_codes may return multiple entries per stock across sectors)
    best_by_code: dict[str, object] = {}
    for s in scored_stocks:
        if s.code not in best_by_code or s.total_score > best_by_code[s.code].total_score:
            best_by_code[s.code] = s
    scored_stocks = list(best_by_code.values())
    logger.info("Analyzing %d unique stocks", len(scored_stocks))

    # 3. Agent-based deep analysis — concurrent with semaphore
    from aisp.engine.agent import analyze_stock

    signals: list[dict] = []
    semaphore = asyncio.Semaphore(5)

    # Pre-fetch all context data (DB queries must be sequential)
    async with session_factory() as session:
        sector_context_cache: dict[str, str] = {}
        global_context_str = await _get_global_context(session, trade_date)
        if btc_metrics is not None:
            global_context_str += btc_metrics.to_prompt_text()

        for stock in scored_stocks:
            if stock.sector not in sector_context_cache:
                sector_context_cache[stock.sector] = await _get_sector_context(
                    session, stock.sector, trade_date
                )

        sentiment_cache: dict[str, str] = {}
        for stock in scored_stocks:
            if stock.code not in sentiment_cache:
                sentiment_cache[stock.code] = await _get_sentiment_context(
                    session, stock.code, trade_date
                )

    async def _analyze_one(stock) -> dict | None:
        """Analyze a single stock with Agent, guarded by semaphore."""
        async with semaphore:
            try:
                veto_warning = ""
                if stock.veto:
                    veto_warning = f"\n⚠️ 否决警告: {stock.veto}"

                extra_instructions = ""
                wyckoff_data = stock.raw_data.get("_wyckoff")
                if wyckoff_data and wyckoff_data.get("phase") != "unknown":
                    extra_instructions = (
                        f"\n## 威科夫阶段分析\n{wyckoff_data['detail']}\n"
                        f"- 请结合威科夫{wyckoff_data['phase']}阶段判断，"
                        f"校准乘数为{wyckoff_data['multiplier']}，"
                        f"已自动调整综合得分"
                    )

                breakout_data = stock.raw_data.get("_breakout")
                if breakout_data:
                    breakout_lines = [b["description"] for b in breakout_data]
                    extra_instructions += (
                        "\n## 突破信号\n"
                        + "\n".join(f"- {t}" for t in breakout_lines)
                        + "\n- 请重点关注上述突破信号对短期走势的影响"
                    )

                prompt = format_stock_analysis(
                    code=stock.code,
                    name=stock.name,
                    sector=stock.sector,
                    pool_type=stock.pool_type.value,
                    close=stock.raw_data.get("close", "N/A"),
                    change_pct=stock.raw_data.get("change_pct", "N/A"),
                    volume=stock.raw_data.get("volume", "N/A"),
                    amount=stock.raw_data.get("amount", "N/A"),
                    turnover_rate=stock.raw_data.get("turnover_rate", "N/A"),
                    volume_ratio=stock.raw_data.get("volume_ratio", "N/A"),
                    net_inflow=stock.raw_data.get("net_inflow", "N/A"),
                    total_score=stock.total_score,
                    f_fund=stock.factor_scores.get("fund", 0.5),
                    f_momentum=stock.factor_scores.get("momentum", 0.5),
                    f_technical=stock.factor_scores.get("technical", 0.5),
                    f_quality=stock.factor_scores.get("quality", 0.5),
                    f_indicators=stock.factor_scores.get("indicators", 0.5),
                    f_macro=stock.factor_scores.get("macro", 0.5),
                    f_sentiment=stock.factor_scores.get("sentiment", 0.5),
                    f_sector=stock.factor_scores.get("sector", 0.5),
                    w_fund=stock.dynamic_weights.get("fund", 0),
                    w_momentum=stock.dynamic_weights.get("momentum", 0),
                    w_technical=stock.dynamic_weights.get("technical", 0),
                    w_quality=stock.dynamic_weights.get("quality", 0),
                    w_indicators=stock.dynamic_weights.get("indicators", 0),
                    w_macro=stock.dynamic_weights.get("macro", 0),
                    w_sentiment=stock.dynamic_weights.get("sentiment", 0),
                    w_sector=stock.dynamic_weights.get("sector", 0),
                    veto_warning=veto_warning,
                    extra_instructions=extra_instructions,
                    sector_context=sector_context_cache[stock.sector],
                    global_context=global_context_str,
                    sentiment_context=sentiment_cache.get(stock.code, "近期无相关舆情"),
                )

                result = await analyze_stock(prompt)

                if result:
                    try:
                        direction = Direction(result.direction)
                    except ValueError:
                        direction = Direction.HOLD

                    return {
                        "trade_date": trade_date,
                        "code": stock.code,
                        "name": stock.name,
                        "sector": stock.sector,
                        "pool_type": stock.pool_type,
                        "direction": direction,
                        "score": stock.total_score,
                        "factor_scores": {
                            **stock.factor_scores,
                            "_weights": stock.dynamic_weights,
                            "_veto": stock.veto,
                            "_key_risks": result.key_risks,
                            "_catalysts": result.catalysts,
                            "_breakout": stock.raw_data.get("_breakout"),
                        },
                        "confidence": result.confidence,
                        "reasoning": result.reasoning,
                    }
            except Exception:
                logger.exception("Failed to analyze stock %s", stock.code)
            return None

    results = await asyncio.gather(*[_analyze_one(s) for s in scored_stocks])
    signals = [r for r in results if r is not None]

    # 4. Generate and store signals
    count = await generate_signals(signals, trade_date)
    await engine.dispose()

    logger.info("Generated %d trading signals", count)
    return count
