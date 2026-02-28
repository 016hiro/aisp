"""Integration tests using synthetic data and mocked LLM."""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

from aisp.config import get_settings
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import (
    AssetType,
    Base,
    GlobalDaily,
    SectorDaily,
    StkDaily,
    StkSectorMap,
    TradingCalendar,
)

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def db_session():
    """Create an in-memory database with all tables."""
    engine = get_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        yield session

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture
async def populated_db(db_session):
    """Populate DB with synthetic test data."""
    trade_date = date(2025, 3, 3)

    # Trading calendar
    for d in [date(2025, 2, 28), date(2025, 3, 3), date(2025, 3, 4)]:
        db_session.add(TradingCalendar(
            cal_date=d,
            is_trading_day=True,
            prev_trading_date=date(2025, 2, 28) if d == date(2025, 3, 3) else None,
            next_trading_date=date(2025, 3, 3) if d == date(2025, 2, 28) else date(2025, 3, 4),
        ))

    # Non-trading days
    for d in [date(2025, 3, 1), date(2025, 3, 2)]:
        db_session.add(TradingCalendar(
            cal_date=d,
            is_trading_day=False,
            prev_trading_date=date(2025, 2, 28),
            next_trading_date=date(2025, 3, 3),
        ))

    # Global data
    db_session.add(GlobalDaily(
        trade_date=trade_date,
        symbol="^GSPC",
        name="S&P 500",
        asset_type=AssetType.INDEX,
        open=5000, high=5050, low=4980, close=5030,
        change_pct=0.6,
        volume=3_000_000_000,
    ))

    # Sector data
    for i, (name, change) in enumerate([
        ("半导体", 3.5), ("汽车整车", 2.1), ("光伏设备", -1.2),
        ("白酒", 0.5), ("银行", 0.3), ("有色金属", 4.2),
        ("电力设备", 3.8), ("医药生物", -2.1), ("房地产", -3.5),
        ("互联网服务", 1.5),
    ]):
        db_session.add(SectorDaily(
            trade_date=trade_date,
            sector_name=name,
            close=1000 + i * 10,
            change_pct=change,
            volume=1_000_000 * (i + 1),
            amount=10_000_000 * (i + 1),
            stock_count=50,
            up_count=30 if change > 0 else 15,
            down_count=15 if change > 0 else 30,
            ma60=990 + i * 10 if change > -2 else 1100 + i * 10,
        ))

    # Sector maps
    for code_suffix in range(1, 6):
        db_session.add(StkSectorMap(
            code=f"00000{code_suffix}",
            sector_name="半导体",
            source="ths",
            is_active=True,
            updated_at=datetime.now(),
        ))

    # Stock data
    for code_suffix in range(1, 6):
        db_session.add(StkDaily(
            trade_date=trade_date,
            code=f"00000{code_suffix}",
            name=f"测试股票{code_suffix}",
            open=10.0 + code_suffix,
            high=11.0 + code_suffix,
            low=9.5 + code_suffix,
            close=10.5 + code_suffix,
            volume=1_000_000 * code_suffix,
            amount=10_000_000 * code_suffix,
            change_pct=code_suffix * 0.5,
            turnover_rate=3.0 + code_suffix,
            volume_ratio=1.0 + code_suffix * 0.2,
            net_inflow=100_000 * code_suffix if code_suffix > 2 else None,
            market_cap=1_000_000_000 * code_suffix,
            is_st=False,
            is_limit_up=False,
            is_limit_down=False,
        ))

    await db_session.commit()
    return trade_date


def test_config_loads():
    """Test that configuration loads with defaults."""
    settings = get_settings()
    assert settings.scoring.weight_fund == 0.4
    assert settings.scoring.weight_momentum == 0.3
    assert settings.scoring.weight_technical == 0.2
    assert settings.scoring.weight_quality == 0.1
    # Weights should sum to 1.0
    total = (
        settings.scoring.weight_fund
        + settings.scoring.weight_momentum
        + settings.scoring.weight_technical
        + settings.scoring.weight_quality
    )
    assert abs(total - 1.0) < 0.001


def test_percentile_rank():
    """Test percentile ranking function."""
    from aisp.screening.stock_scorer import _percentile_rank

    assert _percentile_rank([10, 20, 30]) == [0.0, 0.5, 1.0]
    assert _percentile_rank([30, 20, 10]) == [1.0, 0.5, 0.0]
    assert _percentile_rank([None, 20, 30]) == [0.5, 0.0, 1.0]
    assert _percentile_rank([]) == []


def test_turnover_suitability():
    """Test turnover rate suitability scoring."""
    from aisp.screening.stock_scorer import _turnover_suitability

    scores = _turnover_suitability([5.0, 1.0, 15.0, None, 0.0])
    assert scores[0] == 1.0  # Ideal range
    assert scores[1] < 1.0 and scores[1] > 0  # Low but not zero
    assert scores[2] < 1.0  # Too high
    assert scores[3] == 0.5  # None → default
    assert scores[4] == 0.0  # Zero turnover


def test_json_parsing():
    """Test LLM response JSON parsing."""
    from aisp.engine.llm_client import _parse_json_response

    # Direct JSON
    assert _parse_json_response('{"a": 1}') == {"a": 1}

    # JSON in text
    assert _parse_json_response('Some text {"c": 3} more text') == {"c": 3}

    # Array
    assert _parse_json_response('[{"id": 1}]') == [{"id": 1}]

    # Empty / unparseable
    assert _parse_json_response("no json here") == {}


def test_stop_loss_checks():
    """Test exit signal logic."""
    from aisp.engine.signals import _check_hard_stop, _check_trailing_stop

    # Hard stop
    assert _check_hard_stop(100, 89, False) is True  # -11% > -10%
    assert _check_hard_stop(100, 91, False) is False
    assert _check_hard_stop(100, 94, True) is True  # ST: -6% > -5%

    # Trailing stop
    assert _check_trailing_stop(100, 120, 105) is True  # >50% retracement
    assert _check_trailing_stop(100, 120, 115) is False


def test_retry_decorator():
    """Test the with_retry decorator."""
    from aisp.data import with_retry

    call_count = 0

    @with_retry(max_retries=3, base_delay=0.01)
    async def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("fail")
        return "success"

    result = asyncio.get_event_loop().run_until_complete(flaky())
    assert result == "success"
    assert call_count == 3


def test_adapter_registry():
    """Test sentiment adapter registration."""
    from aisp.data.sources import get_all_adapters
    from aisp.data.sources.akshare_announcements import AkShareAnnouncementAdapter
    from aisp.data.sources.xueqiu import XueqiuAdapter  # noqa: F401 — import triggers registration

    adapters = get_all_adapters()
    assert "akshare" in adapters
    assert "xueqiu" in adapters
    assert adapters["akshare"] is AkShareAnnouncementAdapter
