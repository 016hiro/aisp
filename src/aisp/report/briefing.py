"""Daily Markdown briefing report generator."""

from __future__ import annotations

import logging
from datetime import date, datetime
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from sqlalchemy import select

from aisp.config import get_settings
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import (
    DailySignals,
    Direction,
    GlobalDaily,
    PoolType,
    SectorDaily,
    SectorPoolState,
)
from aisp.review.tracker import PerformanceTracker

logger = logging.getLogger(__name__)
console = Console()


async def generate_briefing(trade_date: date) -> Path:
    """Generate daily briefing report with 5 sections.

    1. Global sentiment
    2. Sector highlights
    3. Top 5 watch stocks
    4. Signal details
    5. Yesterday's performance review

    Returns path to the generated briefing file.
    """
    settings = get_settings()
    engine = get_engine()
    session_factory = get_session_factory(engine)

    sections: list[str] = []
    sections.append(f"# A-ISP 每日简报 — {trade_date}\n")
    sections.append(f"*生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n")

    async with session_factory() as session:
        # ── Section 1: Global Sentiment ──
        sections.append("## 1. 全球市场情绪\n")
        global_data = await session.execute(
            select(GlobalDaily)
            .where(GlobalDaily.trade_date <= trade_date)
            .order_by(GlobalDaily.trade_date.desc())
            .limit(30)
        )
        globals_ = global_data.scalars().all()

        if globals_:
            sections.append("| 品种 | 代码 | 收盘价 | 涨跌幅 |")
            sections.append("|------|------|--------|--------|")

            # Deduplicate by symbol (keep latest)
            seen = set()
            for g in globals_:
                if g.symbol not in seen:
                    seen.add(g.symbol)
                    emoji = "+" if g.change_pct > 0 else ""
                    sections.append(
                        f"| {g.name} | {g.symbol} | {g.close:.2f} | {emoji}{g.change_pct:.2f}% |"
                    )
            sections.append("")

            # Sentiment score based on major indices
            idx_changes = [
                g.change_pct
                for g in globals_
                if g.symbol in ("^GSPC", "^IXIC", "^DJI") and g.trade_date == trade_date
            ]
            if idx_changes:
                avg_change = sum(idx_changes) / len(idx_changes)
                if avg_change > 1:
                    mood = "强势看多"
                elif avg_change > 0:
                    mood = "温和偏多"
                elif avg_change > -1:
                    mood = "温和偏空"
                else:
                    mood = "弱势看空"
                sections.append(f"**全球情绪评分:** {mood} (主要指数均值: {avg_change:+.2f}%)\n")
        else:
            sections.append("*暂无全球市场数据*\n")

        # ── Section 2: Sector Highlights ──
        sections.append("## 2. 板块异动\n")

        # Top gainers
        top_sectors = await session.execute(
            select(SectorDaily)
            .where(SectorDaily.trade_date == trade_date)
            .order_by(SectorDaily.change_pct.desc())
            .limit(5)
        )
        top_list = top_sectors.scalars().all()

        if top_list:
            sections.append("### 领涨板块")
            sections.append("| 板块 | 涨跌幅 | 量(亿) | 上涨/下跌 |")
            sections.append("|------|--------|--------|-----------|")
            for s in top_list:
                vol_yi = s.amount / 1e8 if s.amount else 0
                sections.append(
                    f"| {s.sector_name} | {s.change_pct:+.2f}% | {vol_yi:.1f} | {s.up_count}/{s.down_count} |"
                )
            sections.append("")

        # Top losers
        bottom_sectors = await session.execute(
            select(SectorDaily)
            .where(SectorDaily.trade_date == trade_date)
            .order_by(SectorDaily.change_pct.asc())
            .limit(5)
        )
        bottom_list = bottom_sectors.scalars().all()

        if bottom_list:
            sections.append("### 领跌板块")
            sections.append("| 板块 | 涨跌幅 | 量(亿) | 上涨/下跌 |")
            sections.append("|------|--------|--------|-----------|")
            for s in bottom_list:
                vol_yi = s.amount / 1e8 if s.amount else 0
                sections.append(
                    f"| {s.sector_name} | {s.change_pct:+.2f}% | {vol_yi:.1f} | {s.up_count}/{s.down_count} |"
                )
            sections.append("")

        # Active pools
        pools = await session.execute(
            select(SectorPoolState).where(SectorPoolState.is_active.is_(True))
        )
        pool_list = pools.scalars().all()
        if pool_list:
            sections.append("### 活跃板块池")
            for pt in [PoolType.CORE, PoolType.MOMENTUM, PoolType.OPPORTUNITY]:
                pool_sectors = [p.sector_name for p in pool_list if p.pool_type == pt]
                if pool_sectors:
                    sections.append(f"- **{pt.value}**: {', '.join(pool_sectors)}")
            sections.append("")

        # ── Section 3: Top 5 Watch Stocks ──
        sections.append("## 3. Top 5 观察股\n")

        top_signals = await session.execute(
            select(DailySignals)
            .where(
                DailySignals.trade_date == trade_date,
                DailySignals.direction.in_([Direction.BUY, Direction.WATCH]),
            )
            .order_by(DailySignals.score.desc())
            .limit(5)
        )
        top_stocks = top_signals.scalars().all()

        if top_stocks:
            for rank, sig in enumerate(top_stocks, 1):
                direction_cn = {"buy": "买入", "sell": "卖出", "hold": "持有", "watch": "观察"}.get(
                    sig.direction.value, sig.direction.value
                )
                sections.append(
                    f"### {rank}. {sig.name}({sig.code}) — {direction_cn}"
                )
                sections.append(f"- **板块:** {sig.sector} | **评分:** {sig.score:.4f} | **置信度:** {sig.confidence:.0%}")
                if sig.factor_scores:
                    factors = sig.factor_scores
                    sections.append(
                        f"- **因子:** 资金{factors.get('fund', 'N/A')} | "
                        f"动量{factors.get('momentum', 'N/A')} | "
                        f"技术{factors.get('technical', 'N/A')} | "
                        f"质量{factors.get('quality', 'N/A')}"
                    )
                sections.append(f"- **理由:** {sig.reasoning}")
                sections.append("")
        else:
            sections.append("*今日无观察股*\n")

        # ── Section 4: All Signals ──
        sections.append("## 4. 全部信号详情\n")

        all_signals = await session.execute(
            select(DailySignals)
            .where(DailySignals.trade_date == trade_date)
            .order_by(DailySignals.score.desc())
        )
        all_sigs = all_signals.scalars().all()

        if all_sigs:
            sections.append("| 代码 | 名称 | 方向 | 评分 | 置信度 | 板块 | 池 |")
            sections.append("|------|------|------|------|--------|------|-----|")
            for sig in all_sigs:
                pool_name = sig.pool_type.value if sig.pool_type else "-"
                sections.append(
                    f"| {sig.code} | {sig.name} | {sig.direction.value} | "
                    f"{sig.score:.4f} | {sig.confidence:.0%} | {sig.sector} | {pool_name} |"
                )
            sections.append("")
        else:
            sections.append("*今日无信号*\n")

        # ── Section 5: Yesterday's Performance ──
        sections.append("## 5. 昨日绩效回顾\n")

        tracker = PerformanceTracker()
        stats = await tracker.get_stats()

        if stats.evaluated > 0:
            sections.append(f"- **总信号数:** {stats.total_signals}")
            sections.append(f"- **已评估:** {stats.evaluated}")
            sections.append(f"- **准确率:** {stats.accuracy:.1%} ({stats.correct}正确 / {stats.wrong}错误 / {stats.neutral}中性)")
            sections.append(f"- **平均收益:** {stats.avg_return:+.2f}%")
            sections.append(f"- **待评估:** {stats.pending}")
        else:
            sections.append("*暂无历史绩效数据*")

        sections.append("")

    await engine.dispose()

    # Build final markdown
    content = "\n".join(sections)

    # Save to file
    briefing_dir = settings.briefing_dir
    briefing_dir.mkdir(parents=True, exist_ok=True)
    filepath = briefing_dir / f"{trade_date}.md"
    filepath.write_text(content, encoding="utf-8")

    # Display in terminal
    console.print(Markdown(content))

    logger.info("Briefing saved to %s", filepath)
    return filepath
