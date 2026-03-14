"""Tests for the trading plan generation module."""

from aisp.screening.trading_plan import (
    compute_price_limits,
    compute_trading_plan,
    trading_plan_to_dict,
)
from aisp.screening.wyckoff import OHLCV


def _make_bars(n: int, base: float = 10.0) -> list[OHLCV]:
    """Generate n synthetic OHLCV bars around a base price."""
    bars = []
    for i in range(n):
        c = base + (i % 5) * 0.1 - 0.2
        bars.append(OHLCV(open=c - 0.05, high=c + 0.3, low=c - 0.3, close=c, volume=1000.0))
    return bars


class TestPriceLimits:
    def test_normal_stock(self):
        limits = compute_price_limits("600519", 100.0)
        assert limits.limit_pct == 0.10
        assert limits.up_limit == 110.0
        assert limits.down_limit == 90.0

    def test_chinext(self):
        limits = compute_price_limits("300750", 50.0)
        assert limits.limit_pct == 0.20
        assert limits.up_limit == 60.0
        assert limits.down_limit == 40.0

    def test_star_market(self):
        limits = compute_price_limits("688001", 80.0)
        assert limits.limit_pct == 0.20

    def test_st_stock(self):
        limits = compute_price_limits("000001", 5.0, is_st=True)
        assert limits.limit_pct == 0.05
        assert limits.up_limit == 5.25
        assert limits.down_limit == 4.75


class TestComputeTradingPlan:
    def test_bullish_normal(self):
        bars = _make_bars(30, base=20.0)
        closes = [b.close for b in bars]
        plan = compute_trading_plan(
            "002709", closes, bars,
            direction="buy",
        )
        assert plan is not None
        assert plan.entry_zone is not None
        entry_low, entry_high = plan.entry_zone
        assert entry_low <= entry_high
        assert plan.stop_loss is not None
        assert plan.stop_loss < entry_low
        assert len(plan.targets) >= 1
        assert plan.targets[0] > entry_high
        assert plan.risk_reward > 0
        assert plan.position_hint in ("aggressive", "normal", "conservative")
        assert "T+1" in plan.t1_note

    def test_chinext_20pct_limit(self):
        bars = _make_bars(30, base=50.0)
        closes = [b.close for b in bars]
        plan = compute_trading_plan(
            "300750", closes, bars,
            direction="buy",
        )
        assert plan is not None
        assert plan.price_limits.limit_pct == 0.20

    def test_st_stock_5pct_limit(self):
        bars = _make_bars(30, base=5.0)
        closes = [b.close for b in bars]
        plan = compute_trading_plan(
            "000001", closes, bars,
            is_st=True,
            direction="buy",
        )
        assert plan is not None
        assert plan.price_limits.limit_pct == 0.05

    def test_limit_up_no_entry(self):
        bars = _make_bars(30, base=20.0)
        closes = [b.close for b in bars]
        plan = compute_trading_plan(
            "002709", closes, bars,
            is_limit_up=True,
            direction="buy",
        )
        assert plan is not None
        assert plan.entry_zone is None
        assert "涨停" in plan.rationale

    def test_limit_down_no_entry(self):
        bars = _make_bars(30, base=20.0)
        closes = [b.close for b in bars]
        plan = compute_trading_plan(
            "002709", closes, bars,
            is_limit_down=True,
            direction="sell",
        )
        assert plan is not None
        assert plan.entry_zone is None
        assert "跌停" in plan.rationale

    def test_insufficient_data_returns_none(self):
        plan = compute_trading_plan("002709", [], [])
        assert plan is None

    def test_short_data_fallback_atr(self):
        """With < 20 bars, ATR falls back to 3% of close."""
        bars = _make_bars(5, base=10.0)
        closes = [b.close for b in bars]
        plan = compute_trading_plan(
            "002709", closes, bars,
            direction="buy",
        )
        assert plan is not None
        assert plan.entry_zone is not None

    def test_wyckoff_data_used(self):
        bars = _make_bars(30, base=20.0)
        closes = [b.close for b in bars]
        wyckoff = {
            "phase": "accumulation",
            "support": 18.50,
            "resistance": 21.00,
            "confidence": 0.8,
            "multiplier": 1.15,
            "events": ["spring"],
            "detail": "吸筹",
        }
        plan = compute_trading_plan(
            "002709", closes, bars,
            wyckoff_data=wyckoff,
            direction="buy",
        )
        assert plan is not None
        assert plan.entry_zone is not None

    def test_bearish_plan(self):
        bars = _make_bars(30, base=20.0)
        closes = [b.close for b in bars]
        plan = compute_trading_plan(
            "002709", closes, bars,
            direction="sell",
        )
        assert plan is not None
        assert plan.position_hint == "conservative"
        assert "看空" in plan.rationale

    def test_neutral_plan(self):
        bars = _make_bars(30, base=20.0)
        closes = [b.close for b in bars]
        plan = compute_trading_plan(
            "002709", closes, bars,
            direction="hold",
        )
        assert plan is not None
        assert "中性" in plan.rationale


class TestTradingPlanToDict:
    def test_roundtrip(self):
        bars = _make_bars(30, base=20.0)
        closes = [b.close for b in bars]
        plan = compute_trading_plan(
            "002709", closes, bars,
            direction="buy",
        )
        assert plan is not None
        d = trading_plan_to_dict(plan)
        assert isinstance(d, dict)
        assert d["entry_zone"] is not None
        assert len(d["entry_zone"]) == 2
        assert d["stop_loss"] is not None
        assert d["price_limits"]["pct"] == 0.10
        assert d["position_hint"] in ("aggressive", "normal", "conservative")

    def test_limit_up_dict(self):
        bars = _make_bars(30, base=20.0)
        closes = [b.close for b in bars]
        plan = compute_trading_plan(
            "002709", closes, bars,
            is_limit_up=True,
        )
        d = trading_plan_to_dict(plan)
        assert d["entry_zone"] is None
