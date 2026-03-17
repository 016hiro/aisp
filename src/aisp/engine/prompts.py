"""LLM prompt templates — loaded from config/prompts.toml."""

from __future__ import annotations

import functools
from pathlib import Path

import tomlkit

_PROMPTS_PATH = Path(__file__).resolve().parents[3] / "config" / "prompts.toml"


@functools.lru_cache(maxsize=1)
def _load_prompts() -> dict:
    """Load and cache all prompt templates from TOML."""
    with open(_PROMPTS_PATH, encoding="utf-8") as f:
        return tomlkit.load(f)


def get_template(name: str) -> str:
    """Get a raw prompt template by section name."""
    return _load_prompts()[name]["template"]


# ── Formatting helpers ──

# Factor interpretation: 5 tiers per factor
_FACTOR_LABELS: dict[str, list[str]] = {
    "fund": ["主力大幅流出", "资金偏弱", "中性", "偏强", "主力大幅流入"],
    "momentum": ["大幅落后板块", "偏弱", "居中", "领先", "板块领涨"],
    "technical": ["缩量低迷", "量价偏弱", "量价中性", "量价活跃", "放量异动"],
    "quality": ["小盘低质", "偏小", "中等", "偏大", "大盘龙头"],
    "indicators": ["超卖区域", "偏弱", "中性", "偏强", "超买区域"],
    "macro": ["全球利空", "偏不利", "中性", "偏有利", "全球利好"],
    "sentiment": ["恐慌", "偏空", "中性", "偏多", "狂热"],
    "sector": ["板块弱势", "偏弱", "中性", "偏强", "板块强势"],
}

_FACTOR_NAMES: dict[str, str] = {
    "fund": "资金面",
    "momentum": "动量",
    "technical": "量价",
    "quality": "质量",
    "indicators": "技术指标",
    "macro": "宏观联动",
    "sentiment": "舆情",
    "sector": "板块动量",
}


def _interpret_factor(name: str, score: float) -> str:
    """Interpret a factor score into a Chinese label."""
    labels = _FACTOR_LABELS.get(name, ["极低", "偏低", "中性", "偏高", "极高"])
    if score < 0.2:
        return labels[0]
    if score < 0.4:
        return labels[1]
    if score <= 0.6:
        return labels[2]
    if score <= 0.8:
        return labels[3]
    return labels[4]


def format_factor_table(
    factor_scores: dict[str, float],
    dynamic_weights: dict[str, float],
    raw_indicators: dict | None = None,
) -> str:
    """Generate a markdown factor table sorted by weight descending."""
    rows = []
    for name in sorted(factor_scores, key=lambda n: dynamic_weights.get(n, 0), reverse=True):
        score = factor_scores[name]
        weight = dynamic_weights.get(name, 0)
        label = _interpret_factor(name, score)
        display = _FACTOR_NAMES.get(name, name)

        # Enrich indicators row with RSI/MACD
        extra = ""
        if name == "indicators" and raw_indicators:
            parts = []
            rsi = raw_indicators.get("rsi6")
            if rsi is not None:
                parts.append(f"RSI6={rsi:.0f}")
            hist = raw_indicators.get("macd_hist")
            if hist is not None:
                parts.append(f"MACD柱{'>' if hist > 0 else '<'}0")
            if parts:
                extra = f" ({', '.join(parts)})"

        rows.append(f"| {display} | {score:.2f} | {label}{extra} | {weight:.0%} |")

    header = "| 因子 | 分数 | 解读 | 权重 |\n|------|------|------|------|\n"
    return header + "\n".join(rows)


def format_kline_history(recent_ohlcv: list[dict]) -> str:
    """Format recent OHLCV bars into a compact markdown table."""
    if not recent_ohlcv:
        return "K线数据不可用"

    header = "| T | 开 | 高 | 低 | 收 | 量(万手) | 涨跌% |\n|---|----|----|----|----|---------|-------|\n"
    rows = []
    for idx, bar in enumerate(recent_ohlcv):
        t = idx - len(recent_ohlcv) + 1  # -14, -13, ..., 0
        vol_wan = bar["v"] / 10000
        # Change pct from previous bar
        if idx > 0:
            prev_c = recent_ohlcv[idx - 1]["c"]
            chg = (bar["c"] - prev_c) / prev_c * 100 if prev_c else 0
            chg_str = f"{chg:+.1f}%"
        else:
            chg_str = "—"
        rows.append(
            f"| {t} | {bar['o']:.2f} | {bar['h']:.2f} | {bar['l']:.2f} "
            f"| {bar['c']:.2f} | {vol_wan:.0f} | {chg_str} |"
        )
    return header + "\n".join(rows)


def format_trend_summary(trend: dict) -> str:
    """Convert _trend dict into a one-line Chinese summary."""
    if not trend:
        return "趋势数据不可用"

    consec = trend.get("consecutive_down", 0)
    cum_pct = trend.get("cumulative_pct", 0.0)
    ma5_below = trend.get("ma5_below_ma20", False)

    parts = []
    if consec > 0:
        parts.append(f"连跌{consec}日累计{cum_pct:+.1f}%")
    else:
        parts.append("近期无连跌")

    parts.append(f"MA5{'<' if ma5_below else '>'}MA20")
    parts.append("短期空头" if ma5_below else "短期健康")

    return ", ".join(parts)


def format_market_sentiment(row) -> str:
    """Format MarketSentiment row into a compact one-line summary."""
    if row is None:
        return "市场情绪数据不可用"

    parts = []
    if row.total_amount is not None:
        parts.append(f"两市成交{row.total_amount:.1f}亿" if row.total_amount < 10000
                     else f"两市成交{row.total_amount / 10000:.2f}万亿")

    zt = row.limit_up_count
    real_zt = row.real_limit_up
    if zt is not None:
        zt_str = f"涨停{zt}"
        if real_zt is not None and real_zt != zt:
            zt_str += f"(实板{real_zt})"
        parts.append(zt_str)

    if row.limit_down_count is not None:
        parts.append(f"跌停{row.limit_down_count}")

    if row.blast_rate is not None:
        parts.append(f"炸板率{row.blast_rate:.0f}%")

    if row.max_streak is not None and row.max_streak > 1:
        parts.append(f"最高{row.max_streak}板")

    if row.prev_zt_premium is not None:
        parts.append(f"昨涨停溢价{row.prev_zt_premium:+.1f}%")

    return " | ".join(parts) if parts else "市场情绪数据不完整"


def format_stock_identity(
    profile: dict | None,
    pe_ttm: float | None,
    pb_mrq: float | None,
    close: float | None = None,
) -> str:
    """Format stock identity from profile dict + valuation + market cap."""
    parts = []

    if profile:
        board_labels = {
            "gem": "创业板", "star": "科创板",
            "main_sh": "沪市主板", "main_sz": "深市主板",
        }
        bt = profile.get("board_type", "")
        parts.append(board_labels.get(bt, bt))

        liq = profile.get("liq_shares")
        if liq and liq > 0 and close and close > 0:
            cap_yi = liq * close / 1e8  # 流通市值(亿)
            if cap_yi >= 1000:
                cap_label = "大盘"
            elif cap_yi >= 300:
                cap_label = "中大盘"
            elif cap_yi >= 100:
                cap_label = "中盘"
            elif cap_yi >= 30:
                cap_label = "小盘"
            else:
                cap_label = "微盘"
            parts.append(f"流通市值{cap_yi:.0f}亿({cap_label})")

    if pe_ttm is not None:
        parts.append(f"PE(TTM){pe_ttm:.1f}")
    if pb_mrq is not None:
        parts.append(f"PB{pb_mrq:.1f}")

    return " | ".join(parts) if parts else ""


def format_fund_flow_detail(raw_data: dict) -> str:
    """Format fund flow breakdown into a markdown table."""
    fields = [
        ("超大单", "super_large_net", "super_large_pct"),
        ("大单", "large_net", "large_pct"),
        ("中单", "medium_net", "medium_pct"),
        ("小单", "small_net", "small_pct"),
    ]

    has_data = any(raw_data.get(net) is not None for _, net, _ in fields)
    if not has_data:
        return ""

    header = "| 类型 | 净额(万) | 占比 |\n|------|---------|------|\n"
    rows = []
    for label, net_key, pct_key in fields:
        net = raw_data.get(net_key)
        pct = raw_data.get(pct_key)
        net_str = f"{net / 10000:+.0f}" if net is not None else "—"
        pct_str = f"{pct:+.1f}%" if pct is not None else "—"
        rows.append(f"| {label} | {net_str} | {pct_str} |")

    main_net = raw_data.get("main_net")
    if main_net is not None:
        main_pct = raw_data.get("main_pct")
        pct_str = f"{main_pct:+.1f}%" if main_pct is not None else "—"
        rows.append(f"| **主力合计** | **{main_net / 10000:+.0f}** | **{pct_str}** |")

    return header + "\n".join(rows)


def format_position_info(info: dict | None) -> str:
    """Format position info dict into a compact one-line summary."""
    if not info:
        return ""

    parts = []
    h60 = info.get("dist_60d_high_pct")
    l60 = info.get("dist_60d_low_pct")
    if h60 is not None:
        parts.append(f"距60日高{h60:+.1f}%")
    if l60 is not None:
        parts.append(f"距60日低{l60:+.1f}%")

    h120 = info.get("dist_120d_high_pct")
    if h120 is not None:
        parts.append(f"距120日高{h120:+.1f}%")

    ytd = info.get("ytd_pct")
    if ytd is not None:
        parts.append(f"YTD{ytd:+.1f}%")

    t5 = info.get("turnover_5d")
    if t5 is not None:
        parts.append(f"5日累计换手{t5:.1f}%")

    return " | ".join(parts) if parts else ""


def format_lhb_info(data: dict | None) -> str:
    """Format LHB info into a one-line summary, or empty string if no data."""
    if not data:
        return ""

    parts = ["龙虎榜"]
    net = data.get("net_buy")
    if net is not None:
        parts.append(f"净买{net / 10000:+.0f}万")
    reason = data.get("reason")
    if reason:
        parts.append(f"原因: {reason}")
    return ": ".join(parts[:2]) + (f" | {parts[2]}" if len(parts) > 2 else "")


def format_stock_analysis(**kwargs) -> str:
    """Format stock analysis prompt with new enriched fields."""
    kwargs.setdefault("veto_warning", "")
    kwargs.setdefault("extra_instructions", "")
    kwargs.setdefault("kline_data_quality", "")
    kwargs.setdefault("global_data_quality", "")
    kwargs.setdefault("stock_identity", "")
    kwargs.setdefault("market_sentiment", "")
    kwargs.setdefault("position_info", "")
    kwargs.setdefault("fund_flow_detail", "")
    kwargs.setdefault("lhb_info", "")
    return get_template("stock_analysis").format(**kwargs)


def format_sentiment_classification(**kwargs) -> str:
    """Format sentiment classification prompt."""
    return get_template("sentiment_classification").format(**kwargs)


def format_watchlist_nlp(**kwargs) -> str:
    """Format watchlist NLP prompt."""
    return get_template("watchlist_nlp").format(**kwargs)


def format_ocr_positions(today: str) -> str:
    """Format OCR positions extraction prompt."""
    return get_template("ocr_positions").format(today=today)


def format_ocr_trades() -> str:
    """Format OCR trades extraction prompt."""
    return get_template("ocr_trades")
