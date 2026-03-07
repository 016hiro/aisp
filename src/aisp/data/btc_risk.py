"""BTC risk appetite indicator — multi-timeframe composite score.

Fetches BTC-USD data via yfinance and computes a 0-1 risk score from 5 dimensions:
- Short-term momentum (24h change)
- Mid-term trend (7d change)
- Long-term trend (30d change)
- Volatility regime (7d/30d realized vol ratio)
- Drawdown from 30d high

Not persisted to DB — computed on demand and passed through the pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass

logger = logging.getLogger(__name__)


def _normalize(value: float, lo: float, hi: float) -> float:
    """Clip value to [lo, hi] and linearly map to [0, 1]."""
    clamped = max(lo, min(hi, value))
    return (clamped - lo) / (hi - lo)


@dataclass
class BtcRiskMetrics:
    price: float
    change_24h: float  # %
    change_7d: float  # %
    change_30d: float  # %
    volatility_7d: float  # annualized %
    volatility_30d: float  # annualized %
    drawdown_from_30d_high: float  # negative %
    risk_score: float  # 0-1 composite

    def to_prompt_text(self) -> str:
        """Format as structured text for LLM prompt injection."""
        return (
            f"\n### BTC 风险偏好指标\n"
            f"- BTC 现价: ${self.price:,.0f}\n"
            f"- 24h 涨跌: {self.change_24h:+.1f}% | 7d: {self.change_7d:+.1f}% | 30d: {self.change_30d:+.1f}%\n"
            f"- 7d 波动率: {self.volatility_7d:.1f}% | 30d 波动率: {self.volatility_30d:.1f}%\n"
            f"- 距30日高点回撤: {self.drawdown_from_30d_high:.1f}%\n"
            f"- 风险偏好评分: {self.risk_score:.2f} ({self.sentiment_label})"
        )

    @property
    def sentiment_label(self) -> str:
        if self.risk_score > 0.65:
            return "强风险偏好"
        elif self.risk_score < 0.35:
            return "弱风险偏好"
        return "中性"


def compute_risk_score(
    change_24h: float,
    change_7d: float,
    change_30d: float,
    vol_7d: float,
    vol_30d: float,
    drawdown_pct: float,
) -> float:
    """Compute composite risk score from 5 dimensions."""
    # Short-term momentum: clip [-10%, +10%] → [0, 1], weight 0.15
    short = _normalize(change_24h, -10.0, 10.0)

    # Mid-term trend: clip [-20%, +20%] → [0, 1], weight 0.25
    mid = _normalize(change_7d, -20.0, 20.0)

    # Long-term trend: clip [-30%, +30%] → [0, 1], weight 0.25
    long_ = _normalize(change_30d, -30.0, 30.0)

    # Volatility regime: ratio 0.5→1.0, 2.0→0.0, weight 0.20
    if vol_30d > 0:
        vol_ratio = vol_7d / vol_30d
        vol_score = _normalize(vol_ratio, 0.5, 2.0)
        vol_score = 1.0 - vol_score  # invert: low ratio = good
    else:
        vol_score = 0.5

    # Drawdown from 30d high: 0%→1.0, -30%→0.0, weight 0.15
    dd_score = _normalize(drawdown_pct, -30.0, 0.0)

    return (
        0.15 * short
        + 0.25 * mid
        + 0.25 * long_
        + 0.20 * vol_score
        + 0.15 * dd_score
    )


async def fetch_btc_risk_metrics() -> BtcRiskMetrics | None:
    """Fetch BTC 45-day history via yfinance and compute risk metrics.

    Returns None on failure (graceful degradation).
    """
    try:
        import yfinance as yf

        def _download():
            ticker = yf.Ticker("BTC-USD")
            return ticker.history(period="45d")

        df = await asyncio.to_thread(_download)

        if df is None or len(df) < 2:
            logger.warning("BTC data insufficient (got %d rows)", len(df) if df is not None else 0)
            return None

        closes = df["Close"].values
        current_price = float(closes[-1])

        # Changes
        change_24h = (closes[-1] / closes[-2] - 1) * 100 if len(closes) >= 2 else 0.0

        def _pct_change(days: int) -> float:
            if len(closes) > days:
                return (closes[-1] / closes[-(days + 1)] - 1) * 100
            return (closes[-1] / closes[0] - 1) * 100

        change_7d = _pct_change(7)
        change_30d = _pct_change(30)

        # Realized volatility (annualized)
        import numpy as np

        log_returns = np.diff(np.log(closes))

        def _vol(n: int) -> float:
            if len(log_returns) >= n:
                return float(np.std(log_returns[-n:], ddof=1) * math.sqrt(252) * 100)
            return float(np.std(log_returns, ddof=1) * math.sqrt(252) * 100)

        vol_7d = _vol(7)
        vol_30d = _vol(30)

        # Drawdown from 30d high
        high_30d = float(np.max(closes[-30:])) if len(closes) >= 30 else float(np.max(closes))
        drawdown = (current_price / high_30d - 1) * 100

        score = compute_risk_score(change_24h, change_7d, change_30d, vol_7d, vol_30d, drawdown)

        metrics = BtcRiskMetrics(
            price=float(current_price),
            change_24h=float(round(change_24h, 2)),
            change_7d=float(round(change_7d, 2)),
            change_30d=float(round(change_30d, 2)),
            volatility_7d=float(round(vol_7d, 2)),
            volatility_30d=float(round(vol_30d, 2)),
            drawdown_from_30d_high=float(round(drawdown, 2)),
            risk_score=float(round(score, 4)),
        )
        logger.info("BTC risk metrics: score=%.2f (%s), price=$%.0f", score, metrics.sentiment_label, current_price)
        return metrics

    except Exception:
        logger.warning("Failed to fetch BTC risk metrics", exc_info=True)
        return None
