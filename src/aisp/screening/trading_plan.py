"""Quantitative trading plan generation — pure functions, no DB dependency.

Runs after Wyckoff + breakout in the scoring pipeline. Synthesizes existing
price data (support/resistance, MAs, ATR) into a structured trading plan
with entry zone, stop loss, targets, and risk/reward ratio.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aisp.config import TradingPlanConfig
from aisp.screening.wyckoff import OHLCV, compute_atr


@dataclass
class PriceLimits:
    up_limit: float
    down_limit: float
    limit_pct: float


@dataclass
class TradingPlan:
    entry_zone: tuple[float, float] | None  # (low, high); None when limit up/down
    stop_loss: float | None
    targets: list[float] = field(default_factory=list)  # short-term (1-5 days)
    risk_reward: float = 0.0
    mid_targets: list[float] = field(default_factory=list)  # mid-term (1-3 months)
    mid_risk_reward: float = 0.0
    price_limits: PriceLimits = field(default_factory=lambda: PriceLimits(0, 0, 0))
    position_hint: str = "normal"  # aggressive / normal / conservative
    t1_note: str = ""
    rationale: str = ""


# ── Price limit rules ────────────────────────────────────


def compute_price_limits(code: str, close: float, *, is_st: bool = False) -> PriceLimits:
    """Compute A-share daily price limits based on board type and ST status."""
    if is_st:
        pct = 0.05
    elif code.startswith(("300", "301", "688", "689")):
        pct = 0.20
    else:
        pct = 0.10

    return PriceLimits(
        up_limit=round(close * (1 + pct), 2),
        down_limit=round(close * (1 - pct), 2),
        limit_pct=pct,
    )


# ── Key level collection ─────────────────────────────────


@dataclass
class _KeyLevel:
    price: float
    source: str
    position: str  # "above" / "below" / "at"


def _collect_key_levels(
    close: float,
    bars: list[OHLCV],
    wyckoff_data: dict | None,
    breakout_data: list[dict] | None,
) -> tuple[list[_KeyLevel], float]:
    """Collect key price levels and compute ATR.

    Returns (levels, atr). ATR falls back to 3% of close if data is insufficient.
    """
    levels: list[_KeyLevel] = []

    # Wyckoff support/resistance
    if wyckoff_data:
        sup = wyckoff_data.get("support")
        res = wyckoff_data.get("resistance")
        if sup is not None:
            levels.append(_KeyLevel(sup, "wyckoff_support", _pos(sup, close)))
        if res is not None:
            levels.append(_KeyLevel(res, "wyckoff_resistance", _pos(res, close)))

    # Breakout levels
    if breakout_data:
        for b in breakout_data:
            lvl = b.get("level")
            if lvl is not None:
                levels.append(_KeyLevel(lvl, f"breakout_{b.get('signal_type', '')}", _pos(lvl, close)))

    # MAs from bars
    closes = [b.close for b in bars]
    for period in (5, 10, 20, 60):
        ma = _simple_ma(closes, period)
        if ma is not None:
            levels.append(_KeyLevel(round(ma, 2), f"ma{period}", _pos(ma, close)))

    # ATR
    atr_values = compute_atr(bars, period=20)
    atr = atr_values[-1] if atr_values else close * 0.03

    # Yesterday's high/low
    if len(bars) >= 2:
        prev = bars[-2]
        levels.append(_KeyLevel(prev.high, "prev_high", _pos(prev.high, close)))
        levels.append(_KeyLevel(prev.low, "prev_low", _pos(prev.low, close)))

    # 20-day / 60-day / 120-day high/low
    for period in (20, 60, 120):
        if len(bars) >= period:
            window = bars[-period:]
            high_n = max(b.high for b in window)
            low_n = min(b.low for b in window)
            levels.append(_KeyLevel(high_n, f"high_{period}d", _pos(high_n, close)))
            levels.append(_KeyLevel(low_n, f"low_{period}d", _pos(low_n, close)))

    return levels, atr


def _pos(price: float, close: float) -> str:
    if abs(price - close) / close < 0.001:
        return "at"
    return "above" if price > close else "below"


def _simple_ma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


# ── Core plan computation ────────────────────────────────


def compute_trading_plan(
    code: str,
    closes: list[float],
    bars: list[OHLCV],
    *,
    wyckoff_data: dict | None = None,
    breakout_data: list[dict] | None = None,
    is_st: bool = False,
    is_limit_up: bool = False,
    is_limit_down: bool = False,
    direction: str = "hold",
    config: TradingPlanConfig | None = None,
) -> TradingPlan | None:
    """Compute a structured trading plan from existing price data.

    Returns TradingPlan or None if insufficient data.
    """
    if not closes or not bars:
        return None

    if config is None:
        config = TradingPlanConfig()

    close = closes[-1]
    limits = compute_price_limits(code, close, is_st=is_st)

    # Limit up/down → no actionable plan
    if is_limit_up:
        return TradingPlan(
            entry_zone=None,
            stop_loss=None,
            price_limits=limits,
            rationale="涨停封板，不建议追高，等待次日确认",
            t1_note="T+1 规则：即使次日开板也无法当日卖出",
        )
    if is_limit_down:
        return TradingPlan(
            entry_zone=None,
            stop_loss=None,
            price_limits=limits,
            rationale="跌停封板，流动性极差，不建议接盘",
            t1_note="T+1 规则：当日买入次日才能卖出，隔夜风险极大",
        )

    levels, atr = _collect_key_levels(close, bars, wyckoff_data, breakout_data)

    # Separate levels into supports (below) and resistances (above)
    supports = sorted(
        [lv for lv in levels if lv.position == "below"],
        key=lambda x: x.price,
        reverse=True,  # nearest first
    )
    resistances = sorted(
        [lv for lv in levels if lv.position == "above"],
        key=lambda x: x.price,  # nearest first
    )

    if direction in ("buy", "strong_buy", "weak_buy"):
        return _plan_bullish(close, atr, supports, resistances, limits, config, is_st)
    elif direction in ("sell", "strong_sell", "weak_sell"):
        return _plan_bearish(close, atr, supports, resistances, limits, config, is_st)
    else:
        return _plan_neutral(close, atr, supports, resistances, limits, config, is_st)


def _plan_bullish(
    close: float,
    atr: float,
    supports: list[_KeyLevel],
    resistances: list[_KeyLevel],
    limits: PriceLimits,
    config: TradingPlanConfig,
    is_st: bool,
) -> TradingPlan:
    """Generate plan for bullish direction."""
    # Entry zone: nearest support below → close (or slightly above)
    entry_low = supports[0].price if supports else close - atr * 0.5
    entry_high = close

    # Clamp entry zone width
    max_width = config.entry_zone_max_atr_width * atr
    if entry_high - entry_low > max_width:
        entry_low = entry_high - max_width
    entry_low = max(entry_low, limits.down_limit)
    entry_low = round(entry_low, 2)
    entry_high = round(entry_high, 2)

    # Stop loss
    atr_mult = config.st_atr_multiplier if is_st else config.normal_atr_multiplier
    stop_from_support = entry_low * (1 - config.stop_buffer_pct)
    stop_from_atr = entry_low - atr * atr_mult
    stop_loss = max(stop_from_support, stop_from_atr, limits.down_limit)
    stop_loss = round(stop_loss, 2)

    # Short-term targets: first two resistance levels above close, capped at limit
    targets = []
    for r in resistances[:2]:
        t = min(r.price, limits.up_limit)
        targets.append(round(t, 2))
    # Fallback: ATR-based targets
    if len(targets) == 0:
        targets.append(round(min(close + 1.5 * atr, limits.up_limit), 2))
    if len(targets) == 1:
        targets.append(round(min(close + 2.5 * atr, limits.up_limit), 2))
    targets = _dedup_targets(targets)

    # Mid-term targets: longer-period levels (120d high, 60d high, MA60) + ATR fallback
    mid_targets = _compute_mid_targets(close, atr, resistances)

    # Ensure stop loss has minimum distance from entry (avoid extreme R:R)
    entry_mid = (entry_low + entry_high) / 2
    min_risk = close * 0.015  # at least 1.5% risk
    if entry_mid - stop_loss < min_risk:
        stop_loss = round(max(entry_mid - min_risk, limits.down_limit), 2)

    # Risk/reward (short-term)
    risk = entry_mid - stop_loss
    reward = targets[0] - entry_mid
    rr = _clamp_rr(round(reward / risk, 1) if risk > 0 else 0.0)

    # Mid-term risk/reward
    mid_reward = mid_targets[0] - entry_mid if mid_targets else 0.0
    mid_rr = _clamp_rr(round(mid_reward / risk, 1) if risk > 0 and mid_reward > 0 else 0.0)

    # Position hint
    position_hint = _position_hint(rr)

    # T+1 note
    t1_note = "T+1 提醒：当日买入次日才能卖出，需承受隔夜风险"

    # Rationale
    support_src = supports[0].source if supports else "ATR估算"
    target_src = resistances[0].source if resistances else "ATR估算"
    rationale = (
        f"入场区间基于{support_src}支撑，"
        f"目标位参考{target_src}，"
        f"风险收益比 {rr:.1f}"
    )

    return TradingPlan(
        entry_zone=(entry_low, entry_high),
        stop_loss=stop_loss,
        targets=targets,
        risk_reward=rr,
        mid_targets=mid_targets,
        mid_risk_reward=mid_rr,
        price_limits=limits,
        position_hint=position_hint,
        t1_note=t1_note,
        rationale=rationale,
    )


def _plan_bearish(
    close: float,
    atr: float,
    supports: list[_KeyLevel],
    resistances: list[_KeyLevel],
    limits: PriceLimits,
    config: TradingPlanConfig,
    is_st: bool,
) -> TradingPlan:
    """Generate plan for bearish direction (short-side warning for A-shares)."""
    # For A-shares (no short selling for retail), bearish = avoid / reduce
    # Still compute levels for reference

    # "Entry" for existing holders considering exit
    exit_high = resistances[0].price if resistances else close + atr * 0.5
    exit_low = close

    exit_high = min(round(exit_high, 2), limits.up_limit)
    exit_low = round(exit_low, 2)

    # Support targets (where price may go)
    targets = []
    for s in supports[:2]:
        t = max(s.price, limits.down_limit)
        targets.append(round(t, 2))
    if not targets:
        targets.append(round(max(close - 1.5 * atr, limits.down_limit), 2))

    # Stop loss (for existing holders: cut above resistance to avoid further loss)
    stop_loss = round(max(close * (1 - config.stop_buffer_pct * 3), limits.down_limit), 2)

    return TradingPlan(
        entry_zone=(exit_low, exit_high),
        stop_loss=stop_loss,
        targets=targets,
        risk_reward=0.0,
        price_limits=limits,
        position_hint="conservative",
        t1_note="T+1 提醒：A股无法做空，看空信号建议观望或减仓",
        rationale="看空信号，建议观望或逢高减仓",
    )


def _plan_neutral(
    close: float,
    atr: float,
    supports: list[_KeyLevel],
    resistances: list[_KeyLevel],
    limits: PriceLimits,
    config: TradingPlanConfig,
    is_st: bool,
) -> TradingPlan:
    """Generate plan for neutral/hold direction."""
    entry_low = supports[0].price if supports else close - atr * 0.5
    entry_high = close

    max_width = config.entry_zone_max_atr_width * atr
    if entry_high - entry_low > max_width:
        entry_low = entry_high - max_width
    entry_low = max(round(entry_low, 2), limits.down_limit)
    entry_high = round(entry_high, 2)

    atr_mult = config.st_atr_multiplier if is_st else config.normal_atr_multiplier
    stop_loss = round(max(entry_low - atr * atr_mult, limits.down_limit), 2)

    targets = []
    for r in resistances[:2]:
        targets.append(round(min(r.price, limits.up_limit), 2))
    if not targets:
        targets.append(round(min(close + 1.5 * atr, limits.up_limit), 2))
    targets = _dedup_targets(targets)

    mid_targets = _compute_mid_targets(close, atr, resistances)

    entry_mid = (entry_low + entry_high) / 2
    min_risk = close * 0.015
    if entry_mid - stop_loss < min_risk:
        stop_loss = round(max(entry_mid - min_risk, limits.down_limit), 2)

    risk = entry_mid - stop_loss
    reward = targets[0] - entry_mid
    rr = _clamp_rr(round(reward / risk, 1) if risk > 0 else 0.0)

    mid_reward = mid_targets[0] - entry_mid if mid_targets else 0.0
    mid_rr = _clamp_rr(round(mid_reward / risk, 1) if risk > 0 and mid_reward > 0 else 0.0)

    return TradingPlan(
        entry_zone=(entry_low, entry_high),
        stop_loss=stop_loss,
        targets=targets,
        risk_reward=rr,
        mid_targets=mid_targets,
        mid_risk_reward=mid_rr,
        price_limits=limits,
        position_hint=_position_hint(rr),
        t1_note="T+1 提醒：方向不明确，轻仓或观望为宜",
        rationale=f"方向中性，风险收益比 {rr:.1f}，建议等待明确信号",
    )


# ── Helpers ──────────────────────────────────────────────


def _clamp_rr(rr: float) -> float:
    """Clamp risk/reward ratio to a reasonable range [0.0, 10.0]."""
    return max(0.0, min(rr, 10.0))


def _position_hint(rr: float) -> str:
    if rr >= 3.0:
        return "aggressive"
    if rr >= 1.5:
        return "normal"
    return "conservative"


def _compute_mid_targets(
    close: float, atr: float, resistances: list[_KeyLevel],
) -> list[float]:
    """Compute mid-term targets (1-3 month horizon).

    Prefers long-period levels (120d/60d highs, MA60) over short-period ones.
    Falls back to ATR-based estimates (5x/8x ATR) when no long-period levels exist.
    """
    mid_sources = {"high_120d", "high_60d", "ma60"}
    # Longer-period resistance levels, sorted by price
    mid_levels = sorted(
        [r for r in resistances if r.source in mid_sources and r.price > close * 1.03],
        key=lambda x: x.price,
    )

    targets = []
    for lv in mid_levels[:2]:
        targets.append(round(lv.price, 2))

    # Fallback: ATR-based mid-term targets
    if len(targets) == 0:
        targets.append(round(close + 5.0 * atr, 2))
    if len(targets) == 1:
        targets.append(round(close + 8.0 * atr, 2))

    return _dedup_targets(targets)


def _dedup_targets(targets: list[float]) -> list[float]:
    """Remove duplicate targets, keep order."""
    seen: set[float] = set()
    result: list[float] = []
    for t in targets:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def trading_plan_to_dict(plan: TradingPlan) -> dict:
    """Convert TradingPlan to a plain dict for storage in raw_data/factor_scores."""
    return {
        "entry_zone": list(plan.entry_zone) if plan.entry_zone else None,
        "stop_loss": plan.stop_loss,
        "targets": plan.targets,
        "risk_reward": plan.risk_reward,
        "mid_targets": plan.mid_targets,
        "mid_risk_reward": plan.mid_risk_reward,
        "price_limits": {
            "up": plan.price_limits.up_limit,
            "down": plan.price_limits.down_limit,
            "pct": plan.price_limits.limit_pct,
        },
        "position_hint": plan.position_hint,
        "t1_note": plan.t1_note,
        "rationale": plan.rationale,
    }
