"""LLM analysis orchestration: sentiment classification + stock analysis."""

from __future__ import annotations

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
from aisp.engine.signals import generate_signals
from aisp.screening.sector_pools import SectorPoolManager
from aisp.screening.stock_scorer import StockScorer

logger = logging.getLogger(__name__)

SENTIMENT_BATCH_SIZE = 10

SENTIMENT_PROMPT = """你是一个金融舆情分析专家。请对以下股票相关评论/公告进行情绪分类。

分类标签：
- bullish: 看多/利好
- bearish: 看空/利空
- neutral: 中性
- euphoric: 极度乐观（如涨停狂欢）
- panic: 恐慌（如大幅下跌恐慌）
- noise: 无关噪音

请以 JSON 数组返回，每个元素包含 id、sentiment、score（置信度0-1）、reason（简短理由）。

评论列表：
{comments_text}

返回格式：
[{{"id": 1, "sentiment": "bullish", "score": 0.85, "reason": "公告利好"}}]"""

STOCK_ANALYSIS_PROMPT = """你是一个专业的 A 股分析师。请分析以下股票，给出交易建议。

## 股票信息
- 代码: {code}
- 名称: {name}
- 所属板块: {sector}
- 池类型: {pool_type}

## 日线数据
- 最新价: {close}
- 涨跌幅: {change_pct}%
- 成交量: {volume}
- 成交额: {amount}
- 换手率: {turnover_rate}%
- 量比: {volume_ratio}
- 主力净流入: {net_inflow}

## 多因子评分
- 综合得分: {total_score}
- 资金面: {factor_fund}
- 动量: {factor_momentum}
- 技术面: {factor_technical}
- 质量: {factor_quality}

## 板块动态
{sector_context}

## 全球市场联动
{global_context}

## 近期舆情
{sentiment_context}

请以 JSON 返回分析结果：
{{
    "direction": "buy/sell/hold/watch",
    "confidence": 0.0-1.0,
    "reasoning": "分析理由（150字以内）",
    "key_risks": ["风险1", "风险2"],
    "catalysts": ["催化剂1"]
}}"""


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
                    messages = [
                        {
                            "role": "user",
                            "content": SENTIMENT_PROMPT.format(
                                comments_text=comments_text
                            ),
                        }
                    ]
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


async def run_analysis(trade_date: date) -> int:
    """Run full analysis pipeline: sentiment → screening → LLM analysis → signals.

    Returns number of signals generated.
    """
    get_settings()

    # 1. Classify pending sentiments
    await classify_sentiment(trade_date)

    # 2. Run screening
    pool_mgr = SectorPoolManager()
    pools = await pool_mgr.update_pools(trade_date)
    scorer = StockScorer()
    scored_stocks = await scorer.score_all_pools(pools, trade_date)

    if not scored_stocks:
        logger.warning("No stocks scored, skipping LLM analysis")
        return 0

    # 3. LLM deep analysis for each scored stock
    client = LLMClient()
    signals: list[dict] = []

    engine = get_engine()
    session_factory = get_session_factory(engine)

    try:
        async with session_factory() as session:
            sector_context_cache: dict[str, str] = {}
            global_context = await _get_global_context(session, trade_date)

            for stock in scored_stocks:
                try:
                    # Cache sector context
                    if stock.sector not in sector_context_cache:
                        sector_context_cache[stock.sector] = await _get_sector_context(
                            session, stock.sector, trade_date
                        )

                    sentiment_context = await _get_sentiment_context(
                        session, stock.code, trade_date
                    )

                    prompt = STOCK_ANALYSIS_PROMPT.format(
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
                        factor_fund=stock.factor_scores.get("fund", "N/A"),
                        factor_momentum=stock.factor_scores.get("momentum", "N/A"),
                        factor_technical=stock.factor_scores.get("technical", "N/A"),
                        factor_quality=stock.factor_scores.get("quality", "N/A"),
                        sector_context=sector_context_cache[stock.sector],
                        global_context=global_context,
                        sentiment_context=sentiment_context,
                    )

                    result = await client.analyze_json(
                        [{"role": "user", "content": prompt}],
                        model=client.analysis_model,
                    )

                    if isinstance(result, dict) and result:
                        dir_str = result.get("direction", "watch")
                        try:
                            direction = Direction(dir_str)
                        except ValueError:
                            direction = Direction.WATCH

                        signals.append(
                            {
                                "trade_date": trade_date,
                                "code": stock.code,
                                "name": stock.name,
                                "sector": stock.sector,
                                "pool_type": stock.pool_type,
                                "direction": direction,
                                "score": stock.total_score,
                                "factor_scores": stock.factor_scores,
                                "confidence": float(result.get("confidence", 0.5)),
                                "reasoning": result.get("reasoning", "LLM 分析完成"),
                            }
                        )
                except Exception:
                    logger.exception("Failed to analyze stock %s", stock.code)
    finally:
        await client.close()

    # 4. Generate and store signals
    count = await generate_signals(signals, trade_date)
    await engine.dispose()

    logger.info("Generated %d trading signals", count)
    return count
