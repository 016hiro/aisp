"""Wyckoff phase detection — pure functions, no DB dependency.

Post-scoring calibration layer: identifies Accumulation/Distribution phases
and returns a multiplier to adjust the 8-factor total_score.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from aisp.config import WyckoffConfig


class WyckoffPhase(enum.StrEnum):
    UNKNOWN = "unknown"
    ACCUMULATION = "accumulation"
    DISTRIBUTION = "distribution"


class WyckoffEvent(enum.StrEnum):
    SPRING = "spring"   # 假跌破支撑 + 低量 + 收回
    SOS = "sos"         # 放量突破盘整上沿
    LPS = "lps"         # 缩量回踩确认
    UT = "ut"           # 假突破阻力 + 量不足 + 收回
    SOW = "sow"         # 放量跌破盘整下沿
    LPSY = "lpsy"       # 缩量反弹失败


@dataclass
class WyckoffResult:
    phase: WyckoffPhase
    confidence: float  # 0.0-1.0
    detected_events: list[WyckoffEvent] = field(default_factory=list)
    multiplier: float = 1.0
    detail: str = ""  # 中文描述, 供 LLM prompt
    support: float | None = None
    resistance: float | None = None


@dataclass
class OHLCV:
    open: float
    high: float
    low: float
    close: float
    volume: float


# ── Helper computations ──────────────────────────────────


def compute_atr(bars: list[OHLCV], period: int = 20) -> list[float]:
    """Compute Average True Range series.

    Returns a list of ATR values aligned with bars (first `period` entries are NaN-free
    after enough data). Length = len(bars) - 1 for the TR values, then rolling average.
    Returns empty list if insufficient data.
    """
    if len(bars) < 2:
        return []

    trs: list[float] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        high = bars[i].high
        low = bars[i].low
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    if len(trs) < period:
        return trs  # return raw TRs if not enough for smoothing

    # Wilder smoothing
    atr_values: list[float] = []
    atr = sum(trs[:period]) / period
    atr_values.extend([atr] * period)  # backfill first period entries

    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
        atr_values.append(atr)

    return atr_values


def compute_volume_sma(bars: list[OHLCV], period: int = 20) -> list[float]:
    """Compute volume simple moving average. Returns list aligned with bars."""
    if not bars:
        return []

    volumes = [b.volume for b in bars]
    result: list[float] = []

    for i in range(len(volumes)):
        start = max(0, i - period + 1)
        window = volumes[start : i + 1]
        result.append(sum(window) / len(window))

    return result


# ── Structural detection: SC/BC + AR anchoring ───────────


def _find_climax_bar(
    bars: list[OHLCV],
    vol_sma: list[float],
    atr_values: list[float],
    *,
    min_consolidation_days: int = 20,
    vol_climax_ratio: float = 2.0,
    range_climax_ratio: float = 1.5,
) -> tuple[int, str] | None:
    """Find the most significant climax bar (Selling Climax or Buying Climax).

    A climax bar has abnormally high volume AND large price range.
    SC (bearish close) anchors support; BC (bullish close) anchors resistance.

    Returns (bar_index, "sc"|"bc") or None.
    """
    search_start = 10  # skip vol_sma/ATR warmup
    search_end = len(bars) - min_consolidation_days
    if search_start >= search_end:
        return None

    best_score = 0.0
    best_idx = -1
    best_type = ""

    for i in range(search_start, search_end):
        bar = bars[i]
        bar_range = bar.high - bar.low
        if bar_range <= 0:
            continue

        atr_idx = i - 1  # atr_values[j] aligns with bars[j+1]
        if atr_idx < 0 or atr_idx >= len(atr_values) or i >= len(vol_sma):
            continue

        vol_avg = vol_sma[i]
        atr = atr_values[atr_idx]
        if vol_avg <= 0 or atr <= 0:
            continue

        vol_ratio = bar.volume / vol_avg
        range_ratio = bar_range / atr
        if vol_ratio < vol_climax_ratio or range_ratio < range_climax_ratio:
            continue

        score = vol_ratio * range_ratio
        if score > best_score:
            best_score = score
            best_idx = i
            close_pos = (bar.close - bar.low) / bar_range
            best_type = "bc" if close_pos > 0.6 else "sc"

    return (best_idx, best_type) if best_idx >= 0 else None


def _find_automatic_reaction(
    bars: list[OHLCV],
    climax_idx: int,
    climax_type: str,
    *,
    max_lookforward: int = 10,
) -> int | None:
    """Find the Automatic Reaction (AR) bar after a climax.

    For SC: AR = bar with highest high in 1..max_lookforward bars after SC.
    For BC: AR = bar with lowest low in 1..max_lookforward bars after BC.
    """
    start = climax_idx + 1
    end = min(climax_idx + max_lookforward + 1, len(bars))
    if start >= end:
        return None

    window = bars[start:end]
    if climax_type == "sc":
        ar_local = max(range(len(window)), key=lambda j: window[j].high)
    else:
        ar_local = min(range(len(window)), key=lambda j: window[j].low)

    return start + ar_local


def _detect_consolidation_structural(
    bars: list[OHLCV],
    vol_sma: list[float],
    atr_values: list[float],
    *,
    min_consolidation_days: int,
    max_range_ratio: float,
) -> tuple[bool, int, float, float] | None:
    """Detect trading range using Wyckoff structural events (SC/AR or BC/AR).

    Returns (True, consol_days, support, resistance) or None if no pattern.
    SC low = support, AR high = resistance (accumulation).
    BC high = resistance, AR low = support (distribution).
    """
    climax = _find_climax_bar(
        bars, vol_sma, atr_values,
        min_consolidation_days=min_consolidation_days,
    )
    if climax is None:
        return None

    climax_idx, climax_type = climax
    ar_idx = _find_automatic_reaction(bars, climax_idx, climax_type)
    if ar_idx is None:
        return None

    if climax_type == "sc":
        support = bars[climax_idx].low
        resistance = bars[ar_idx].high
    else:
        resistance = bars[climax_idx].high
        support = bars[ar_idx].low

    if support >= resistance:
        return None
    mid = (resistance + support) / 2
    if mid <= 0 or (resistance - support) / mid > max_range_ratio:
        return None

    consol_days = len(bars) - climax_idx
    if consol_days < min_consolidation_days:
        return None

    return True, consol_days, support, resistance


# ── Fallback: ATR-based range detection ───────────────────


def _detect_consolidation_range(
    bars: list[OHLCV],
    atr_values: list[float],
    *,
    atr_ratio_threshold: float,
    min_consolidation_days: int,
    max_range_ratio: float,
) -> tuple[bool, int, float, float]:
    """Fallback consolidation detection via ATR-ratio backward scan.

    Used when no structural climax event is found.
    """
    n = len(atr_values)
    consol_days = 0
    support = bars[-1].low
    resistance = bars[-1].high

    for i in range(n - 1, -1, -1):
        bar_idx = i + 1
        close = bars[bar_idx].close
        if close == 0:
            break
        ratio = atr_values[i] / close
        if ratio >= atr_ratio_threshold:
            break

        new_support = min(support, bars[bar_idx].low)
        new_resistance = max(resistance, bars[bar_idx].high)
        mid = (new_resistance + new_support) / 2
        if mid > 0 and (new_resistance - new_support) / mid > max_range_ratio:
            break

        support = new_support
        resistance = new_resistance
        consol_days += 1

    if consol_days < min_consolidation_days:
        return False, 0, 0.0, 0.0

    # Exclude last 5 bars from support/resistance so event detectors can breach
    range_start = len(bars) - consol_days
    event_window = 5
    base_end = max(range_start, len(bars) - event_window)
    if base_end > range_start:
        base_bars = bars[range_start:base_end]
        support = min(b.low for b in base_bars)
        resistance = max(b.high for b in base_bars)

    return True, consol_days, support, resistance


# ── Consolidation detection (orchestrator) ────────────────


def detect_consolidation(
    bars: list[OHLCV],
    *,
    atr_ratio_threshold: float = 0.05,
    min_consolidation_days: int = 20,
    max_range_ratio: float = 0.30,
) -> tuple[bool, int, float, float]:
    """Detect if recent price action is in a Wyckoff trading range.

    Returns (is_consolidating, consolidation_days, support, resistance).

    Strategy:
    1. Primary: find SC/AR or BC/AR structural events to anchor the range.
       Support = SC low, Resistance = AR high (accumulation), or vice versa.
    2. Fallback: ATR-ratio backward scan for data without clear climax events.
    """
    if len(bars) < min_consolidation_days + 1:
        return False, 0, 0.0, 0.0

    atr_values = compute_atr(bars)
    if len(atr_values) < min_consolidation_days:
        return False, 0, 0.0, 0.0

    vol_sma = compute_volume_sma(bars)

    # Primary: structural detection (SC/AR or BC/AR)
    result = _detect_consolidation_structural(
        bars, vol_sma, atr_values,
        min_consolidation_days=min_consolidation_days,
        max_range_ratio=max_range_ratio,
    )
    if result is not None:
        return result

    # Fallback: ATR-based range detection
    return _detect_consolidation_range(
        bars, atr_values,
        atr_ratio_threshold=atr_ratio_threshold,
        min_consolidation_days=min_consolidation_days,
        max_range_ratio=max_range_ratio,
    )


# ── Prior trend detection ────────────────────────────────


def _simple_ma(values: list[float], period: int) -> float | None:
    """Compute simple MA of the last `period` values. None if insufficient."""
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def detect_prior_trend(bars: list[OHLCV], range_start_idx: int) -> str:
    """Detect the trend before the consolidation range.

    Returns "down", "up", or "flat".
    Algorithm: compare MA20 vs MA60 of the 60 bars before range_start_idx.
    """
    # Take bars before the consolidation range
    pre_bars = bars[:range_start_idx]
    if len(pre_bars) < 60 and len(pre_bars) < 20:
        return "flat"

    closes = [b.close for b in pre_bars]

    ma20 = _simple_ma(closes, min(20, len(closes)))
    ma60 = _simple_ma(closes, min(60, len(closes)))

    if ma20 is None or ma60 is None:
        return "flat"

    diff_pct = (ma20 - ma60) / ma60 if ma60 != 0 else 0
    if diff_pct < -0.02:
        return "down"
    elif diff_pct > 0.02:
        return "up"
    return "flat"


# ── Event detectors ──────────────────────────────────────
# Each detector examines the last few bars of the consolidation range.


def detect_spring(
    bars: list[OHLCV],
    support: float,
    vol_sma: list[float],
    *,
    breach_threshold: float = 0.02,
    vol_low_ratio: float = 0.8,
    close_position_threshold: float = 0.5,
) -> bool:
    """Detect Spring: false breakdown below support with low volume and close recovery.

    Conditions (checked on the last 5 bars):
    - low < support (breach)
    - close > support (recovery)
    - volume < vol_low_ratio * avg volume
    - close position within bar > threshold (closed in upper half)
    """
    window = bars[-5:]
    if not window or not vol_sma:
        return False

    for i, bar in enumerate(window):
        idx = len(bars) - len(window) + i
        if idx >= len(vol_sma):
            continue

        breach = bar.low < support * (1 - breach_threshold)
        recovered = bar.close > support
        low_vol = bar.volume < vol_sma[idx] * vol_low_ratio

        bar_range = bar.high - bar.low
        close_pos = (bar.close - bar.low) / bar_range if bar_range > 0 else 0.5
        upper_close = close_pos > close_position_threshold

        if breach and recovered and low_vol and upper_close:
            return True

    return False


def detect_sos(
    bars: list[OHLCV],
    resistance: float,
    vol_sma: list[float],
    *,
    vol_high_ratio: float = 1.5,
) -> bool:
    """Detect Sign of Strength: breakout above resistance on high volume.

    Conditions (last 5 bars):
    - close > resistance
    - volume > vol_high_ratio * avg volume
    """
    window = bars[-5:]
    if not window or not vol_sma:
        return False

    for i, bar in enumerate(window):
        idx = len(bars) - len(window) + i
        if idx >= len(vol_sma):
            continue

        breakout = bar.close > resistance
        high_vol = bar.volume > vol_sma[idx] * vol_high_ratio

        if breakout and high_vol:
            return True

    return False


def detect_lps(
    bars: list[OHLCV],
    resistance: float,
    support: float,
    vol_sma: list[float],
    *,
    vol_low_ratio: float = 0.8,
) -> bool:
    """Detect Last Point of Support: low-volume pullback after SOS, holds above support.

    Conditions (last 3 bars):
    - There was a prior close above resistance (SOS implied)
    - Current bar pulls back but close > support
    - Volume < vol_low_ratio * avg
    """
    if len(bars) < 8:
        return False

    # Check if there was a recent breakout above resistance in the prior bars
    prior_breakout = any(b.close > resistance for b in bars[-8:-3])
    if not prior_breakout:
        return False

    window = bars[-3:]
    for i, bar in enumerate(window):
        idx = len(bars) - len(window) + i
        if idx >= len(vol_sma):
            continue

        pullback = bar.close < resistance
        holds_support = bar.close > support
        low_vol = bar.volume < vol_sma[idx] * vol_low_ratio

        if pullback and holds_support and low_vol:
            return True

    return False


def detect_ut(
    bars: list[OHLCV],
    resistance: float,
    vol_sma: list[float],
    *,
    breach_threshold: float = 0.02,
    vol_low_ratio: float = 0.8,
    close_position_threshold: float = 0.5,
) -> bool:
    """Detect Upthrust: false breakout above resistance, weak close, low volume.

    Conditions (last 5 bars):
    - high > resistance (breach)
    - close < resistance (failed breakout)
    - volume < vol_low_ratio * avg (lack of conviction)
    - close position < threshold (closed in lower half)
    """
    window = bars[-5:]
    if not window or not vol_sma:
        return False

    for i, bar in enumerate(window):
        idx = len(bars) - len(window) + i
        if idx >= len(vol_sma):
            continue

        breach = bar.high > resistance * (1 + breach_threshold)
        failed = bar.close < resistance
        low_vol = bar.volume < vol_sma[idx] * vol_low_ratio

        bar_range = bar.high - bar.low
        close_pos = (bar.close - bar.low) / bar_range if bar_range > 0 else 0.5
        lower_close = close_pos < close_position_threshold

        if breach and failed and low_vol and lower_close:
            return True

    return False


def detect_sow(
    bars: list[OHLCV],
    support: float,
    vol_sma: list[float],
    *,
    vol_high_ratio: float = 1.5,
) -> bool:
    """Detect Sign of Weakness: breakdown below support on high volume.

    Conditions (last 5 bars):
    - close < support
    - volume > vol_high_ratio * avg volume
    """
    window = bars[-5:]
    if not window or not vol_sma:
        return False

    for i, bar in enumerate(window):
        idx = len(bars) - len(window) + i
        if idx >= len(vol_sma):
            continue

        breakdown = bar.close < support
        high_vol = bar.volume > vol_sma[idx] * vol_high_ratio

        if breakdown and high_vol:
            return True

    return False


def detect_lpsy(
    bars: list[OHLCV],
    resistance: float,
    support: float,
    vol_sma: list[float],
    *,
    vol_low_ratio: float = 0.8,
) -> bool:
    """Detect Last Point of Supply: low-volume rally failure after SOW.

    Conditions (last 3 bars):
    - There was a prior close below support (SOW implied)
    - Current bar rallies but close < resistance
    - Volume < vol_low_ratio * avg
    """
    if len(bars) < 8:
        return False

    # Check if there was a recent breakdown below support
    prior_breakdown = any(b.close < support for b in bars[-8:-3])
    if not prior_breakdown:
        return False

    window = bars[-3:]
    for i, bar in enumerate(window):
        idx = len(bars) - len(window) + i
        if idx >= len(vol_sma):
            continue

        rally_fail = bar.close < resistance
        above_support = bar.close > support
        low_vol = bar.volume < vol_sma[idx] * vol_low_ratio

        if rally_fail and above_support and low_vol:
            return True

    return False


# ── Main entry point ─────────────────────────────────────


def _lerp(a: float, b: float, t: float) -> float:
    """Linear interpolation from a to b by t (clamped to [0, 1])."""
    t = max(0.0, min(1.0, t))
    return a + (b - a) * t


_TREND_LABELS = {"down": "下跌", "up": "上涨", "flat": "横盘"}
_PHASE_LABELS = {
    WyckoffPhase.ACCUMULATION: "威科夫吸筹阶段",
    WyckoffPhase.DISTRIBUTION: "威科夫派发阶段",
}


def _build_result(
    phase: WyckoffPhase,
    confidence: float,
    events: list[WyckoffEvent],
    multiplier: float,
    consol_days: int,
    prior_trend: str,
    support: float,
    resistance: float,
) -> WyckoffResult:
    """Build a WyckoffResult with a human-readable detail string."""
    parts = [
        f"盘整{consol_days}日",
        f"前趋势:{_TREND_LABELS.get(prior_trend, prior_trend)}",
        f"支撑:{support:.2f}",
        f"阻力:{resistance:.2f}",
    ]
    if events:
        parts.append(f"事件:{','.join(e.value for e in events)}")
    parts.append(f"置信度:{confidence:.0%}")

    label = _PHASE_LABELS.get(phase, str(phase.value))
    return WyckoffResult(
        phase=phase,
        confidence=confidence,
        detected_events=events,
        multiplier=multiplier,
        detail=f"{label} — " + "，".join(parts),
        support=support,
        resistance=resistance,
    )


def detect_phase(bars: list[OHLCV], config: WyckoffConfig | None = None) -> WyckoffResult:
    """Detect Wyckoff phase from OHLCV bars.

    Main orchestrator: consolidation detection → prior trend → event detection → phase + multiplier.
    """
    if config is None:
        config = WyckoffConfig()

    unknown = WyckoffResult(
        phase=WyckoffPhase.UNKNOWN,
        confidence=0.0,
        multiplier=1.0,
        detail="数据不足或未处于盘整阶段",
    )

    # 1. Insufficient data
    if len(bars) < config.min_bars:
        return unknown

    # 2. Consolidation detection
    is_consol, consol_days, support, resistance = detect_consolidation(
        bars,
        atr_ratio_threshold=config.atr_ratio_threshold,
        min_consolidation_days=config.min_consolidation_days,
        max_range_ratio=config.consolidation_max_range,
    )
    if not is_consol:
        return unknown

    # 3. Prior trend
    range_start = len(bars) - consol_days
    prior_trend = detect_prior_trend(bars, range_start)

    # 4. Volume SMA for event detection
    vol_sma = compute_volume_sma(bars)

    # Common kwargs for event detectors
    breach_kw = {
        "breach_threshold": config.breach_threshold,
        "vol_low_ratio": config.vol_low_ratio,
        "close_position_threshold": config.close_position_threshold,
    }

    # ── Check BOTH accumulation and distribution events ──
    # Events themselves determine the phase; prior trend is a tiebreaker.
    acc_events: list[WyckoffEvent] = []
    acc_conf = 0.0

    if detect_spring(bars, support, vol_sma, **breach_kw):
        acc_events.append(WyckoffEvent.SPRING)
        acc_conf += config.spring_weight
    if detect_sos(bars, resistance, vol_sma, vol_high_ratio=config.vol_high_ratio):
        acc_events.append(WyckoffEvent.SOS)
        acc_conf += config.sos_weight
    if detect_lps(bars, resistance, support, vol_sma, vol_low_ratio=config.vol_low_ratio):
        acc_events.append(WyckoffEvent.LPS)
        acc_conf += config.lps_weight
    acc_conf = min(acc_conf, 1.0)

    dist_events: list[WyckoffEvent] = []
    dist_conf = 0.0

    if detect_ut(bars, resistance, vol_sma, **breach_kw):
        dist_events.append(WyckoffEvent.UT)
        dist_conf += config.ut_weight
    if detect_sow(bars, support, vol_sma, vol_high_ratio=config.vol_high_ratio):
        dist_events.append(WyckoffEvent.SOW)
        dist_conf += config.sow_weight
    if detect_lpsy(bars, resistance, support, vol_sma, vol_low_ratio=config.vol_low_ratio):
        dist_events.append(WyckoffEvent.LPSY)
        dist_conf += config.lpsy_weight
    dist_conf = min(dist_conf, 1.0)

    # ── Decide phase: events win, prior trend is tiebreaker ──
    if acc_conf == 0 and dist_conf == 0:
        # No events detected; use prior trend as weak hint
        if prior_trend == "down":
            return _build_result(
                WyckoffPhase.ACCUMULATION, 0.0, [], 1.0,
                consol_days, prior_trend, support, resistance,
            )
        elif prior_trend == "up":
            return _build_result(
                WyckoffPhase.DISTRIBUTION, 0.0, [], 1.0,
                consol_days, prior_trend, support, resistance,
            )
        return unknown

    if acc_conf > dist_conf:
        multiplier = _lerp(1.0, config.acc_max_multiplier, acc_conf)
        return _build_result(
            WyckoffPhase.ACCUMULATION, acc_conf, acc_events, round(multiplier, 4),
            consol_days, prior_trend, support, resistance,
        )

    if dist_conf > acc_conf:
        if WyckoffEvent.LPSY in dist_events:
            multiplier = config.markdown_multiplier
        else:
            multiplier = _lerp(1.0, config.dist_min_multiplier, dist_conf)
        return _build_result(
            WyckoffPhase.DISTRIBUTION, dist_conf, dist_events, round(multiplier, 4),
            consol_days, prior_trend, support, resistance,
        )

    # Tied confidence → prior trend breaks the tie
    if prior_trend == "down":
        multiplier = _lerp(1.0, config.acc_max_multiplier, acc_conf)
        return _build_result(
            WyckoffPhase.ACCUMULATION, acc_conf, acc_events, round(multiplier, 4),
            consol_days, prior_trend, support, resistance,
        )
    if prior_trend == "up":
        if WyckoffEvent.LPSY in dist_events:
            multiplier = config.markdown_multiplier
        else:
            multiplier = _lerp(1.0, config.dist_min_multiplier, dist_conf)
        return _build_result(
            WyckoffPhase.DISTRIBUTION, dist_conf, dist_events, round(multiplier, 4),
            consol_days, prior_trend, support, resistance,
        )

    # Flat trend + tied → no clear signal
    return unknown
