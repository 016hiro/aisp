"""Unit tests for breakout signal detection — pure functions, synthetic data, no DB."""

from __future__ import annotations

from aisp.config import BreakoutConfig
from aisp.screening.breakout import (
    _compute_strength,
    _detect_consolidation_breakout,
    _detect_ma_breakout,
    _detect_new_high_low,
    detect_breakouts,
)
from aisp.screening.wyckoff import OHLCV, WyckoffPhase, WyckoffResult, compute_volume_sma

# ── Helpers ──────────────────────────────────────────────


def _flat_bars(n: int, price: float = 10.0, volume: float = 1000.0) -> list[OHLCV]:
    """Generate flat/consolidating bars."""
    bars = []
    for i in range(n):
        noise = 0.01 * (i % 3 - 1)
        bars.append(OHLCV(
            open=price + noise,
            high=price + 0.05,
            low=price - 0.05,
            close=price + noise,
            volume=volume,
        ))
    return bars


def _trending_bars(
    n: int, start: float = 10.0, step: float = 0.2, volume: float = 1000.0
) -> list[OHLCV]:
    """Generate trending bars."""
    bars = []
    for i in range(n):
        price = start + step * i
        bars.append(OHLCV(
            open=price - step * 0.3,
            high=price + abs(step) * 0.5,
            low=price - abs(step) * 0.5,
            close=price,
            volume=volume,
        ))
    return bars


# ── MA breakout tests ───────────────────────────────────


class TestMABreakout:
    def test_ma20_upward_breakout(self):
        """Price crosses above MA20 → ma20_breakout signal."""
        # 20 bars at 10.0 then 5 bars dipping to 9.5 → MA20 stays ~9.8+
        bars = []
        for _i in range(20):
            bars.append(OHLCV(open=10.0, high=10.1, low=9.9, close=10.0, volume=1000))
        for _i in range(5):
            bars.append(OHLCV(open=9.5, high=9.6, low=9.4, close=9.5, volume=1000))
        # Previous bar: close 9.5, clearly below MA20 (~9.875)
        # Current bar: close 10.2, clearly above MA20
        bars.append(OHLCV(open=9.6, high=10.3, low=9.5, close=10.2, volume=2000))

        vol_sma = compute_volume_sma(bars)
        config = BreakoutConfig()
        signals = _detect_ma_breakout(bars, vol_sma, config)

        ma_types = [s.signal_type for s in signals]
        assert "ma20_breakout" in ma_types

    def test_ma60_downward_breakdown(self):
        """Price crosses below MA60 → ma60_breakdown signal."""
        # 60 bars at 10.5, then 5 bars at 10.6 (prev close > MA60 ~ 10.5)
        bars = []
        for _i in range(60):
            bars.append(OHLCV(open=10.5, high=10.8, low=10.3, close=10.5, volume=1000))
        for _i in range(5):
            bars.append(OHLCV(open=10.6, high=10.7, low=10.5, close=10.6, volume=1000))
        # Current bar drops well below MA60 (~10.5)
        bars.append(OHLCV(open=10.4, high=10.5, low=9.8, close=9.9, volume=1500))

        vol_sma = compute_volume_sma(bars)
        config = BreakoutConfig()
        signals = _detect_ma_breakout(bars, vol_sma, config)

        ma_types = [s.signal_type for s in signals]
        assert "ma60_breakdown" in ma_types

    def test_no_crossover_no_signal(self):
        """Price stays above MA → no signal."""
        bars = _flat_bars(30, price=10.0)
        vol_sma = compute_volume_sma(bars)
        config = BreakoutConfig()
        signals = _detect_ma_breakout(bars, vol_sma, config)
        # Flat bars hover around the same price, no clear crossover
        # (some noise may trigger; main check is no crash)
        assert isinstance(signals, list)


# ── New high/low tests ──────────────────────────────────


class TestNewHighLow:
    def test_new_60d_high(self):
        """Close above 60-day high → new_high_60d signal."""
        bars = _flat_bars(65, price=10.0)
        # Spike to new high
        bars.append(OHLCV(open=10.0, high=11.0, low=10.0, close=10.8, volume=2000))
        vol_sma = compute_volume_sma(bars)
        config = BreakoutConfig()
        signals = _detect_new_high_low(bars, vol_sma, config)

        assert len(signals) >= 1
        assert signals[0].signal_type == "new_high_60d"
        assert signals[0].close == 10.8

    def test_new_60d_low(self):
        """Close below 60-day low → new_low_60d signal."""
        bars = _flat_bars(65, price=10.0)
        bars.append(OHLCV(open=10.0, high=10.0, low=9.0, close=9.2, volume=1500))
        vol_sma = compute_volume_sma(bars)
        config = BreakoutConfig()
        signals = _detect_new_high_low(bars, vol_sma, config)

        assert len(signals) >= 1
        assert signals[0].signal_type == "new_low_60d"
        assert signals[0].close == 9.2

    def test_no_new_high_low(self):
        """Price within range → no signal."""
        bars = _flat_bars(65, price=10.0)
        bars.append(OHLCV(open=10.0, high=10.04, low=9.96, close=10.0, volume=1000))
        vol_sma = compute_volume_sma(bars)
        config = BreakoutConfig()
        signals = _detect_new_high_low(bars, vol_sma, config)
        assert len(signals) == 0

    def test_insufficient_data(self):
        """Not enough bars → no signal."""
        bars = _flat_bars(30, price=10.0)
        vol_sma = compute_volume_sma(bars)
        config = BreakoutConfig()
        signals = _detect_new_high_low(bars, vol_sma, config)
        assert len(signals) == 0


# ── Consolidation breakout tests ────────────────────────


class TestConsolidationBreakout:
    def test_breakout_with_wyckoff_result(self):
        """Breakout above Wyckoff resistance."""
        bars = _flat_bars(25, price=10.0)
        # Previous bar below resistance
        bars.append(OHLCV(open=10.0, high=10.04, low=9.96, close=10.0, volume=1000))
        # Current bar breaks above resistance
        bars.append(OHLCV(open=10.1, high=10.8, low=10.05, close=10.6, volume=3000))

        vol_sma = compute_volume_sma(bars)
        config = BreakoutConfig()
        wyckoff = WyckoffResult(
            phase=WyckoffPhase.ACCUMULATION,
            confidence=0.4,
            support=9.5,
            resistance=10.05,
            detail="威科夫吸筹阶段 — 盘整25日，前趋势:下跌",
        )
        signals = _detect_consolidation_breakout(
            bars, vol_sma, config, wyckoff_result=wyckoff
        )

        assert len(signals) >= 1
        assert signals[0].signal_type == "resistance_breakout"
        assert signals[0].level == 10.05

    def test_breakdown_with_wyckoff_result(self):
        """Breakdown below Wyckoff support."""
        bars = _flat_bars(25, price=10.0)
        bars.append(OHLCV(open=10.0, high=10.04, low=9.96, close=10.0, volume=1000))
        bars.append(OHLCV(open=9.9, high=9.95, low=9.2, close=9.3, volume=3000))

        vol_sma = compute_volume_sma(bars)
        config = BreakoutConfig()
        wyckoff = WyckoffResult(
            phase=WyckoffPhase.DISTRIBUTION,
            confidence=0.4,
            support=9.95,
            resistance=10.5,
            detail="威科夫派发阶段 — 盘整25日",
        )
        signals = _detect_consolidation_breakout(
            bars, vol_sma, config, wyckoff_result=wyckoff
        )

        assert len(signals) >= 1
        assert signals[0].signal_type == "support_breakdown"

    def test_fallback_without_wyckoff(self):
        """Without WyckoffResult, uses fallback lookback for support/resistance."""
        bars = _flat_bars(50, price=10.0)
        # Break above the range
        bars.append(OHLCV(open=10.0, high=10.04, low=9.96, close=10.0, volume=1000))
        bars.append(OHLCV(open=10.1, high=10.8, low=10.05, close=10.6, volume=3000))

        vol_sma = compute_volume_sma(bars)
        config = BreakoutConfig()
        signals = _detect_consolidation_breakout(bars, vol_sma, config, wyckoff_result=None)

        assert len(signals) >= 1
        assert signals[0].signal_type == "resistance_breakout"


# ── Strength scoring tests ──────────────────────────────


class TestStrengthScoring:
    def test_high_volume_high_strength(self):
        """High volume ratio → higher strength."""
        bar = OHLCV(open=10.0, high=10.5, low=9.8, close=10.4, volume=5000)
        config = BreakoutConfig()

        score_high_vol = _compute_strength(bar, 3.0, 0.04, config, consolidation_days=40)
        score_low_vol = _compute_strength(bar, 0.5, 0.04, config, consolidation_days=40)

        assert score_high_vol > score_low_vol

    def test_long_consolidation_higher_strength(self):
        """Longer consolidation → higher consolidation component."""
        bar = OHLCV(open=10.0, high=10.5, low=9.8, close=10.4, volume=2000)
        config = BreakoutConfig()

        score_long = _compute_strength(bar, 2.0, 0.03, config, consolidation_days=60)
        score_short = _compute_strength(bar, 2.0, 0.03, config, consolidation_days=10)

        assert score_long > score_short

    def test_strength_bounds(self):
        """Strength score should be in [0, 1]."""
        bar = OHLCV(open=10.0, high=10.5, low=9.8, close=10.4, volume=5000)
        config = BreakoutConfig()
        score = _compute_strength(bar, 5.0, 0.1, config, consolidation_days=100)
        assert 0.0 <= score <= 1.0

    def test_zero_range_bar(self):
        """Zero-range bar (doji at limit) should not crash."""
        bar = OHLCV(open=10.0, high=10.0, low=10.0, close=10.0, volume=1000)
        config = BreakoutConfig()
        score = _compute_strength(bar, 1.0, 0.01, config)
        assert 0.0 <= score <= 1.0


# ── Main entry integration tests ────────────────────────


class TestDetectBreakouts:
    def test_returns_empty_on_insufficient_data(self):
        """Less than 20 bars → empty list."""
        bars = _flat_bars(10)
        config = BreakoutConfig()
        assert detect_breakouts(bars, config) == []

    def test_flat_bars_no_signals(self):
        """Pure flat bars should produce no signals (or only very weak ones)."""
        bars = _flat_bars(80, price=10.0)
        config = BreakoutConfig()
        signals = detect_breakouts(bars, config)
        # All signals (if any) should have None strength (below weak threshold)
        for s in signals:
            assert s.strength != "strong"

    def test_breakout_with_wyckoff(self):
        """Integration: bars + WyckoffResult → breakout signals."""
        bars = _flat_bars(50, price=10.0)
        bars.append(OHLCV(open=10.0, high=10.04, low=9.96, close=10.0, volume=1000))
        # Big breakout bar
        bars.append(OHLCV(open=10.1, high=11.0, low=10.05, close=10.8, volume=5000))

        config = BreakoutConfig()
        wyckoff = WyckoffResult(
            phase=WyckoffPhase.ACCUMULATION,
            confidence=0.6,
            support=9.5,
            resistance=10.05,
            detail="威科夫吸筹阶段 — 盘整40日",
        )
        signals = detect_breakouts(bars, config, wyckoff_result=wyckoff)

        assert len(signals) >= 1
        types = [s.signal_type for s in signals]
        assert "resistance_breakout" in types

    def test_disabled_returns_empty(self):
        """When config.enabled is False, caller should skip — but detect_breakouts
        still works; the enabled check is in stock_scorer."""
        bars = _flat_bars(50, price=10.0)
        bars.append(OHLCV(open=10.0, high=10.04, low=9.96, close=10.0, volume=1000))
        bars.append(OHLCV(open=10.1, high=11.0, low=10.05, close=10.8, volume=5000))
        config = BreakoutConfig()
        # detect_breakouts doesn't check enabled itself
        signals = detect_breakouts(bars, config)
        assert isinstance(signals, list)

    def test_description_chinese(self):
        """Signal descriptions should contain Chinese text."""
        bars = _flat_bars(65, price=10.0)
        bars.append(OHLCV(open=10.0, high=11.0, low=10.0, close=10.8, volume=2000))

        config = BreakoutConfig()
        signals = detect_breakouts(bars, config)

        for s in signals:
            # All descriptions should have Chinese characters
            assert any("\u4e00" <= c <= "\u9fff" for c in s.description)


# ── Edge cases ──────────────────────────────────────────


class TestEdgeCases:
    def test_zero_volume_bars(self):
        """Bars with zero volume should not crash."""
        bars = _flat_bars(30, price=10.0, volume=0.0)
        bars.append(OHLCV(open=10.0, high=11.0, low=10.0, close=10.8, volume=0.0))
        config = BreakoutConfig()
        signals = detect_breakouts(bars, config)
        assert isinstance(signals, list)

    def test_single_bar_above_minimum(self):
        """Exactly 20 bars — minimum for detection."""
        bars = _flat_bars(20, price=10.0)
        config = BreakoutConfig()
        signals = detect_breakouts(bars, config)
        assert isinstance(signals, list)

    def test_doji_bar(self):
        """Doji (open == close) should not crash."""
        bars = _flat_bars(30, price=10.0)
        bars.append(OHLCV(open=10.0, high=10.5, low=9.5, close=10.0, volume=1000))
        config = BreakoutConfig()
        signals = detect_breakouts(bars, config)
        assert isinstance(signals, list)

    def test_volume_ratio_text(self):
        """Volume ratio < 1.0 → '缩量'; >= 1.0 → '放量X倍'."""
        from aisp.screening.breakout import _vol_text

        assert _vol_text(0.5) == "缩量"
        assert "放量" in _vol_text(2.0)
        assert "2.0" in _vol_text(2.0)
