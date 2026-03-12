"""Unit tests for Wyckoff phase detection — pure functions, synthetic data, no DB."""

from __future__ import annotations

import pytest

from aisp.config import WyckoffConfig
from aisp.screening.wyckoff import (
    OHLCV,
    WyckoffEvent,
    WyckoffPhase,
    compute_atr,
    compute_volume_sma,
    detect_consolidation,
    detect_phase,
    detect_prior_trend,
    detect_sos,
    detect_sow,
    detect_spring,
    detect_ut,
)

# ── Helpers ──────────────────────────────────────────────


def _flat_bars(n: int, price: float = 10.0, volume: float = 1000.0) -> list[OHLCV]:
    """Generate flat/consolidating bars with minimal price movement."""
    bars = []
    for i in range(n):
        noise = 0.01 * (i % 3 - 1)  # tiny oscillation: -0.01, 0, +0.01
        bars.append(OHLCV(
            open=price + noise,
            high=price + 0.05,
            low=price - 0.05,
            close=price + noise,
            volume=volume,
        ))
    return bars


def _trending_bars(n: int, start: float = 10.0, step: float = 0.2, volume: float = 1000.0) -> list[OHLCV]:
    """Generate trending bars (uptrend if step > 0, downtrend if step < 0)."""
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


# ── ATR tests ────────────────────────────────────────────


class TestComputeAtr:
    def test_empty_input(self):
        assert compute_atr([]) == []

    def test_single_bar(self):
        assert compute_atr([OHLCV(10, 11, 9, 10, 100)]) == []

    def test_known_values(self):
        """Two identical bars → TR = high-low, ATR = that value."""
        bars = [
            OHLCV(10, 12, 8, 10, 100),
            OHLCV(10, 12, 8, 10, 100),
            OHLCV(10, 12, 8, 10, 100),
        ]
        atrs = compute_atr(bars, period=2)
        # TR for each bar after first = max(12-8, |12-10|, |8-10|) = 4
        assert len(atrs) == 2
        assert atrs[0] == pytest.approx(4.0, abs=0.01)

    def test_sufficient_data(self):
        bars = _flat_bars(25)
        atrs = compute_atr(bars, period=20)
        assert len(atrs) >= 20
        # Flat bars should have very small ATR
        assert all(a < 0.5 for a in atrs)


# ── Volume SMA tests ────────────────────────────────────


class TestComputeVolumeSma:
    def test_empty(self):
        assert compute_volume_sma([]) == []

    def test_constant_volume(self):
        bars = _flat_bars(10, volume=500)
        sma = compute_volume_sma(bars, period=5)
        assert len(sma) == 10
        # After warmup, all should be 500
        assert sma[-1] == pytest.approx(500.0)


# ── Consolidation tests ─────────────────────────────────


class TestDetectConsolidation:
    def test_flat_bars_detected(self):
        """Flat bars should be detected as consolidation."""
        bars = _flat_bars(80, price=100.0)
        is_consol, days, support, resistance = detect_consolidation(
            bars, atr_ratio_threshold=0.03, min_consolidation_days=20
        )
        assert is_consol
        assert days >= 20
        assert support <= 100.0
        assert resistance >= 100.0

    def test_trending_bars_not_detected(self):
        """Strong trend should NOT be consolidation.

        Trending bars have low per-bar ATR relative to price (absolute step is constant),
        so the range check must reject them — any 20+ bar window spans > 10% of price.
        """
        bars = _trending_bars(80, start=10.0, step=0.5)
        is_consol, _, _, _ = detect_consolidation(
            bars, atr_ratio_threshold=0.05, min_consolidation_days=20, max_range_ratio=0.10
        )
        assert not is_consol

    def test_insufficient_data(self):
        bars = _flat_bars(10)
        is_consol, _, _, _ = detect_consolidation(
            bars, min_consolidation_days=20
        )
        assert not is_consol


# ── Structural detection tests ─────────────────────────────


class TestStructuralDetection:
    def test_selling_climax_anchors_range(self):
        """SC bar defines support, AR defines resistance."""
        down = _trending_bars(30, start=20.0, step=-0.2, volume=1000)
        sc = OHLCV(open=14.0, high=14.5, low=12.0, close=12.5, volume=5000)
        ar_bars = _trending_bars(5, start=13.0, step=0.4, volume=1500)
        flat = _flat_bars(30, price=13.5, volume=1000)

        bars = [*down, sc, *ar_bars, *flat]
        is_consol, _days, support, resistance = detect_consolidation(
            bars, min_consolidation_days=15,
        )
        assert is_consol
        assert support == pytest.approx(12.0)  # SC low
        assert resistance > 14.0  # AR high

    def test_buying_climax_anchors_range(self):
        """BC bar defines resistance, AR defines support."""
        up = _trending_bars(30, start=10.0, step=0.2, volume=1000)
        bc = OHLCV(open=16.0, high=18.0, low=15.5, close=17.5, volume=5000)
        ar_bars = _trending_bars(5, start=17.0, step=-0.4, volume=1500)
        flat = _flat_bars(30, price=16.0, volume=1000)

        bars = [*up, bc, *ar_bars, *flat]
        is_consol, _days, support, resistance = detect_consolidation(
            bars, min_consolidation_days=15,
        )
        assert is_consol
        assert resistance == pytest.approx(18.0)  # BC high
        assert support < 16.0  # AR low

    def test_no_climax_falls_to_range_detection(self):
        """Uniform volume data (no climax) falls to ATR-based fallback."""
        bars = _flat_bars(80, price=50.0, volume=1000)
        is_consol, *_ = detect_consolidation(
            bars, atr_ratio_threshold=0.03, min_consolidation_days=20,
        )
        assert is_consol

    def test_structural_accumulation_with_spring(self):
        """Full structural path: downtrend -> SC -> AR -> flat -> spring."""
        down = _trending_bars(30, start=20.0, step=-0.2, volume=1000)
        sc = OHLCV(open=14.0, high=14.5, low=12.0, close=12.5, volume=5000)
        ar_bars = _trending_bars(5, start=13.0, step=0.4, volume=1500)
        flat = _flat_bars(20, price=13.5, volume=1000)
        spring = OHLCV(open=13.5, high=13.6, low=11.5, close=13.4, volume=500)

        bars = [*down, sc, *ar_bars, *flat, spring]
        config = WyckoffConfig(min_bars=30, min_consolidation_days=15)
        result = detect_phase(bars, config)

        assert result.phase == WyckoffPhase.ACCUMULATION
        assert WyckoffEvent.SPRING in result.detected_events
        assert result.multiplier >= 1.0

    def test_structural_distribution_with_ut(self):
        """Full structural path: uptrend -> BC -> AR -> flat -> UT."""
        up = _trending_bars(30, start=10.0, step=0.2, volume=1000)
        bc = OHLCV(open=16.0, high=18.0, low=15.5, close=17.5, volume=5000)
        ar_bars = _trending_bars(5, start=17.0, step=-0.4, volume=1500)
        flat = _flat_bars(20, price=16.0, volume=1000)
        ut = OHLCV(open=16.0, high=18.5, low=15.9, close=15.95, volume=500)

        bars = [*up, bc, *ar_bars, *flat, ut]
        config = WyckoffConfig(min_bars=30, min_consolidation_days=15)
        result = detect_phase(bars, config)

        assert result.phase == WyckoffPhase.DISTRIBUTION
        assert WyckoffEvent.UT in result.detected_events
        assert result.multiplier <= 1.0


# ── Prior trend tests ────────────────────────────────────


class TestDetectPriorTrend:
    def test_downtrend(self):
        bars = _trending_bars(60, start=20.0, step=-0.2)
        result = detect_prior_trend(bars, range_start_idx=60)
        assert result == "down"

    def test_uptrend(self):
        bars = _trending_bars(60, start=10.0, step=0.2)
        result = detect_prior_trend(bars, range_start_idx=60)
        assert result == "up"

    def test_flat(self):
        bars = _flat_bars(60)
        result = detect_prior_trend(bars, range_start_idx=60)
        assert result == "flat"

    def test_insufficient_data(self):
        bars = _flat_bars(5)
        result = detect_prior_trend(bars, range_start_idx=5)
        assert result == "flat"


# ── Spring detection ─────────────────────────────────────


class TestDetectSpring:
    def test_spring_detected(self):
        """False breakdown below support + low volume + close recovery → Spring."""
        support = 10.0
        bars = _flat_bars(20, price=10.0)
        # Insert a spring bar: low dips below support, closes above
        bars.append(OHLCV(open=10.0, high=10.1, low=9.7, close=10.05, volume=500))
        vol_sma = compute_volume_sma(bars)
        result = detect_spring(
            bars, support, vol_sma,
            breach_threshold=0.02,
            vol_low_ratio=0.8,
            close_position_threshold=0.5,
        )
        assert result

    def test_no_spring_high_volume(self):
        """Breakdown on high volume is NOT a spring."""
        support = 10.0
        bars = _flat_bars(20, price=10.0)
        # High volume breakdown
        bars.append(OHLCV(open=10.0, high=10.1, low=9.7, close=10.05, volume=5000))
        vol_sma = compute_volume_sma(bars)
        result = detect_spring(
            bars, support, vol_sma,
            breach_threshold=0.02,
            vol_low_ratio=0.8,
            close_position_threshold=0.5,
        )
        assert not result


# ── SOS detection ────────────────────────────────────────


class TestDetectSos:
    def test_sos_detected(self):
        """Breakout above resistance on high volume → SOS."""
        resistance = 10.05
        bars = _flat_bars(20, price=10.0)
        bars.append(OHLCV(open=10.0, high=10.5, low=10.0, close=10.3, volume=3000))
        vol_sma = compute_volume_sma(bars)
        result = detect_sos(bars, resistance, vol_sma, vol_high_ratio=1.5)
        assert result

    def test_no_sos_low_volume(self):
        """Breakout on low volume is NOT SOS."""
        resistance = 10.05
        bars = _flat_bars(20, price=10.0)
        bars.append(OHLCV(open=10.0, high=10.5, low=10.0, close=10.3, volume=500))
        vol_sma = compute_volume_sma(bars)
        result = detect_sos(bars, resistance, vol_sma, vol_high_ratio=1.5)
        assert not result


# ── UT detection ─────────────────────────────────────────


class TestDetectUt:
    def test_ut_detected(self):
        """False breakout above resistance + weak close + low volume → UT."""
        resistance = 10.05
        bars = _flat_bars(20, price=10.0)
        # UT bar: high spikes above resistance, closes below, low volume
        bars.append(OHLCV(open=10.0, high=10.5, low=9.9, close=9.95, volume=500))
        vol_sma = compute_volume_sma(bars)
        result = detect_ut(
            bars, resistance, vol_sma,
            breach_threshold=0.02,
            vol_low_ratio=0.8,
            close_position_threshold=0.5,
        )
        assert result

    def test_no_ut_high_volume(self):
        """High volume breakout is NOT UT (could be genuine breakout)."""
        resistance = 10.05
        bars = _flat_bars(20, price=10.0)
        bars.append(OHLCV(open=10.0, high=10.5, low=9.9, close=9.95, volume=5000))
        vol_sma = compute_volume_sma(bars)
        result = detect_ut(
            bars, resistance, vol_sma,
            breach_threshold=0.02,
            vol_low_ratio=0.8,
            close_position_threshold=0.5,
        )
        assert not result


# ── SOW detection ────────────────────────────────────────


class TestDetectSow:
    def test_sow_detected(self):
        """Breakdown below support on high volume → SOW."""
        support = 9.95
        bars = _flat_bars(20, price=10.0)
        bars.append(OHLCV(open=10.0, high=10.0, low=9.5, close=9.6, volume=3000))
        vol_sma = compute_volume_sma(bars)
        result = detect_sow(bars, support, vol_sma, vol_high_ratio=1.5)
        assert result


# ── Full phase detection ─────────────────────────────────


class TestDetectPhase:
    def test_insufficient_data(self):
        """Too few bars → UNKNOWN, multiplier=1.0."""
        bars = _flat_bars(10)
        result = detect_phase(bars)
        assert result.phase == WyckoffPhase.UNKNOWN
        assert result.multiplier == 1.0
        assert result.confidence == 0.0

    def test_no_consolidation(self):
        """Strong trend → UNKNOWN (with tight range limit)."""
        bars = _trending_bars(90, step=0.5)
        config = WyckoffConfig(consolidation_max_range=0.10)
        result = detect_phase(bars, config)
        assert result.phase == WyckoffPhase.UNKNOWN
        assert result.multiplier == 1.0

    def test_accumulation_with_spring(self):
        """Downtrend → consolidation → spring → Accumulation phase."""
        # Steep downtrend (step=-0.3) so the range fills up fast,
        # leaving enough pre-bars for prior trend detection
        down = _trending_bars(60, start=30.0, step=-0.3)
        # 25 bars flat consolidation near the downtrend end
        flat = _flat_bars(25, price=12.0)
        # Spring event at the end
        spring_bar = OHLCV(open=12.0, high=12.05, low=11.7, close=12.02, volume=500)

        bars = down + flat + [spring_bar]

        config = WyckoffConfig(min_bars=30, min_consolidation_days=15)
        result = detect_phase(bars, config)

        assert result.phase == WyckoffPhase.ACCUMULATION
        assert result.confidence > 0
        assert result.multiplier >= 1.0
        assert WyckoffEvent.SPRING in result.detected_events

    def test_distribution_with_ut(self):
        """Uptrend → consolidation → UT → Distribution phase."""
        # 60 bars uptrend
        up = _trending_bars(60, start=5.0, step=0.1)
        # 25 bars flat consolidation
        flat = _flat_bars(25, price=11.0)
        # UT event: high spikes, close fails, low volume
        ut_bar = OHLCV(open=11.0, high=11.5, low=10.9, close=10.92, volume=500)

        bars = up + flat + [ut_bar]

        config = WyckoffConfig(min_bars=30, min_consolidation_days=15)
        result = detect_phase(bars, config)

        assert result.phase == WyckoffPhase.DISTRIBUTION
        assert result.confidence > 0
        assert result.multiplier <= 1.0
        assert WyckoffEvent.UT in result.detected_events

    def test_confidence_scales_multiplier(self):
        """Higher confidence → stronger multiplier adjustment."""
        from aisp.screening.wyckoff import _lerp
        assert _lerp(1.0, 1.25, 0.0) == pytest.approx(1.0)
        assert _lerp(1.0, 1.25, 0.5) == pytest.approx(1.125)
        assert _lerp(1.0, 1.25, 1.0) == pytest.approx(1.25)

    def test_distribution_multiplier_discount(self):
        """Distribution → multiplier < 1.0."""
        from aisp.screening.wyckoff import _lerp
        assert _lerp(1.0, 0.60, 0.5) == pytest.approx(0.80)
        assert _lerp(1.0, 0.60, 1.0) == pytest.approx(0.60)

    def test_wyckoff_disabled(self):
        """When disabled, detect_phase still works but caller should skip."""
        config = WyckoffConfig(enabled=False)
        bars = _flat_bars(90)
        # detect_phase doesn't check enabled — that's the caller's job
        result = detect_phase(bars, config)
        # Still returns a result; enabled check is in stock_scorer
        assert result.phase in WyckoffPhase

    def test_flat_prior_trend_is_unknown(self):
        """Flat prior trend → UNKNOWN (no clear accumulation or distribution)."""
        flat_pre = _flat_bars(30, price=10.0)
        flat_consol = _flat_bars(35, price=10.0)
        bars = flat_pre + flat_consol

        config = WyckoffConfig(min_bars=30, min_consolidation_days=15)
        result = detect_phase(bars, config)
        assert result.phase == WyckoffPhase.UNKNOWN
