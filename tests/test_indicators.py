"""Unit tests for technical indicators."""

from __future__ import annotations

from aisp.screening.indicators import (
    compute_ma_position,
    compute_macd,
    compute_rsi,
    compute_rsi_multi,
    compute_technical_score,
)


def test_rsi_known_sequence():
    """RSI on a known up-trending sequence should be > 50."""
    closes = [44.0 + i * 0.5 for i in range(20)]  # steadily rising
    rsi = compute_rsi(closes, period=14)
    assert rsi is not None
    assert rsi > 70  # strong uptrend

    # Steadily falling
    closes_down = [60.0 - i * 0.5 for i in range(20)]
    rsi_down = compute_rsi(closes_down, period=14)
    assert rsi_down is not None
    assert rsi_down < 30


def test_rsi_insufficient_data():
    assert compute_rsi([10, 20, 30], period=14) is None


def test_rsi_all_gains():
    closes = list(range(1, 20))
    rsi = compute_rsi(closes)
    assert rsi == 100.0


def test_rsi_multi():
    closes = [44.0 + i * 0.5 for i in range(15)]
    result = compute_rsi_multi(closes, periods=[3, 6, 9])
    assert 3 in result and 6 in result and 9 in result
    # All uptrend → all RSI should be > 50
    for p, v in result.items():
        assert v is not None, f"RSI({p}) should not be None with {len(closes)} points"
        assert v > 50


def test_macd_known_sequence():
    # Need at least slow + signal = 18 data points (default 6/13/5)
    closes = [100.0 + i * 0.3 for i in range(25)]
    result = compute_macd(closes)
    assert result is not None
    assert "macd" in result
    assert "signal" in result
    assert "histogram" in result
    # In an uptrend, MACD should be positive
    assert result["macd"] > 0


def test_macd_insufficient_data():
    assert compute_macd([10, 20, 30]) is None


def test_ma_position():
    # Price above all MAs
    score = compute_ma_position(100.0, {5: 95, 10: 90, 20: 85, 60: 80})
    assert score == 1.0

    # Price below all MAs
    score = compute_ma_position(70.0, {5: 95, 10: 90, 20: 85, 60: 80})
    assert score == 0.0

    # Price above MA60 only
    score = compute_ma_position(82.0, {5: 95, 10: 90, 20: 85, 60: 80})
    assert abs(score - 0.30) < 0.01  # only MA60 weight

    # Empty MA values
    assert compute_ma_position(100.0, {}) == 0.5


def test_technical_score_defaults():
    """With insufficient data, should return ~0.5."""
    score = compute_technical_score([], {})
    assert abs(score - 0.5) < 0.01


def test_technical_score_strong_uptrend():
    closes = [50.0 + i * 0.5 for i in range(40)]
    ma_values = {5: 68, 10: 65, 20: 60, 60: 50}
    score = compute_technical_score(closes, ma_values)
    # RSI high + MACD positive + above all MAs → score should be high
    assert score > 0.7
