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
    GlobalDaily,
    PoolType,
    SectorDaily,
    SectorPoolState,
    StkDaily,
)
from aisp.review.tracker import PerformanceTracker
from aisp.screening.indicators import compute_macd, compute_rsi_multi

logger = logging.getLogger(__name__)
console = Console()


def _rsi_one_label(rsi: float | None) -> str:
    if rsi is None:
        return "-"
    if rsi >= 70:
        return f"**{rsi:.1f}**(超买)"
    if rsi <= 30:
        return f"**{rsi:.1f}**(超卖)"
    return f"{rsi:.1f}"


def _rsi_multi_label(rsi_map: dict[int, float | None]) -> str:
    parts = [f"RSI({p})={_rsi_one_label(v)}" for p, v in sorted(rsi_map.items())]
    return " | ".join(parts) if any(v is not None for v in rsi_map.values()) else "数据不足"


def _macd_label(macd_data: dict | None) -> str:
    if macd_data is None:
        return "数据不足"
    hist = macd_data["histogram"]
    direction = "多头" if hist > 0 else "空头"
    return f"DIF {macd_data['macd']:.3f} / DEA {macd_data['signal']:.3f} / 柱 {hist:+.3f} ({direction})"


def _build_signal_card(
    sig, stk: StkDaily | None, closes: list[float], sector: SectorDaily | None,
) -> list[str]:
    """Build a detailed Markdown card for one signal."""
    lines: list[str] = []
    f = sig.factor_scores or {}
    weights = f.get("_weights", {})
    veto = f.get("_veto")
    key_risks = f.get("_key_risks", [])
    catalysts = f.get("_catalysts", [])

    direction_cn = {
        "strong_buy": "🔥 强烈买入", "buy": "✅ 买入", "weak_buy": "📈 弱买入",
        "hold": "⏸️ 持有", "watch": "👀 观察",
        "weak_sell": "📉 弱卖出", "sell": "❌ 卖出", "strong_sell": "🚨 强烈卖出",
    }.get(sig.direction.value, sig.direction.value)
    pool_name = sig.pool_type.value if sig.pool_type else "-"

    lines.append(f"### {sig.name}({sig.code}) — {direction_cn}")
    lines.append(f"**板块:** {sig.sector} | **池:** {pool_name} | "
                 f"**综合评分:** {sig.score:.4f} | **置信度:** {sig.confidence:.0%}")
    if veto:
        lines.append(f"> ⚠️ **否决警告:** {veto}")
    lines.append("")

    # ── 日线数据 ──
    if stk:
        lines.append("**日线数据**")
        lines.append(
            "| 收盘价 | 涨跌幅 | 成交量(手) | 成交额(万) | 换手率 | 量比 | 主力净流入(万) |"
        )
        lines.append("|--------|--------|-----------|-----------|--------|------|---------------|")
        vol_str = f"{stk.volume / 100:.0f}" if stk.volume else "N/A"
        amt_str = f"{stk.amount / 1e4:.0f}" if stk.amount else "N/A"
        inflow_str = f"{stk.net_inflow / 1e4:+.0f}" if stk.net_inflow is not None else "N/A"
        lines.append(
            f"| {stk.close:.2f} | {stk.change_pct:+.2f}% | {vol_str} | {amt_str} | "
            f"{stk.turnover_rate or 0:.2f}% | {stk.volume_ratio or 0:.2f} | {inflow_str} |"
        )
        lines.append("")

    # ── 技术指标 ──
    rsi_map = compute_rsi_multi(closes) if closes else {}
    macd = compute_macd(closes) if closes else None

    lines.append("**技术指标**")
    lines.append(f"- {_rsi_multi_label(rsi_map)}")
    lines.append(f"- MACD: {_macd_label(macd)}")
    if sector:
        ma_parts = []
        for period, val in [(5, sector.ma5), (10, sector.ma10), (20, sector.ma20), (60, sector.ma60)]:
            if val is not None:
                above = ">" if stk and stk.close > val else "<" if stk else ""
                ma_parts.append(f"MA{period}={val:.2f}{above}")
        if ma_parts:
            lines.append(f"- 板块均线: {' | '.join(ma_parts)}")
    lines.append("")

    # ── 8因子评分 ──
    factor_names = {
        "fund": "资金面", "momentum": "动量", "technical": "量价", "quality": "质量",
        "indicators": "技术指标", "macro": "宏观联动", "sentiment": "舆情", "sector": "板块动量",
    }
    lines.append("**8因子评分**")
    lines.append("| 因子 | 评分 | 权重 | 状态 |")
    lines.append("|------|------|------|------|")
    for key, cn_name in factor_names.items():
        score = f.get(key)
        weight = weights.get(key)
        if score is not None:
            s = float(score)
            if s >= 0.7:
                status = "强"
            elif s >= 0.4:
                status = "中"
            else:
                status = "弱"
            w_str = f"{float(weight):.0%}" if weight is not None else "-"
            lines.append(f"| {cn_name} | {s:.2f} | {w_str} | {status} |")
    lines.append("")

    # ── 板块概况 ──
    if sector:
        lines.append("**所属板块概况**")
        lines.append(
            f"- 板块涨跌: {sector.change_pct:+.2f}% | "
            f"上涨/下跌: {sector.up_count}/{sector.down_count} | "
            f"总股票数: {sector.stock_count}"
        )
        if sector.net_inflow is not None:
            lines.append(f"- 板块资金净流入: {sector.net_inflow:+.2f}亿")
        lines.append("")

    # ── 突破信号 ──
    breakout_data = f.get("_breakout")
    if breakout_data:
        lines.append("**突破信号**")
        for b in breakout_data:
            desc = b.get("description", "")
            score = b.get("strength_score", 0)
            lines.append(f"- {desc}（强度: {score:.2f}）")
        lines.append("")

    # ── LLM 分析 ──
    lines.append("**LLM 分析**")
    lines.append(f"- {sig.reasoning}")
    if key_risks:
        lines.append(f"- **风险:** {' / '.join(key_risks)}")
    if catalysts:
        lines.append(f"- **催化剂:** {' / '.join(catalysts)}")
    lines.append("")
    lines.append("---")
    lines.append("")

    return lines


async def generate_briefing(trade_date: date, *, btc_metrics=None) -> Path:
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

        if btc_metrics is not None:
            sections.append(
                f"**BTC 风险偏好:** {btc_metrics.sentiment_label} ({btc_metrics.risk_score:.2f})"
                f" | ${btc_metrics.price:,.0f}"
                f" | 24h {btc_metrics.change_24h:+.1f}%"
                f" | 7d {btc_metrics.change_7d:+.1f}%"
                f" | 30d {btc_metrics.change_30d:+.1f}%\n"
            )

        if not globals_:
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
                vol_yi = s.amount if s.amount else 0
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
                vol_yi = s.amount if s.amount else 0
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

        # ── Section 3: Signal Overview ──
        sections.append("## 3. 信号总览\n")

        overview_signals = await session.execute(
            select(DailySignals)
            .where(DailySignals.trade_date == trade_date)
            .order_by(DailySignals.score.desc())
        )
        overview_list = overview_signals.scalars().all()

        if overview_list:
            direction_cn_map = {
                "strong_buy": "🔥强买", "buy": "✅买入", "weak_buy": "📈弱买",
                "hold": "⏸️持有", "watch": "👀观察",
                "weak_sell": "📉弱卖", "sell": "❌卖出", "strong_sell": "🚨强卖",
            }
            sections.append("| # | 代码 | 名称 | 方向 | 评分 | 置信度 | 板块 | 池 |")
            sections.append("|---|------|------|------|------|--------|------|-----|")
            for rank, sig in enumerate(overview_list, 1):
                dir_cn = direction_cn_map.get(sig.direction.value, sig.direction.value)
                pool_name = sig.pool_type.value if sig.pool_type else "-"
                sections.append(
                    f"| {rank} | {sig.code} | {sig.name} | {dir_cn} | "
                    f"{sig.score:.4f} | {sig.confidence:.0%} | {sig.sector} | {pool_name} |"
                )
            sections.append("")
        else:
            sections.append("*今日无信号*\n")

        # ── Section 4: All Signals (detailed) ──
        sections.append("## 4. 全部信号详情\n")

        all_signals = await session.execute(
            select(DailySignals)
            .where(DailySignals.trade_date == trade_date)
            .order_by(DailySignals.score.desc())
        )
        all_sigs = all_signals.scalars().all()

        if all_sigs:
            # Batch-query stock daily data + historical closes for all signal codes
            sig_codes = [sig.code for sig in all_sigs]

            # Current day data
            stk_q = await session.execute(
                select(StkDaily).where(
                    StkDaily.trade_date == trade_date,
                    StkDaily.code.in_(sig_codes),
                )
            )
            stk_map = {s.code: s for s in stk_q.scalars().all()}

            # Historical closes (70 trading days) for RSI/MACD
            from datetime import timedelta

            cutoff = trade_date - timedelta(days=105)
            hist_q = await session.execute(
                select(StkDaily.code, StkDaily.trade_date, StkDaily.close)
                .where(
                    StkDaily.code.in_(sig_codes),
                    StkDaily.trade_date >= cutoff,
                    StkDaily.trade_date <= trade_date,
                )
                .order_by(StkDaily.trade_date)
            )
            closes_by_code: dict[str, list[float]] = {}
            for code, _, close in hist_q.all():
                closes_by_code.setdefault(code, []).append(float(close))

            # Sector daily data for each signal's sector
            sig_sectors = list({sig.sector for sig in all_sigs})
            sector_q = await session.execute(
                select(SectorDaily).where(
                    SectorDaily.trade_date == trade_date,
                    SectorDaily.sector_name.in_(sig_sectors),
                )
            )
            sector_map = {s.sector_name: s for s in sector_q.scalars().all()}

            for sig in all_sigs:
                sections.extend(
                    _build_signal_card(
                        sig, stk_map.get(sig.code), closes_by_code.get(sig.code, []),
                        sector_map.get(sig.sector),
                    )
                )
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
