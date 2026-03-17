"""LLM analysis orchestration: sentiment classification + stock analysis."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select

from aisp.config import get_settings
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import (
    Direction,
    GlobalDaily,
    MarketSentiment,
    SectorDaily,
    Sentiment,
    StkComments,
)
from aisp.engine.llm_client import LLMClient
from aisp.engine.prompts import (
    format_factor_table,
    format_fund_flow_detail,
    format_kline_history,
    format_lhb_info,
    format_market_sentiment,
    format_position_info,
    format_sentiment_classification,
    format_stock_analysis,
    format_stock_identity,
    format_trend_summary,
)
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
                        use_local=True,
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


def _merge_trading_plans(quant: dict | None, llm: dict | None) -> dict | None:
    """Merge quantitative and LLM trading plans.

    LLM adjustments (entry_zone, stop_loss, targets, guidance) take priority;
    quant provides price_limits, position_hint, t1_note as base.
    """
    if not quant and not llm:
        return None
    if not quant:
        return llm
    if not llm:
        return quant

    merged = dict(quant)
    # LLM overrides for price levels
    if llm.get("entry_zone"):
        merged["entry_zone"] = llm["entry_zone"]
    if llm.get("stop_loss") is not None:
        merged["stop_loss"] = llm["stop_loss"]
    if llm.get("targets"):
        merged["targets"] = llm["targets"]
    if llm.get("guidance"):
        merged["guidance"] = llm["guidance"]

    # Recalculate risk/reward if we have the needed values
    entry = merged.get("entry_zone")
    sl = merged.get("stop_loss")
    targets = merged.get("targets")
    if entry and sl and targets and len(entry) == 2:
        entry_mid = (entry[0] + entry[1]) / 2
        risk = entry_mid - sl
        reward = targets[0] - entry_mid
        merged["risk_reward"] = round(reward / risk, 1) if risk > 0 else 0.0

    return merged


def _get_filtered_global_context(
    global_rows: list, sector_name: str, settings
) -> tuple[str, str]:
    """Filter global data by sector linkage. Return (context_str, data_quality_tag)."""
    # Reverse mapping: sector → [global_symbols]
    reverse: dict[str, list[str]] = {}
    for symbol, sectors in settings.asset_linkage.mapping.items():
        for s in sectors:
            reverse.setdefault(s, []).append(symbol)

    linked_symbols = reverse.get(sector_name, [])
    if linked_symbols:
        filtered = [g for g in global_rows if g.symbol in linked_symbols]
        quality = f"[{len(filtered)}只关联资产]"
    else:
        # No linkage → only major indices
        idx_symbols = {"^GSPC", "^IXIC", "^DJI"}
        filtered = [g for g in global_rows if g.symbol in idx_symbols]
        quality = "[无直接关联，仅大盘]"

    lines = [f"{g.name}({g.symbol}): {g.close} ({g.change_pct:+.2f}%)" for g in filtered]
    return "\n".join(lines) or "全球数据不可用", quality


_BULLISH_DOWNGRADE = {
    Direction.STRONG_BUY: Direction.BUY,
    Direction.BUY: Direction.WEAK_BUY,
    Direction.WEAK_BUY: Direction.HOLD,
}


def _apply_direction_guardrails(
    direction: Direction,
    trading_plan: dict | None,
    trend: dict | None,
) -> Direction:
    """Override direction when R:R or trend data contradicts.

    1. R:R consistency: bullish signal + R:R < 1.0 → downgrade one level
    2. Trend filter: 3+ consecutive down days with > -5% decline → block buy
    """
    is_bullish = direction in (Direction.STRONG_BUY, Direction.BUY, Direction.WEAK_BUY)
    if not is_bullish:
        return direction

    # Rule 1: R:R < 1.0 → downgrade
    if trading_plan and trading_plan.get("entry_zone"):
        rr = trading_plan.get("risk_reward", 0)
        if rr < 1.0:
            direction = _BULLISH_DOWNGRADE.get(direction, direction)
            logger.info(
                "Direction downgraded due to poor R:R (%.1f): %s",
                rr, direction.value,
            )

    # Rule 2: Persistent downtrend → block buy
    if trend:
        consec = trend.get("consecutive_down", 0)
        cum_pct = trend.get("cumulative_pct", 0)
        ma5_below = trend.get("ma5_below_ma20", False)
        # 3+ consecutive down days AND cumulative decline > 5% AND MA5 < MA20
        if consec >= 3 and cum_pct <= -5.0 and ma5_below:
            direction = Direction.HOLD
            logger.info(
                "Direction blocked to HOLD: %d consecutive down days (%.1f%%), MA5<MA20",
                consec, cum_pct,
            )

    return direction


def _dump_prompt(prompt_dir: Path, code: str, name: str, user_prompt: str) -> None:
    """Write system + user prompt to a file for external agent testing."""
    from aisp.engine.agent import AGENT_TOOLS
    from aisp.engine.prompts import get_template

    system_prompt = get_template("agent_system").format(
        output_schema=get_template("output_schema"),
    )

    tool_desc_lines = []
    for t in AGENT_TOOLS:
        tool_desc_lines.append(f"- {t.name}: {t.description}")
    tools_section = "\n".join(tool_desc_lines)

    content = (
        f"# {name}({code}) — Agent 深度分析 Prompt\n\n"
        f"## System Prompt\n\n{system_prompt}\n\n"
        f"## Available Tools\n\n{tools_section}\n\n"
        f"## User Prompt\n\n{user_prompt}\n"
    )

    prompt_dir.mkdir(parents=True, exist_ok=True)
    path = prompt_dir / f"{code}.md"
    path.write_text(content, encoding="utf-8")
    logger.info("Prompt dumped: %s", path)


async def run_analysis(
    trade_date: date,
    *,
    btc_metrics=None,
    codes: list[str] | None = None,
    prompt_dir: Path | None = None,
) -> int:
    """Run full analysis pipeline: sentiment -> screening -> LLM analysis -> signals.

    Args:
        btc_metrics: Optional BtcRiskMetrics for global context enrichment.
        codes: Optional list of stock codes to analyze. If provided, only these
               stocks will be sent to LLM (screening still runs fully for ranking).
        prompt_dir: If set, dump prompts to this directory instead of calling LLM.

    Returns number of signals generated (0 when dumping prompts).
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
    settings = get_settings()
    async with session_factory() as session:
        sector_context_cache: dict[str, str] = {}
        # Fetch global rows for filtered context
        global_daily_q2 = await session.execute(
            select(GlobalDaily).where(GlobalDaily.trade_date == trade_date)
        )
        global_rows_cache = global_daily_q2.scalars().all()

        # Fetch market sentiment (shared across all stocks)
        market_sent_q = await session.execute(
            select(MarketSentiment).where(MarketSentiment.trade_date == trade_date)
        )
        market_sentiment_row = market_sent_q.scalar_one_or_none()

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
                        f"- 威科夫{wyckoff_data['phase']}阶段，"
                        f"校准乘数{wyckoff_data['multiplier']}，已调整综合得分"
                    )

                breakout_data = stock.raw_data.get("_breakout")
                if breakout_data:
                    breakout_lines = [b["description"] for b in breakout_data]
                    extra_instructions += (
                        "\n## 突破信号\n"
                        + "\n".join(f"- {t}" for t in breakout_lines)
                    )

                trading_plan = stock.raw_data.get("_trading_plan")
                if trading_plan and trading_plan.get("entry_zone"):
                    ez = trading_plan["entry_zone"]
                    short_targets = trading_plan.get("targets", [])
                    mid_targets = trading_plan.get("mid_targets", [])
                    short_rr = trading_plan.get("risk_reward", 0)
                    mid_rr = trading_plan.get("mid_risk_reward", 0)
                    extra_instructions += (
                        "\n## 量化交易计划（请验证并调整）\n"
                        f"- 入场区间: {ez[0]:.2f} - {ez[1]:.2f}\n"
                        f"- 止损位: {trading_plan['stop_loss']:.2f}\n"
                        f"- 短线目标(1-5日): "
                        f"{', '.join(f'{t:.2f}' for t in short_targets)}"
                        f" | R:R {short_rr:.1f}\n"
                        f"- 中线目标(1-3月): "
                        f"{', '.join(f'{t:.2f}' for t in mid_targets)}"
                        f" | R:R {mid_rr:.1f}\n"
                        f"- 涨停/跌停: {trading_plan['price_limits']['down']:.2f}"
                        f" ~ {trading_plan['price_limits']['up']:.2f}\n"
                        f"- {trading_plan['t1_note']}\n"
                    )
                elif trading_plan:
                    extra_instructions += (
                        f"\n## 交易提示\n- {trading_plan.get('rationale', '')}\n"
                    )

                # Build enriched prompt fields
                recent_ohlcv = stock.raw_data.get("_recent_ohlcv", [])
                kline_history = format_kline_history(recent_ohlcv)
                kline_quality = (
                    f"[{len(recent_ohlcv)}根]" if recent_ohlcv else "[数据不可用]"
                )

                trend_summary = format_trend_summary(stock.raw_data.get("_trend", {}))

                factor_table = format_factor_table(
                    stock.factor_scores,
                    stock.dynamic_weights,
                    raw_indicators=stock.raw_data.get("_raw_indicators"),
                )

                filtered_ctx, global_quality = _get_filtered_global_context(
                    global_rows_cache, stock.sector, settings
                )
                # Append BTC metrics if available
                if btc_metrics is not None:
                    filtered_ctx += btc_metrics.to_prompt_text()

                # New enriched fields
                market_sent_str = format_market_sentiment(market_sentiment_row)
                stock_id_str = format_stock_identity(
                    stock.raw_data.get("_profile"),
                    stock.raw_data.get("pe_ttm"),
                    stock.raw_data.get("pb_mrq"),
                    close=stock.raw_data.get("close"),
                )
                fund_flow_str = format_fund_flow_detail(stock.raw_data)
                position_str = format_position_info(stock.raw_data.get("_position_info"))
                lhb_str = format_lhb_info(stock.raw_data.get("_lhb"))

                prompt = format_stock_analysis(
                    code=stock.code,
                    name=stock.name,
                    sector=stock.sector,
                    pool_type=stock.pool_type.value,
                    close=stock.raw_data.get("close", "N/A"),
                    change_pct=stock.raw_data.get("change_pct", "N/A"),
                    turnover_rate=stock.raw_data.get("turnover_rate", "N/A"),
                    volume_ratio=stock.raw_data.get("volume_ratio", "N/A"),
                    total_score=stock.total_score,
                    kline_history=kline_history,
                    kline_data_quality=kline_quality,
                    trend_summary=trend_summary,
                    factor_table=factor_table,
                    veto_warning=veto_warning,
                    extra_instructions=extra_instructions,
                    sector_context=sector_context_cache[stock.sector],
                    filtered_global_context=filtered_ctx,
                    global_data_quality=global_quality,
                    sentiment_context=sentiment_cache.get(stock.code, "近期无相关舆情"),
                    stock_identity=stock_id_str,
                    market_sentiment=market_sent_str,
                    position_info=position_str,
                    fund_flow_detail=fund_flow_str,
                    lhb_info=lhb_str,
                )

                # Dump mode: write prompt to file, skip LLM
                if prompt_dir is not None:
                    _dump_prompt(prompt_dir, stock.code, stock.name, prompt)
                    return None

                result = await analyze_stock(prompt)

                if result:
                    try:
                        direction = Direction(result.direction)
                    except ValueError:
                        direction = Direction.HOLD

                    # Merge trading plans: LLM adjustments over quant base
                    quant_plan = stock.raw_data.get("_trading_plan")
                    llm_plan = result.trading_plan
                    final_plan = _merge_trading_plans(quant_plan, llm_plan)

                    # ── Direction guardrails ──
                    direction = _apply_direction_guardrails(
                        direction, final_plan, stock.raw_data.get("_trend"),
                    )

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
                            "_trading_plan": final_plan,
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
