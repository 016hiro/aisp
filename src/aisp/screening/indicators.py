"""Technical indicators — pure functions, no DB dependency."""

from __future__ import annotations

# Default RSI periods (short-term multi-period)
DEFAULT_RSI_PERIODS: list[int] = [3, 6, 9]


def compute_rsi(closes: list[float], period: int = 6) -> float | None:
    """Compute RSI using Wilder smoothing. Returns 0-100 or None if insufficient data."""
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]

    # Initial averages from first `period` deltas
    gains = [max(d, 0) for d in deltas[:period]]
    losses = [max(-d, 0) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Wilder smoothing for remaining deltas
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def compute_rsi_multi(
    closes: list[float], periods: list[int] | None = None
) -> dict[int, float | None]:
    """Compute RSI for multiple periods. Returns {period: rsi_value}."""
    periods = periods or DEFAULT_RSI_PERIODS
    return {p: compute_rsi(closes, period=p) for p in periods}


def compute_macd(
    closes: list[float], fast: int = 6, slow: int = 13, signal: int = 5
) -> dict[str, float] | None:
    """Compute MACD line, signal line, and histogram. Returns None if insufficient data."""
    if len(closes) < slow + signal:
        return None

    def _ema(data: list[float], span: int) -> list[float]:
        multiplier = 2.0 / (span + 1)
        result = [data[0]]
        for val in data[1:]:
            result.append(val * multiplier + result[-1] * (1 - multiplier))
        return result

    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)

    macd_line = [f - s for f, s in zip(ema_fast, ema_slow, strict=False)]
    signal_line = _ema(macd_line[slow - 1 :], signal)

    return {
        "macd": macd_line[-1],
        "signal": signal_line[-1],
        "histogram": macd_line[-1] - signal_line[-1],
    }


def compute_ma_position(close: float, ma_values: dict[int, float | None]) -> float:
    """Weighted proportion of MAs that price is above. Returns 0-1.

    Weights: MA5=0.15, MA10=0.25, MA20=0.30, MA60=0.30.
    """
    weights = {5: 0.15, 10: 0.25, 20: 0.30, 60: 0.30}
    total_weight = 0.0
    score = 0.0

    for period, w in weights.items():
        ma = ma_values.get(period)
        if ma is not None:
            total_weight += w
            if close >= ma:
                score += w

    return score / total_weight if total_weight > 0 else 0.5


def compute_technical_score(
    closes: list[float], ma_values: dict[int, float | None]
) -> float:
    """Composite technical score: RSI(0.3) + MACD direction(0.3) + MA position(0.4).

    RSI: [30, 70] linearly mapped to [0, 1]. Outside range: clipped.
    MACD: histogram > 0 -> 1.0, < 0 -> 0.0, == 0 -> 0.5.
    Insufficient data defaults to 0.5 per component.
    """
    # RSI component (use middle period=6)
    rsi = compute_rsi(closes)
    rsi_norm = max(0.0, min(1.0, (rsi - 30.0) / 40.0)) if rsi is not None else 0.5

    # MACD component
    macd = compute_macd(closes)
    if macd is not None:
        hist = macd["histogram"]
        macd_score = 1.0 if hist > 0 else (0.0 if hist < 0 else 0.5)
    else:
        macd_score = 0.5

    # MA position component
    ma_score = compute_ma_position(closes[-1], ma_values) if closes else 0.5

    return rsi_norm * 0.3 + macd_score * 0.3 + ma_score * 0.4
