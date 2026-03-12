"""Breakout signal detection — pure functions, no DB dependency.

Runs after Wyckoff calibration. Detects three types of breakouts:
1. Consolidation breakout (resistance/support, reuses Wyckoff support/resistance)
2. MA breakout (MA20/MA60 crossovers)
3. N-day new high/low

Strong signals generate Chinese text descriptions for LLM prompt injection.
Weak signals only adjust the Wyckoff multiplier.
"""

from __future__ import annotations

from dataclasses import dataclass

from aisp.config import BreakoutConfig
from aisp.screening.wyckoff import OHLCV, WyckoffResult, compute_volume_sma


@dataclass
class BreakoutSignal:
    signal_type: str  # e.g. "resistance_breakout", "ma60_breakout", "new_high_60d"
    strength: str | None  # "strong" / "weak" / None
    strength_score: float  # 0.0-1.0
    description: str  # Chinese, for LLM prompt
    multiplier_adj: float  # multiplier adjustment amount
    level: float  # breakout level
    close: float  # closing price
    volume_ratio: float  # vol / vol_sma20


# ── Strength scoring ────────────────────────────────────


def _compute_strength(
    bar: OHLCV,
    vol_ratio: float,
    gap_pct: float,
    config: BreakoutConfig,
    *,
    consolidation_days: int | None = None,
) -> float:
    """Compute breakout strength score (0.0-1.0).

    Formula:
      w_volume   * min(vol_ratio / 3.0, 1.0)
    + w_close_pos * close_position
    + w_body      * body_ratio
    + w_consol    * min(consol_days / 60, 1.0)  (or 0.5 if not consolidation)
    + w_gap       * min(gap_pct / 0.05, 1.0)
    """
    bar_range = bar.high - bar.low
    if bar_range <= 0:
        close_pos = 0.5
        body_ratio = 0.0
    else:
        close_pos = (bar.close - bar.low) / bar_range
        body_ratio = abs(bar.close - bar.open) / bar_range

    consol_score = min(consolidation_days / 60.0, 1.0) if consolidation_days else 0.5

    return (
        config.w_volume * min(vol_ratio / 3.0, 1.0)
        + config.w_close_pos * close_pos
        + config.w_body_ratio * body_ratio
        + config.w_consolidation * consol_score
        + config.w_gap * min(abs(gap_pct) / 0.05, 1.0)
    )


def _classify_strength(
    score: float, config: BreakoutConfig
) -> tuple[str | None, float]:
    """Classify strength and determine multiplier adjustment.

    Returns (strength_label, multiplier_adj).
    """
    if score >= config.strong_threshold:
        return "strong", 0.0  # strong signals don't adjust multiplier; they inject into prompt
    if score >= config.weak_threshold:
        return "weak", 0.0  # caller sets multiplier_adj based on direction
    return None, 0.0


# ── Volume helpers ──────────────────────────────────────


def _vol_ratio_at_last(bars: list[OHLCV], vol_sma: list[float]) -> float:
    """Volume ratio of the last bar relative to its SMA."""
    if not vol_sma or vol_sma[-1] <= 0:
        return 0.0
    return bars[-1].volume / vol_sma[-1]


def _vol_text(vol_ratio: float) -> str:
    """Chinese volume description."""
    if vol_ratio < 1.0:
        return "缩量"
    return f"放量{vol_ratio:.1f}倍"


# ── Simple MA computation ───────────────────────────────


def _simple_ma(values: list[float], period: int) -> float | None:
    """Compute simple MA of the last `period` values."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


# ── Sub-detectors ───────────────────────────────────────


def _detect_consolidation_breakout(
    bars: list[OHLCV],
    vol_sma: list[float],
    config: BreakoutConfig,
    *,
    wyckoff_result: WyckoffResult | None = None,
) -> list[BreakoutSignal]:
    """Detect consolidation breakout (resistance/support).

    If WyckoffResult is available, reuses its support/resistance.
    Otherwise falls back to min(lows)/max(highs) of last N bars.
    """
    signals: list[BreakoutSignal] = []
    curr = bars[-1]
    vol_ratio = _vol_ratio_at_last(bars, vol_sma)

    support: float | None = None
    resistance: float | None = None
    consol_days: int | None = None
    ctx = "盘整"

    if wyckoff_result and wyckoff_result.support is not None and wyckoff_result.resistance is not None:
        support = wyckoff_result.support
        resistance = wyckoff_result.resistance
        # Extract consolidation days from detail string
        detail = wyckoff_result.detail
        if "盘整" in detail:
            try:
                part = detail.split("盘整")[1].split("日")[0]
                consol_days = int(part)
            except (ValueError, IndexError):
                pass
        ctx = f"盘整{consol_days}日" if consol_days else "盘整"
    else:
        # Fallback: use recent highs/lows
        lookback = min(config.fallback_lookback, len(bars) - 1)
        if lookback < 10:
            return signals
        window = bars[-(lookback + 1) : -1]  # exclude current bar
        support = min(b.low for b in window)
        resistance = max(b.high for b in window)
        consol_days = lookback
        ctx = f"近{lookback}日"

    if support is None or resistance is None or support >= resistance:
        return signals

    prev = bars[-2]

    # Resistance breakout: close above resistance
    if curr.close > resistance and prev.close <= resistance:
        gap_pct = (curr.close - resistance) / resistance if resistance > 0 else 0
        strength_score = _compute_strength(
            curr, vol_ratio, gap_pct, config, consolidation_days=consol_days
        )
        label, _ = _classify_strength(strength_score, config)
        adj = config.bullish_multiplier_adj if label == "weak" else 0.0

        vol_desc = _vol_text(vol_ratio)
        desc = (
            f"今日{vol_desc}突破{ctx}阻力位{resistance:.2f}，"
            f"收盘站稳{curr.close:.2f}"
        )
        signals.append(BreakoutSignal(
            signal_type="resistance_breakout",
            strength=label,
            strength_score=strength_score,
            description=desc,
            multiplier_adj=adj,
            level=resistance,
            close=curr.close,
            volume_ratio=vol_ratio,
        ))

    # Support breakdown: close below support
    if curr.close < support and prev.close >= support:
        gap_pct = (support - curr.close) / support if support > 0 else 0
        strength_score = _compute_strength(
            curr, vol_ratio, gap_pct, config, consolidation_days=consol_days
        )
        label, _ = _classify_strength(strength_score, config)
        adj = config.bearish_multiplier_adj if label == "weak" else 0.0

        vol_desc = _vol_text(vol_ratio)
        desc = (
            f"今日{vol_desc}跌破{ctx}支撑位{support:.2f}，"
            f"收于{curr.close:.2f}"
        )
        signals.append(BreakoutSignal(
            signal_type="support_breakdown",
            strength=label,
            strength_score=strength_score,
            description=desc,
            multiplier_adj=adj,
            level=support,
            close=curr.close,
            volume_ratio=vol_ratio,
        ))

    return signals


def _detect_ma_breakout(
    bars: list[OHLCV],
    vol_sma: list[float],
    config: BreakoutConfig,
) -> list[BreakoutSignal]:
    """Detect MA breakout/breakdown for configured periods (default MA20, MA60)."""
    signals: list[BreakoutSignal] = []
    closes = [b.close for b in bars]
    curr = bars[-1]
    prev = bars[-2]
    vol_ratio = _vol_ratio_at_last(bars, vol_sma)

    for period in config.ma_periods:
        ma = _simple_ma(closes, period)
        if ma is None:
            continue

        # Also compute previous MA to check crossover
        prev_closes = closes[:-1]
        prev_ma = _simple_ma(prev_closes, period)
        if prev_ma is None:
            continue

        # Upward breakout: prev_close < prev_ma and curr_close > ma
        if prev.close < prev_ma and curr.close > ma:
            gap_pct = (curr.close - ma) / ma if ma > 0 else 0
            strength_score = _compute_strength(curr, vol_ratio, gap_pct, config)
            label, _ = _classify_strength(strength_score, config)
            adj = config.bullish_multiplier_adj if label == "weak" else 0.0

            vol_desc = _vol_text(vol_ratio)
            desc = (
                f"今日{vol_desc}突破{period}日均线({ma:.2f})，"
                f"收于{curr.close:.2f}"
            )
            signals.append(BreakoutSignal(
                signal_type=f"ma{period}_breakout",
                strength=label,
                strength_score=strength_score,
                description=desc,
                multiplier_adj=adj,
                level=round(ma, 2),
                close=curr.close,
                volume_ratio=vol_ratio,
            ))

        # Downward breakdown: prev_close > prev_ma and curr_close < ma
        elif prev.close > prev_ma and curr.close < ma:
            gap_pct = (ma - curr.close) / ma if ma > 0 else 0
            strength_score = _compute_strength(curr, vol_ratio, gap_pct, config)
            label, _ = _classify_strength(strength_score, config)
            adj = config.bearish_multiplier_adj if label == "weak" else 0.0

            desc = (
                f"今日跌破{period}日均线({ma:.2f})，"
                f"收于{curr.close:.2f}"
            )
            signals.append(BreakoutSignal(
                signal_type=f"ma{period}_breakdown",
                strength=label,
                strength_score=strength_score,
                description=desc,
                multiplier_adj=adj,
                level=round(ma, 2),
                close=curr.close,
                volume_ratio=vol_ratio,
            ))

    return signals


def _detect_new_high_low(
    bars: list[OHLCV],
    vol_sma: list[float],
    config: BreakoutConfig,
) -> list[BreakoutSignal]:
    """Detect N-day new high or new low."""
    signals: list[BreakoutSignal] = []
    period = config.new_high_low_period

    if len(bars) < period + 1:
        return signals

    curr = bars[-1]
    vol_ratio = _vol_ratio_at_last(bars, vol_sma)
    window = bars[-(period + 1) : -1]  # exclude current bar

    prev_high = max(b.high for b in window)
    prev_low = min(b.low for b in window)

    # New high
    if curr.close > prev_high:
        gap_pct = (curr.close - prev_high) / prev_high if prev_high > 0 else 0
        strength_score = _compute_strength(curr, vol_ratio, gap_pct, config)
        label, _ = _classify_strength(strength_score, config)
        adj = config.bullish_multiplier_adj if label == "weak" else 0.0

        vol_desc = _vol_text(vol_ratio)
        desc = (
            f"今日创{period}日新高，{vol_desc}，"
            f"收于{curr.close:.2f}（前高{prev_high:.2f}）"
        )
        signals.append(BreakoutSignal(
            signal_type=f"new_high_{period}d",
            strength=label,
            strength_score=strength_score,
            description=desc,
            multiplier_adj=adj,
            level=prev_high,
            close=curr.close,
            volume_ratio=vol_ratio,
        ))

    # New low
    if curr.close < prev_low:
        gap_pct = (prev_low - curr.close) / prev_low if prev_low > 0 else 0
        strength_score = _compute_strength(curr, vol_ratio, gap_pct, config)
        label, _ = _classify_strength(strength_score, config)
        adj = config.bearish_multiplier_adj if label == "weak" else 0.0

        desc = (
            f"今日创{period}日新低，"
            f"收于{curr.close:.2f}（前低{prev_low:.2f}）"
        )
        signals.append(BreakoutSignal(
            signal_type=f"new_low_{period}d",
            strength=label,
            strength_score=strength_score,
            description=desc,
            multiplier_adj=adj,
            level=prev_low,
            close=curr.close,
            volume_ratio=vol_ratio,
        ))

    return signals


# ── Public API ──────────────────────────────────────────


def detect_breakouts(
    bars: list[OHLCV],
    config: BreakoutConfig,
    *,
    wyckoff_result: WyckoffResult | None = None,
) -> list[BreakoutSignal]:
    """Detect breakout signals from OHLCV bars.

    Runs after Wyckoff calibration, reusing WyckoffResult's support/resistance
    to avoid redundant computation.

    Returns a list of BreakoutSignal (may be empty).
    """
    if len(bars) < 20:
        return []

    vol_sma = compute_volume_sma(bars)

    signals: list[BreakoutSignal] = []
    signals.extend(
        _detect_consolidation_breakout(bars, vol_sma, config, wyckoff_result=wyckoff_result)
    )
    signals.extend(_detect_ma_breakout(bars, vol_sma, config))
    signals.extend(_detect_new_high_low(bars, vol_sma, config))

    return signals
