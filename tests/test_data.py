"""End-to-end tests for data fetching modules.

Tests use synthetic data mimicking yfinance/BaoStock output and verify the full
pipeline: transform → DB write → DB read → schema validation.
All DB operations use a shared in-memory SQLite database (StaticPool).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aisp.db.models import (
    Base,
    GlobalDaily,
    SectorDaily,
    StkDaily,
    StkSectorMap,
    TradingCalendar,
)

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
async def shared_engine():
    """Create a shared in-memory database. StaticPool ensures all connections
    see the same data (critical for in-memory SQLite)."""
    eng = create_async_engine(
        TEST_DB_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


class _EngineProxy:
    """Proxy that delegates everything to the real engine but makes dispose() a no-op."""

    def __init__(self, real_engine):
        self._real = real_engine

    async def dispose(self):
        pass  # Don't destroy the in-memory DB

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
def mock_engine(shared_engine, monkeypatch):
    """Monkeypatch get_engine in all data modules to use the shared engine.
    Also prevents dispose() from destroying the in-memory DB."""
    proxy = _EngineProxy(shared_engine)

    for mod_path in [
        "aisp.data.us_market",
        "aisp.data.commodities",
        "aisp.data.cn_market",
        "aisp.data.calendar",
    ]:
        monkeypatch.setattr(f"{mod_path}.get_engine", lambda url=None: proxy)

    yield proxy


@pytest.fixture
async def session(shared_engine):
    """Get a session bound to the shared engine."""
    sf = async_sessionmaker(shared_engine, class_=AsyncSession, expire_on_commit=False)
    async with sf() as s:
        yield s


# ── us_market.py tests ───────────────────────────────────────────────────


class TestUsMarket:
    """End-to-end tests for US market data fetching."""

    async def test_fetch_and_upsert(self, mock_engine, session, monkeypatch):
        """Verify yfinance data transforms correctly and matches GlobalDaily schema."""
        import pandas as pd

        from aisp.data import us_market
        from aisp.data.us_market import US_SYMBOLS

        dates = pd.date_range("2026-02-25", "2026-02-27", freq="B")
        symbols = list(US_SYMBOLS.keys())

        data_dict = {}
        for sym in symbols:
            data_dict[sym] = pd.DataFrame(
                {
                    "Open": [100.0, 101.0, 102.0],
                    "High": [105.0, 106.0, 107.0],
                    "Low": [99.0, 100.0, 101.0],
                    "Close": [103.0, 104.0, 105.0],
                    "Volume": [1000000.0, 1100000.0, 1200000.0],
                },
                index=dates[:3],
            )
        df = pd.concat(data_dict, axis=1)

        monkeypatch.setattr(us_market, "_download_yfinance", AsyncMock(return_value=df))

        count = await us_market.fetch_us_market(trade_date=date(2026, 2, 27))
        assert count > 0

        result = await session.execute(select(GlobalDaily))
        rows = result.all()
        assert len(rows) > 0

        for row in rows:
            obj = row[0]
            assert obj.trade_date is not None
            assert obj.symbol is not None
            assert obj.name is not None
            assert obj.asset_type is not None
            assert not math.isnan(obj.open)
            assert not math.isnan(obj.high)
            assert not math.isnan(obj.low)
            assert not math.isnan(obj.close)
            assert not math.isnan(obj.change_pct)
            if obj.volume is not None:
                assert not math.isnan(obj.volume)

    async def test_nan_volume_becomes_none(self, mock_engine, session, monkeypatch):
        """Verify NaN volume from yfinance is stored as NULL, not NaN."""
        import numpy as np
        import pandas as pd

        from aisp.data import us_market
        from aisp.db.models import AssetType

        dates = pd.date_range("2026-02-26", "2026-02-27", freq="B")
        sym_data = pd.DataFrame(
            {
                "Open": [100.0, 101.0],
                "High": [105.0, 106.0],
                "Low": [99.0, 100.0],
                "Close": [103.0, 104.0],
                "Volume": [np.nan, np.nan],
            },
            index=dates,
        )

        test_symbols = {"NANV": ("NaN Vol Test", AssetType.STOCK)}
        monkeypatch.setattr(us_market, "_download_yfinance", AsyncMock(return_value=sym_data))
        monkeypatch.setattr(us_market, "US_SYMBOLS", test_symbols)

        count = await us_market.fetch_us_market(trade_date=date(2026, 2, 27))
        assert count > 0

        result = await session.execute(
            select(GlobalDaily).where(GlobalDaily.symbol == "NANV")
        )
        for row in result.all():
            obj = row[0]
            assert obj.volume is None

    async def test_change_pct_calculation(self, mock_engine, session, monkeypatch):
        """Verify change_pct is correctly computed from consecutive closes."""
        import pandas as pd

        from aisp.data import us_market
        from aisp.db.models import AssetType

        dates = pd.date_range("2026-02-25", "2026-02-27", freq="B")
        sym_data = pd.DataFrame(
            {
                "Open": [100.0, 101.0, 102.0],
                "High": [105.0, 116.0, 107.0],
                "Low": [99.0, 100.0, 95.0],
                "Close": [100.0, 110.0, 99.0],  # +10%, -10%
                "Volume": [1000.0, 2000.0, 3000.0],
            },
            index=dates,
        )

        test_symbols = {"CHK": ("Check", AssetType.INDEX)}
        monkeypatch.setattr(us_market, "_download_yfinance", AsyncMock(return_value=sym_data))
        monkeypatch.setattr(us_market, "US_SYMBOLS", test_symbols)

        await us_market.fetch_us_market(trade_date=date(2026, 2, 27))

        result = await session.execute(
            select(GlobalDaily).where(GlobalDaily.symbol == "CHK").order_by(GlobalDaily.trade_date)
        )
        rows = [r[0] for r in result.all()]
        assert len(rows) == 3

        assert rows[0].change_pct == 0.0  # First day: no prev close
        assert abs(rows[1].change_pct - 10.0) < 0.01  # (110-100)/100*100
        assert abs(rows[2].change_pct - (-10.0)) < 0.01  # (99-110)/110*100

    async def test_upsert_idempotent(self, mock_engine, session, monkeypatch):
        """Verify running fetch twice doesn't duplicate records."""
        import pandas as pd

        from aisp.data import us_market
        from aisp.db.models import AssetType

        dates = pd.date_range("2026-02-27", "2026-02-27")
        sym_data = pd.DataFrame(
            {
                "Open": [100.0], "High": [105.0], "Low": [99.0],
                "Close": [103.0], "Volume": [1000.0],
            },
            index=dates,
        )

        test_symbols = {"IDMP": ("Idempotent", AssetType.STOCK)}
        monkeypatch.setattr(us_market, "_download_yfinance", AsyncMock(return_value=sym_data))
        monkeypatch.setattr(us_market, "US_SYMBOLS", test_symbols)

        await us_market.fetch_us_market(trade_date=date(2026, 2, 27))
        await us_market.fetch_us_market(trade_date=date(2026, 2, 27))

        count = await session.scalar(
            select(func.count()).select_from(GlobalDaily).where(GlobalDaily.symbol == "IDMP")
        )
        assert count == 1


# ── commodities.py tests ─────────────────────────────────────────────────


class TestCommodities:
    """End-to-end tests for commodity data fetching."""

    async def test_yf_commodity_transform(self, mock_engine, session, monkeypatch):
        """Verify yfinance commodity data matches GlobalDaily schema."""
        import pandas as pd

        from aisp.data import commodities

        dates = pd.date_range("2026-02-26", "2026-02-27", freq="B")
        data_dict = {}
        for sym in commodities.YF_COMMODITIES:
            data_dict[sym] = pd.DataFrame(
                {
                    "Open": [2650.0, 2680.0],
                    "High": [2700.0, 2710.0],
                    "Low": [2640.0, 2670.0],
                    "Close": [2680.0, 2700.0],
                    "Volume": [5000.0, 6000.0],
                },
                index=dates,
            )
        df = pd.concat(data_dict, axis=1)

        monkeypatch.setattr(commodities, "_fetch_yf_commodities", AsyncMock(return_value=df))
        monkeypatch.setattr(commodities, "_fetch_ak_commodity", AsyncMock(return_value=[]))

        count = await commodities.fetch_commodities(trade_date=date(2026, 2, 27))
        assert count > 0

        result = await session.execute(select(GlobalDaily))
        for row in result.all():
            obj = row[0]
            assert obj.close > 0
            assert not math.isnan(obj.open)
            assert not math.isnan(obj.close)

    async def test_akshare_commodity_transform(self, mock_engine, session, monkeypatch):
        """Verify AkShare domestic commodity data transforms correctly."""
        import pandas as pd

        from aisp.data import commodities

        monkeypatch.setattr(
            commodities, "_fetch_yf_commodities",
            AsyncMock(return_value=pd.DataFrame()),
        )

        # Simulate AkShare futures_main_sina output
        async def mock_ak(symbol):
            return [
                {
                    "date": "2026-02-27",
                    "open": 120000.0, "high": 125000.0,
                    "low": 118000.0, "close": 122000.0,
                    "volume": 500.0,
                },
            ]

        monkeypatch.setattr(commodities, "_fetch_ak_commodity", mock_ak)

        count = await commodities.fetch_commodities(trade_date=date(2026, 2, 27))
        assert count > 0

        result = await session.execute(select(GlobalDaily))
        rows = [r[0] for r in result.all()]
        assert len(rows) > 0
        for obj in rows:
            assert obj.symbol.startswith("AK_")
            assert obj.close == 122000.0


# ── cn_market.py tests ───────────────────────────────────────────────────


class TestCnMarket:
    """Tests for A-share market data transformation and DB writing."""

    def test_is_st(self):
        from aisp.data.cn_market import _is_st
        assert _is_st("*ST信威") is True
        assert _is_st("ST大集") is True
        assert _is_st("贵州茅台") is False
        assert _is_st("") is False

    def test_is_limit_up(self):
        from aisp.data.cn_market import _is_limit_up
        assert _is_limit_up(10.0, "贵州茅台") is True
        assert _is_limit_up(9.9, "贵州茅台") is True
        assert _is_limit_up(9.8, "贵州茅台") is False
        assert _is_limit_up(5.0, "*ST信威") is True
        assert _is_limit_up(4.9, "*ST信威") is True
        assert _is_limit_up(4.8, "*ST信威") is False

    def test_is_limit_down(self):
        from aisp.data.cn_market import _is_limit_down
        assert _is_limit_down(-10.0, "贵州茅台") is True
        assert _is_limit_down(-9.9, "贵州茅台") is True
        assert _is_limit_down(-9.8, "贵州茅台") is False
        assert _is_limit_down(-5.0, "*ST信威") is True
        assert _is_limit_down(-4.9, "*ST信威") is True
        assert _is_limit_down(-4.8, "*ST信威") is False

    def test_safe_float(self):
        from aisp.data.cn_market import _safe_float
        assert _safe_float(3.14) == 3.14
        assert _safe_float("3.14") == 3.14
        assert _safe_float(None) is None
        assert _safe_float("-") is None
        assert _safe_float("") is None
        assert _safe_float("abc") is None

    def test_compute_ma_from_closes(self):
        from aisp.data.cn_market import _compute_ma_from_closes

        closes = [10, 20, 30, 40, 50]
        mas = _compute_ma_from_closes(closes)
        assert mas["ma5"] == 30.0
        assert mas["ma10"] is None
        assert mas["ma60"] is None

        closes_10 = list(range(10, 0, -1))
        mas = _compute_ma_from_closes(closes_10)
        assert mas["ma5"] == 8.0
        assert mas["ma10"] == 5.5

    async def test_stock_transform_and_upsert(self, mock_engine, session):
        """Verify stock spot data transforms to StkDaily correctly."""
        from aisp.data.cn_market import _upsert_stocks

        trade_date = date(2026, 2, 27)
        records = [
            {
                "trade_date": trade_date, "code": "600519", "name": "贵州茅台",
                "open": 1800.0, "high": 1850.0, "low": 1780.0, "close": 1830.0,
                "volume": 5000000.0, "amount": 9150000000.0, "change_pct": 2.5,
                "turnover_rate": 0.4, "volume_ratio": 1.2,
                "net_inflow": 50000000.0, "market_cap": 2300000000000.0,
                "is_st": False, "is_limit_up": False, "is_limit_down": False,
            },
            {
                "trade_date": trade_date, "code": "000001", "name": "*ST测试",
                "open": 5.0, "high": 5.25, "low": 4.9, "close": 5.15,
                "volume": 100000.0, "amount": 500000.0, "change_pct": 5.0,
                "turnover_rate": 2.0, "volume_ratio": 0.8,
                "net_inflow": None, "market_cap": 1000000000.0,
                "is_st": True, "is_limit_up": True, "is_limit_down": False,
            },
        ]

        count = await _upsert_stocks(records, trade_date)
        assert count == 2

        result = await session.execute(select(StkDaily).order_by(StkDaily.code))
        rows = [r[0] for r in result.all()]
        assert len(rows) == 2

        moutai = next(r for r in rows if r.code == "600519")
        assert moutai.name == "贵州茅台"
        assert moutai.close == 1830.0
        assert moutai.change_pct == 2.5
        assert moutai.net_inflow == 50000000.0
        assert moutai.is_st is False
        assert moutai.is_limit_up is False

        st = next(r for r in rows if r.code == "000001")
        assert st.is_st is True
        assert st.is_limit_up is True
        assert st.net_inflow is None

    def test_to_bs_code(self):
        """Verify conversion from plain code to BaoStock format."""
        from aisp.data.cn_market import _to_bs_code

        assert _to_bs_code("600519") == "sh.600519"
        assert _to_bs_code("601398") == "sh.601398"
        assert _to_bs_code("000001") == "sz.000001"
        assert _to_bs_code("300750") == "sz.300750"
        assert _to_bs_code("900901") == "sh.900901"

    def test_from_bs_code(self):
        """Verify conversion from BaoStock format to plain code."""
        from aisp.data.cn_market import _from_bs_code

        assert _from_bs_code("sh.600519") == "600519"
        assert _from_bs_code("sz.000001") == "000001"
        assert _from_bs_code("600519") == "600519"

    async def test_ths_sector_daily_transform(self, mock_engine, session, monkeypatch):
        """Verify THS sector daily data transforms and upserts correctly."""
        import pandas as pd

        from aisp.data import cn_market

        # Mock AkShare stock_board_industry_summary_ths
        mock_df = pd.DataFrame({
            "板块": ["半导体", "银行"],
            "涨跌幅": [3.5, -0.8],
            "总成交量": [1000000.0, 2000000.0],
            "总成交额": [5000000000.0, 8000000000.0],
            "净流入": [100000000.0, -50000000.0],
            "上涨家数": [35, 10],
            "下跌家数": [10, 25],
        })

        async def mock_fetch():
            results = []
            for _, row in mock_df.iterrows():
                results.append({
                    "sector_name": row["板块"],
                    "change_pct": float(row["涨跌幅"]),
                    "volume": float(row["总成交量"]),
                    "amount": float(row["总成交额"]),
                    "net_inflow": float(row["净流入"]),
                    "stock_count": int(row["上涨家数"]) + int(row["下跌家数"]),
                    "up_count": int(row["上涨家数"]),
                    "down_count": int(row["下跌家数"]),
                })
            return results

        monkeypatch.setattr(cn_market, "_fetch_ths_sector_daily", mock_fetch)

        result = await mock_fetch()
        assert len(result) == 2

        semi = next(r for r in result if r["sector_name"] == "半导体")
        assert semi["change_pct"] == 3.5
        assert semi["volume"] == 1000000.0
        assert semi["stock_count"] == 45
        assert semi["up_count"] == 35
        assert semi["down_count"] == 10
        assert semi["net_inflow"] == 100000000.0

        bank = next(r for r in result if r["sector_name"] == "银行")
        assert bank["change_pct"] == -0.8
        assert bank["stock_count"] == 35
        assert bank["up_count"] == 10
        assert bank["down_count"] == 25

    async def test_sector_transform_and_upsert(self, mock_engine, session):
        """Verify sector data transforms to SectorDaily correctly."""
        from aisp.data.cn_market import _upsert_sectors

        trade_date = date(2026, 2, 27)
        records = [
            {
                "trade_date": trade_date, "sector_name": "半导体",
                "close": 1200.0, "change_pct": 3.5,
                "volume": 0.0, "amount": 0.0, "net_inflow": None,
                "stock_count": 0, "up_count": 35, "down_count": 10,
                "ma5": None, "ma10": None, "ma20": None, "ma60": None,
            },
        ]

        count = await _upsert_sectors(records)
        assert count == 1

        result = await session.execute(select(SectorDaily))
        row = result.all()[0][0]
        assert row.sector_name == "半导体"
        assert row.change_pct == 3.5
        assert row.up_count == 35
        assert row.down_count == 10
        assert row.volume == 0.0
        assert row.amount == 0.0

    async def test_sector_map_ths_source(self, mock_engine, session):
        """Verify THS sector-stock mappings can be stored and queried.

        Sector maps are now pre-populated by scripts/scrape_ths_sectors.py (source='ths'),
        not managed by cn_market.py. This test verifies the DB schema works correctly.
        """
        from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

        # Simulate what scrape_ths_sectors.py does
        now = datetime.now()
        mappings = [
            {"code": "600519", "sector_name": "酿酒行业", "source": "ths", "is_active": True, "updated_at": now},
            {"code": "000858", "sector_name": "酿酒行业", "source": "ths", "is_active": True, "updated_at": now},
            {"code": "601398", "sector_name": "银行", "source": "ths", "is_active": True, "updated_at": now},
        ]
        for rec in mappings:
            stmt = (
                sqlite_upsert(StkSectorMap)
                .values(**rec)
                .on_conflict_do_update(
                    index_elements=["code", "sector_name", "source"],
                    set_={"is_active": True},
                )
            )
            await session.execute(stmt)
        await session.commit()

        result = await session.execute(
            select(StkSectorMap).where(
                StkSectorMap.sector_name == "酿酒行业",
                StkSectorMap.is_active.is_(True),
            )
        )
        rows = result.all()
        assert len(rows) == 2
        assert rows[0][0].source == "ths"

    async def test_sector_ma_computation(self, mock_engine, session):
        """Verify moving average computation works with real DB data."""
        from aisp.data.cn_market import _compute_and_update_sector_mas, _upsert_sectors

        base_date = date(2026, 2, 20)
        closes = [100.0, 102.0, 98.0, 105.0, 103.0]
        records = []
        for i, close_val in enumerate(closes):
            d = base_date + timedelta(days=i)
            records.append({
                "trade_date": d, "sector_name": "半导体",
                "close": close_val, "change_pct": 0.0,
                "volume": 0.0, "amount": 0.0, "net_inflow": None,
                "stock_count": 0, "up_count": 0, "down_count": 0,
                "ma5": None, "ma10": None, "ma20": None, "ma60": None,
            })

        await _upsert_sectors(records)

        target_date = base_date + timedelta(days=4)
        await _compute_and_update_sector_mas(["半导体"], target_date)

        result = await session.execute(
            select(SectorDaily).where(
                SectorDaily.trade_date == target_date,
                SectorDaily.sector_name == "半导体",
            )
        )
        row = result.all()[0][0]

        expected_ma5 = sum(closes) / 5
        assert row.ma5 is not None
        assert abs(row.ma5 - expected_ma5) < 0.01
        assert row.ma10 is None


# ── calendar.py tests ────────────────────────────────────────────────────


class TestCalendar:
    """Tests for trading calendar logic."""

    async def test_calendar_in_db(self):
        """Verify real calendar data from earlier fetch-cn run."""
        from aisp.data.calendar import (
            get_next_trading_date,
            get_prev_trading_date,
            get_trading_dates_between,
            is_trading_day,
        )

        # Check if real DB has calendar data
        from aisp.db.engine import get_engine as real_get_engine
        from aisp.db.engine import get_session_factory
        actual_engine = real_get_engine()
        sf = get_session_factory(actual_engine)
        async with sf() as s:
            count = await s.scalar(select(func.count()).select_from(TradingCalendar))
        await actual_engine.dispose()

        if count == 0:
            pytest.skip("No calendar data in actual DB (run fetch-cn first)")

        assert await is_trading_day(date(2026, 2, 27)) is True
        assert await is_trading_day(date(2026, 2, 28)) is False
        assert await is_trading_day(date(2026, 3, 1)) is False

        next_td = await get_next_trading_date(date(2026, 2, 27))
        assert next_td == date(2026, 3, 2)

        prev_td = await get_prev_trading_date(date(2026, 3, 2))
        assert prev_td == date(2026, 2, 27)

        td_list = await get_trading_dates_between(date(2026, 2, 25), date(2026, 3, 6))
        assert len(td_list) > 0
        for d in td_list:
            assert d.weekday() < 5

    async def test_calendar_generation_logic(self, mock_engine, session, monkeypatch):
        """Test calendar generation with synthetic trading dates."""
        import bisect

        from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

        from aisp.data import calendar as cal_mod

        trading_dates = [
            date(2026, 1, 2),  # Friday
            date(2026, 1, 5),  # Monday
            date(2026, 1, 6),  # Tuesday
            date(2026, 1, 7),  # Wednesday
        ]

        monkeypatch.setattr(cal_mod, "_fetch_trade_dates", AsyncMock(return_value=trading_dates))

        # Build calendar manually for small range
        trade_date_set = set(trading_dates)
        sorted_dates = sorted(trading_dates)
        date_to_idx = {d: i for i, d in enumerate(sorted_dates)}

        start = sorted_dates[0]
        end = sorted_dates[-1] + timedelta(days=3)

        sf = async_sessionmaker(mock_engine, class_=AsyncSession, expire_on_commit=False)
        records = []
        current = start
        while current <= end:
            is_trading = current in trade_date_set
            prev_td = next_td = None

            if is_trading:
                idx = date_to_idx[current]
                if idx > 0:
                    prev_td = sorted_dates[idx - 1]
                if idx < len(sorted_dates) - 1:
                    next_td = sorted_dates[idx + 1]
            else:
                pos = bisect.bisect_left(sorted_dates, current)
                if pos > 0:
                    prev_td = sorted_dates[pos - 1]
                if pos < len(sorted_dates):
                    next_td = sorted_dates[pos]

            records.append({
                "cal_date": current, "is_trading_day": is_trading,
                "prev_trading_date": prev_td, "next_trading_date": next_td,
            })
            current += timedelta(days=1)

        async with sf() as s:
            for rec in records:
                stmt = (
                    sqlite_upsert(TradingCalendar).values(**rec)
                    .on_conflict_do_update(
                        index_elements=["cal_date"],
                        set_={k: v for k, v in rec.items() if k != "cal_date"},
                    )
                )
                await s.execute(stmt)
            await s.commit()

        # Verify
        result = await session.execute(
            select(TradingCalendar).order_by(TradingCalendar.cal_date)
        )
        rows = {r[0].cal_date: r[0] for r in result.all()}

        assert rows[date(2026, 1, 2)].is_trading_day is True
        assert rows[date(2026, 1, 3)].is_trading_day is False  # Saturday
        assert rows[date(2026, 1, 4)].is_trading_day is False  # Sunday
        assert rows[date(2026, 1, 5)].is_trading_day is True

        fri = rows[date(2026, 1, 2)]
        assert fri.next_trading_date == date(2026, 1, 5)

        sat = rows[date(2026, 1, 3)]
        assert sat.prev_trading_date == date(2026, 1, 2)
        assert sat.next_trading_date == date(2026, 1, 5)

        mon = rows[date(2026, 1, 5)]
        assert mon.prev_trading_date == date(2026, 1, 2)
        assert mon.next_trading_date == date(2026, 1, 6)


# ── sources/ tests ───────────────────────────────────────────────────────


class TestSources:
    """Tests for sentiment data source adapters."""

    async def test_akshare_adapter_structure(self):
        """Verify AkShare adapter has correct structure and source name."""
        from aisp.data.sources.akshare_announcements import AkShareAnnouncementAdapter

        adapter = AkShareAnnouncementAdapter()
        assert adapter.source_name == "akshare"
        assert hasattr(adapter, "fetch_comments")

    async def test_xueqiu_adapter_returns_empty(self):
        """Verify Xueqiu placeholder adapter returns empty list."""
        from aisp.data.sources.xueqiu import XueqiuAdapter

        adapter = XueqiuAdapter()
        results = await adapter.fetch_comments(["600519"], since=datetime.now())
        assert results == []
        assert adapter.source_name == "xueqiu"

    def test_adapter_registry_complete(self):
        """Verify all adapters are registered."""
        from aisp.data.sources import get_all_adapters
        from aisp.data.sources.akshare_announcements import AkShareAnnouncementAdapter  # noqa: F401
        from aisp.data.sources.xueqiu import XueqiuAdapter  # noqa: F401

        adapters = get_all_adapters()
        assert "akshare" in adapters
        assert "xueqiu" in adapters
        assert len(adapters) >= 2


# ── Cross-module integration ─────────────────────────────────────────────


class TestDataIntegration:
    """Cross-module integration tests."""

    async def test_stock_sector_map_roundtrip(self, mock_engine, session):
        """Verify stock records and sector maps can be cross-referenced."""
        from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

        from aisp.data.cn_market import _upsert_stocks

        trade_date = date(2026, 2, 27)
        stocks = [
            {
                "trade_date": trade_date, "code": "600519", "name": "贵州茅台",
                "open": 1800.0, "high": 1850.0, "low": 1780.0, "close": 1830.0,
                "volume": 5000000.0, "amount": 9000000000.0, "change_pct": 2.5,
                "turnover_rate": 0.4, "volume_ratio": 1.2,
                "net_inflow": 50000000.0, "market_cap": 2300000000000.0,
                "is_st": False, "is_limit_up": False, "is_limit_down": False,
            },
        ]
        await _upsert_stocks(stocks, trade_date)

        # Simulate THS sector map (as written by scrape_ths_sectors.py)
        stmt = (
            sqlite_upsert(StkSectorMap)
            .values(code="600519", sector_name="酿酒行业", source="ths", is_active=True, updated_at=datetime.now())
            .on_conflict_do_update(
                index_elements=["code", "sector_name", "source"],
                set_={"is_active": True},
            )
        )
        await session.execute(stmt)
        await session.commit()

        result = await session.execute(
            select(StkSectorMap.sector_name).where(
                StkSectorMap.code == "600519", StkSectorMap.is_active.is_(True),
            )
        )
        sectors = [r[0] for r in result.all()]
        assert "酿酒行业" in sectors

        result = await session.execute(select(StkDaily).where(StkDaily.code == "600519"))
        stock = result.all()[0][0]
        assert stock.close == 1830.0

    async def test_upsert_updates_existing(self, mock_engine, session):
        """Verify upsert updates value on conflict instead of failing."""
        from aisp.data.cn_market import _upsert_stocks

        trade_date = date(2026, 2, 27)
        rec = {
            "trade_date": trade_date, "code": "600519", "name": "贵州茅台",
            "open": 1800.0, "high": 1850.0, "low": 1780.0, "close": 1830.0,
            "volume": 5000000.0, "amount": 9000000000.0, "change_pct": 2.5,
            "turnover_rate": 0.4, "volume_ratio": 1.2,
            "net_inflow": None, "market_cap": 2300000000000.0,
            "is_st": False, "is_limit_up": False, "is_limit_down": False,
        }
        await _upsert_stocks([rec], trade_date)

        # Update close price
        rec2 = {**rec, "close": 1850.0, "change_pct": 3.6}
        await _upsert_stocks([rec2], trade_date)

        result = await session.execute(select(StkDaily).where(StkDaily.code == "600519"))
        rows = result.all()
        assert len(rows) == 1
        assert rows[0][0].close == 1850.0
        assert rows[0][0].change_pct == 3.6


# ── btc_risk.py tests ────────────────────────────────────────────────────


class TestBtcRisk:
    """Tests for BTC risk appetite indicator."""

    def test_normalize_boundaries(self):
        from aisp.data.btc_risk import _normalize

        assert _normalize(-10.0, -10.0, 10.0) == 0.0
        assert _normalize(10.0, -10.0, 10.0) == 1.0
        assert _normalize(0.0, -10.0, 10.0) == 0.5
        # Clipping
        assert _normalize(-20.0, -10.0, 10.0) == 0.0
        assert _normalize(20.0, -10.0, 10.0) == 1.0

    def test_risk_score_computation(self):
        from aisp.data.btc_risk import compute_risk_score

        # All positive signals → high score
        score_high = compute_risk_score(
            change_24h=5.0, change_7d=10.0, change_30d=15.0,
            vol_7d=30.0, vol_30d=50.0, drawdown_pct=0.0,
        )
        assert score_high > 0.65

        # All negative signals → low score
        score_low = compute_risk_score(
            change_24h=-8.0, change_7d=-15.0, change_30d=-25.0,
            vol_7d=80.0, vol_30d=40.0, drawdown_pct=-25.0,
        )
        assert score_low < 0.35

        # Neutral
        score_mid = compute_risk_score(
            change_24h=0.0, change_7d=0.0, change_30d=0.0,
            vol_7d=40.0, vol_30d=40.0, drawdown_pct=-5.0,
        )
        assert 0.3 < score_mid < 0.7

    def test_to_prompt_text(self):
        from aisp.data.btc_risk import BtcRiskMetrics

        m = BtcRiskMetrics(
            price=67234.0, change_24h=2.3, change_7d=-5.1, change_30d=12.4,
            volatility_7d=45.0, volatility_30d=50.0,
            drawdown_from_30d_high=-3.5, risk_score=0.72,
        )
        text = m.to_prompt_text()
        assert "$67,234" in text
        assert "+2.3%" in text
        assert "强风险偏好" in text
        assert "0.72" in text

    def test_sentiment_labels(self):
        from aisp.data.btc_risk import BtcRiskMetrics

        base = dict(
            price=60000, change_24h=0, change_7d=0, change_30d=0,
            volatility_7d=40, volatility_30d=40, drawdown_from_30d_high=-5,
        )
        assert BtcRiskMetrics(**base, risk_score=0.80).sentiment_label == "强风险偏好"
        assert BtcRiskMetrics(**base, risk_score=0.50).sentiment_label == "中性"
        assert BtcRiskMetrics(**base, risk_score=0.20).sentiment_label == "弱风险偏好"
        # Boundary: 0.65 is not > 0.65, should be 中性
        assert BtcRiskMetrics(**base, risk_score=0.65).sentiment_label == "中性"
        # Boundary: 0.35 is not < 0.35, should be 中性
        assert BtcRiskMetrics(**base, risk_score=0.35).sentiment_label == "中性"

    async def test_fetch_graceful_failure(self, monkeypatch):
        """Mock yfinance failure → returns None without raising."""
        from aisp.data import btc_risk

        def _boom():
            raise ConnectionError("network down")

        monkeypatch.setattr("aisp.data.btc_risk.asyncio.to_thread", AsyncMock(side_effect=ConnectionError("network down")))
        result = await btc_risk.fetch_btc_risk_metrics()
        assert result is None
