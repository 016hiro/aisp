"""End-to-end tests for market_data.py fetch functions.

Each test hits the real external API for ONE day/record, writes to an in-memory DB,
reads it back, and validates schema + data sanity. This catches:
- API signature changes (AkShare / BaoStock / EastMoney)
- Column name / index mapping errors
- Upsert key conflicts
- Data type mismatches

Marked with `@pytest.mark.e2e` — skipped in CI, run with `pytest -m e2e`.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aisp.db.models import (
    Base,
    MarketSentiment,
    StkDaily,
    StkLhb,
    StkMargin,
    StkProfile,
    StkQuarterly,
)

# Use a recent known trading day
TRADE_DATE = date(2026, 3, 13)
TEST_CODE = "002709"  # 天赐材料 (深市主板, 两融标的)
TEST_CODE_SH = "601398"  # 工商银行 (沪市主板)


class _EngineProxy:
    """Proxy that makes dispose() a no-op to keep in-memory DB alive."""

    def __init__(self, real_engine):
        self._real = real_engine

    async def dispose(self):
        pass

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.fixture
async def shared_engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
def mock_engine(shared_engine, monkeypatch):
    proxy = _EngineProxy(shared_engine)
    monkeypatch.setattr("aisp.data.market_data.get_engine", lambda url=None: proxy)
    monkeypatch.setattr("aisp.data.cn_market.get_engine", lambda url=None: proxy)
    yield proxy


@pytest.fixture
async def session(shared_engine):
    sf = async_sessionmaker(shared_engine, class_=AsyncSession, expire_on_commit=False)
    async with sf() as s:
        yield s


# ── 1. PE/PB (cn_market.py BaoStock fields) ─────────────────────────


@pytest.mark.e2e
class TestPePb:
    """Verify BaoStock peTTM / pbMRQ fields are fetched and stored."""

    async def test_fetch_cn_includes_pe_pb(self, mock_engine, session):
        from aisp.data.cn_market import fetch_cn_market

        result = await fetch_cn_market(TRADE_DATE, codes=[TEST_CODE])
        assert result["stocks"] > 0

        row = await session.execute(
            select(StkDaily).where(
                StkDaily.code == TEST_CODE,
                StkDaily.trade_date == TRADE_DATE,
            )
        )
        stk = row.scalar_one()
        assert stk.close > 0
        assert stk.pe_ttm is not None, "pe_ttm should not be None"
        assert stk.pb_mrq is not None, "pb_mrq should not be None"
        assert stk.pe_ttm > 0
        assert stk.pb_mrq > 0

    async def test_pe_pb_idempotent(self, mock_engine, session):
        from aisp.data.cn_market import fetch_cn_market

        await fetch_cn_market(TRADE_DATE, codes=[TEST_CODE])
        await fetch_cn_market(TRADE_DATE, codes=[TEST_CODE])

        count = await session.scalar(
            select(func.count()).select_from(StkDaily).where(
                StkDaily.code == TEST_CODE,
                StkDaily.trade_date == TRADE_DATE,
            )
        )
        assert count == 1


# ── 2. Market Sentiment ──────────────────────────────────────────────


@pytest.mark.e2e
class TestMarketSentiment:
    """Verify market sentiment data (涨停/跌停/炸板率) fetch and schema."""

    async def test_fetch_market_sentiment(self, mock_engine, session):
        from aisp.data.market_data import fetch_market_sentiment

        n = await fetch_market_sentiment(TRADE_DATE)
        assert n == 1

        row = await session.execute(
            select(MarketSentiment).where(MarketSentiment.trade_date == TRADE_DATE)
        )
        ms = row.scalar_one()
        assert ms.trade_date == TRADE_DATE

        # At least some fields should be populated
        has_data = any([
            ms.limit_up_count is not None,
            ms.activity_rate is not None,
            ms.max_streak is not None,
        ])
        assert has_data, "At least one sentiment field should be non-None"

        # Sanity checks on populated fields
        if ms.limit_up_count is not None:
            assert ms.limit_up_count >= 0
        if ms.limit_down_count is not None:
            assert ms.limit_down_count >= 0
        if ms.blast_rate is not None:
            assert 0 <= ms.blast_rate <= 100
        if ms.max_streak is not None:
            assert ms.max_streak >= 1

    async def test_market_sentiment_idempotent(self, mock_engine, session):
        from aisp.data.market_data import fetch_market_sentiment

        await fetch_market_sentiment(TRADE_DATE)
        await fetch_market_sentiment(TRADE_DATE)

        count = await session.scalar(
            select(func.count()).select_from(MarketSentiment).where(
                MarketSentiment.trade_date == TRADE_DATE
            )
        )
        assert count == 1

    async def test_market_sentiment_nontrading_day(self, mock_engine, session):
        """Non-trading day should not raise. May return 0 or 1 depending on
        whether stock_market_activity_legu returns cached latest-day data."""
        from aisp.data.market_data import fetch_market_sentiment

        n = await fetch_market_sentiment(date(2026, 3, 15))  # Sunday
        assert n in (0, 1)  # graceful either way


# ── 3. Fund Flow ─────────────────────────────────────────────────────


@pytest.mark.e2e
class TestFundFlow:
    """Verify individual stock fund flow (资金分层) fetch and stk_daily update."""

    async def test_fetch_fund_flow(self, mock_engine, session):
        from aisp.data.cn_market import fetch_cn_market
        from aisp.data.market_data import fetch_fund_flow

        # First insert base stk_daily rows
        await fetch_cn_market(TRADE_DATE, codes=[TEST_CODE])

        n = await fetch_fund_flow([TEST_CODE])
        assert n > 0

        row = await session.execute(
            select(StkDaily).where(
                StkDaily.code == TEST_CODE,
                StkDaily.main_net.isnot(None),
            ).order_by(StkDaily.trade_date.desc()).limit(1)
        )
        stk = row.scalar_one()
        # main_net should be a real number (positive or negative)
        assert isinstance(stk.main_net, float)
        assert stk.main_pct is not None
        # All 4 tiers should be populated
        assert stk.super_large_net is not None
        assert stk.large_net is not None
        assert stk.medium_net is not None
        assert stk.small_net is not None

    async def test_fund_flow_idempotent(self, mock_engine, session):
        from aisp.data.cn_market import fetch_cn_market
        from aisp.data.market_data import fetch_fund_flow

        await fetch_cn_market(TRADE_DATE, codes=[TEST_CODE])
        await fetch_fund_flow([TEST_CODE])
        await fetch_fund_flow([TEST_CODE])

        count = await session.scalar(
            select(func.count()).select_from(StkDaily).where(
                StkDaily.code == TEST_CODE,
                StkDaily.trade_date == TRADE_DATE,
            )
        )
        assert count == 1  # No duplicate rows


# ── 4. Stock Profile ─────────────────────────────────────────────────


@pytest.mark.e2e
class TestStkProfile:
    """Verify stock profile (股本结构/板块/上市日期) fetch and schema."""

    async def test_fetch_stk_profile(self, mock_engine, session):
        from aisp.data.market_data import fetch_stk_profile

        n = await fetch_stk_profile([TEST_CODE])
        assert n == 1

        row = await session.execute(
            select(StkProfile).where(StkProfile.code == TEST_CODE)
        )
        p = row.scalar_one()
        assert p.code == TEST_CODE
        assert p.name is not None and len(p.name) > 0
        assert p.board_type in ("main_sz", "main_sh", "gem", "star", "other")
        assert p.listing_date is not None
        assert p.listing_date < date.today()
        assert p.updated_at is not None

        # Share counts from BaoStock query_profit_data
        assert p.total_shares is not None and p.total_shares > 0
        assert p.liq_shares is not None and p.liq_shares > 0
        assert p.liq_shares <= p.total_shares

    async def test_profile_idempotent(self, mock_engine, session):
        from aisp.data.market_data import fetch_stk_profile

        await fetch_stk_profile([TEST_CODE])
        await fetch_stk_profile([TEST_CODE])

        count = await session.scalar(
            select(func.count()).select_from(StkProfile).where(StkProfile.code == TEST_CODE)
        )
        assert count == 1

    async def test_profile_board_detection(self, mock_engine, session):
        """Verify board type detection for different code prefixes."""
        from aisp.data.market_data import _detect_board_type

        assert _detect_board_type("600519") == "main_sh"
        assert _detect_board_type("000001") == "main_sz"
        assert _detect_board_type("300750") == "gem"
        assert _detect_board_type("688981") == "star"


# ── 5. Quarterly Financials ──────────────────────────────────────────


@pytest.mark.e2e
class TestStkQuarterly:
    """Verify quarterly financial data fetch and schema."""

    async def test_fetch_stk_quarterly(self, mock_engine, session):
        from aisp.data.market_data import fetch_stk_quarterly

        n = await fetch_stk_quarterly([TEST_CODE], 2025, 3)
        assert n == 1

        row = await session.execute(
            select(StkQuarterly).where(
                StkQuarterly.code == TEST_CODE,
                StkQuarterly.year == 2025,
                StkQuarterly.quarter == 3,
            )
        )
        q = row.scalar_one()
        assert q.code == TEST_CODE
        assert q.year == 2025
        assert q.quarter == 3

        # Financial data sanity checks
        assert q.net_profit is not None
        assert q.roe is not None
        assert q.np_margin is not None
        assert q.pub_date is not None
        assert q.pub_date < date.today()
        assert q.updated_at is not None

    async def test_quarterly_growth_fields(self, mock_engine, session):
        from aisp.data.market_data import fetch_stk_quarterly

        await fetch_stk_quarterly([TEST_CODE], 2025, 3)

        row = await session.execute(
            select(StkQuarterly).where(
                StkQuarterly.code == TEST_CODE,
                StkQuarterly.year == 2025,
                StkQuarterly.quarter == 3,
            )
        )
        q = row.scalar_one()
        # Growth data from query_growth_data
        assert q.yoy_profit is not None, "yoy_profit should come from YOYNI"
        assert q.yoy_equity is not None, "yoy_equity should come from YOYEquity"

    async def test_quarterly_idempotent(self, mock_engine, session):
        from aisp.data.market_data import fetch_stk_quarterly

        await fetch_stk_quarterly([TEST_CODE], 2025, 3)
        await fetch_stk_quarterly([TEST_CODE], 2025, 3)

        count = await session.scalar(
            select(func.count()).select_from(StkQuarterly).where(
                StkQuarterly.code == TEST_CODE,
                StkQuarterly.year == 2025,
                StkQuarterly.quarter == 3,
            )
        )
        assert count == 1

    async def test_quarterly_empty_quarter(self, mock_engine, session):
        """Future quarter should return 0 without error."""
        from aisp.data.market_data import fetch_stk_quarterly

        n = await fetch_stk_quarterly([TEST_CODE], 2030, 4)
        assert n == 0


# ── 6. LHB (龙虎榜) ─────────────────────────────────────────────────


@pytest.mark.e2e
class TestStkLhb:
    """Verify LHB data fetch and schema."""

    async def test_fetch_lhb(self, mock_engine, session):
        from aisp.data.market_data import fetch_lhb

        n = await fetch_lhb(TRADE_DATE)
        assert n > 0

        rows = await session.execute(
            select(StkLhb).where(StkLhb.trade_date == TRADE_DATE)
        )
        records = rows.scalars().all()
        assert len(records) > 0

        for lhb in records:
            assert lhb.trade_date == TRADE_DATE
            assert len(lhb.code) == 6
            assert lhb.name is not None
            assert lhb.reason is not None and len(lhb.reason) > 0
            # buy_amount and sell_amount should be non-negative
            if lhb.buy_amount is not None:
                assert lhb.buy_amount >= 0
            if lhb.sell_amount is not None:
                assert lhb.sell_amount >= 0

    async def test_lhb_idempotent(self, mock_engine, session):
        from aisp.data.market_data import fetch_lhb

        await fetch_lhb(TRADE_DATE)
        count1 = await session.scalar(
            select(func.count()).select_from(StkLhb).where(StkLhb.trade_date == TRADE_DATE)
        )

        await fetch_lhb(TRADE_DATE)
        count2 = await session.scalar(
            select(func.count()).select_from(StkLhb).where(StkLhb.trade_date == TRADE_DATE)
        )

        assert count1 == count2

    async def test_lhb_nontrading_day(self, mock_engine, session):
        from aisp.data.market_data import fetch_lhb

        n = await fetch_lhb(date(2026, 3, 15))
        assert n == 0


# ── 7. Margin (融资融券) ─────────────────────────────────────────────


@pytest.mark.e2e
class TestStkMargin:
    """Verify margin data fetch via EastMoney datacenter API."""

    async def test_fetch_margin(self, mock_engine, session):
        from aisp.data.market_data import fetch_margin

        n = await fetch_margin([TEST_CODE], TRADE_DATE)
        assert n > 0

        rows = await session.execute(
            select(StkMargin).where(StkMargin.code == TEST_CODE)
            .order_by(StkMargin.trade_date.desc())
            .limit(3)
        )
        records = rows.scalars().all()
        assert len(records) > 0

        latest = records[0]
        assert latest.code == TEST_CODE
        assert latest.rzye is not None and latest.rzye > 0, "融资余额 should be positive"
        assert latest.rzrqye is not None and latest.rzrqye > 0

    async def test_margin_idempotent(self, mock_engine, session):
        from aisp.data.market_data import fetch_margin

        await fetch_margin([TEST_CODE], TRADE_DATE)
        count1 = await session.scalar(
            select(func.count()).select_from(StkMargin).where(StkMargin.code == TEST_CODE)
        )

        await fetch_margin([TEST_CODE], TRADE_DATE)
        count2 = await session.scalar(
            select(func.count()).select_from(StkMargin).where(StkMargin.code == TEST_CODE)
        )

        assert count1 == count2

    async def test_margin_multiple_stocks(self, mock_engine, session):
        from aisp.data.market_data import fetch_margin

        n = await fetch_margin([TEST_CODE, TEST_CODE_SH], TRADE_DATE)
        assert n > 0

        codes_in_db = set()
        rows = await session.execute(select(StkMargin.code).distinct())
        for row in rows.all():
            codes_in_db.add(row[0])

        # At least one of the two codes should have data
        assert codes_in_db & {TEST_CODE, TEST_CODE_SH}


# ── 8. Prompt format functions (pure, no API) ────────────────────────


class TestPromptFormatters:
    """Verify prompt format functions produce expected output."""

    def test_format_market_sentiment_full(self):
        from types import SimpleNamespace

        from aisp.engine.prompts import format_market_sentiment

        row = SimpleNamespace(
            total_amount=12345.0,
            limit_up_count=78,
            limit_down_count=12,
            real_limit_up=52,
            blast_rate=33.3,
            max_streak=5,
            prev_zt_premium=2.3,
            activity_rate=15.8,
        )
        result = format_market_sentiment(row)
        assert "涨停78" in result
        assert "实板52" in result
        assert "跌停12" in result
        assert "炸板率33%" in result
        assert "5板" in result
        assert "+2.3%" in result

    def test_format_market_sentiment_none(self):
        from aisp.engine.prompts import format_market_sentiment

        assert "不可用" in format_market_sentiment(None)

    def test_format_stock_identity(self):
        from aisp.engine.prompts import format_stock_identity

        profile = {"board_type": "gem", "liq_shares": 5.8e8}
        result = format_stock_identity(profile, pe_ttm=25.3, pb_mrq=3.2, close=50.0)
        assert "创业板" in result
        assert "流通市值290亿(中盘)" in result
        assert "PE(TTM)25.3" in result
        assert "PB3.2" in result

    def test_format_stock_identity_cap_tiers(self):
        from aisp.engine.prompts import format_stock_identity

        # 大盘: >= 1000亿
        p = {"board_type": "main_sh", "liq_shares": 10e8}
        assert "大盘" in format_stock_identity(p, None, None, close=120.0)
        # 中大盘: 300-1000亿
        p = {"board_type": "main_sz", "liq_shares": 5e8}
        assert "中大盘" in format_stock_identity(p, None, None, close=80.0)
        # 小盘: 30-100亿
        p = {"board_type": "gem", "liq_shares": 2e8}
        assert "小盘" in format_stock_identity(p, None, None, close=20.0)
        # 微盘: < 30亿
        p = {"board_type": "star", "liq_shares": 1e8}
        assert "微盘" in format_stock_identity(p, None, None, close=10.0)

    def test_format_fund_flow_detail(self):
        from aisp.engine.prompts import format_fund_flow_detail

        data = {
            "super_large_net": 1e8,
            "super_large_pct": 5.0,
            "large_net": -5e7,
            "large_pct": -2.5,
            "medium_net": 3e7,
            "medium_pct": 1.5,
            "small_net": -8e7,
            "small_pct": -4.0,
            "main_net": 5e7,
            "main_pct": 2.5,
        }
        result = format_fund_flow_detail(data)
        assert "超大单" in result
        assert "主力合计" in result
        assert "|" in result  # markdown table

    def test_format_fund_flow_empty(self):
        from aisp.engine.prompts import format_fund_flow_detail

        assert format_fund_flow_detail({}) == ""

    def test_format_position_info(self):
        from aisp.engine.prompts import format_position_info

        info = {
            "dist_60d_high_pct": -15.3,
            "dist_60d_low_pct": 8.2,
            "ytd_pct": -5.3,
            "turnover_5d": 12.5,
        }
        result = format_position_info(info)
        assert "60日高" in result
        assert "-15.3%" in result
        assert "YTD" in result

    def test_format_position_info_empty(self):
        from aisp.engine.prompts import format_position_info

        assert format_position_info(None) == ""
        assert format_position_info({}) == ""

    def test_format_lhb_info(self):
        from aisp.engine.prompts import format_lhb_info

        data = {"net_buy": 23450000, "reason": "涨幅偏离>7%"}
        result = format_lhb_info(data)
        assert "龙虎榜" in result
        assert "2345" in result
        assert "涨幅偏离" in result

    def test_format_lhb_info_empty(self):
        from aisp.engine.prompts import format_lhb_info

        assert format_lhb_info(None) == ""


# ── 9. _compute_position_info (pure function) ────────────────────────


class TestComputePositionInfo:
    def test_basic(self):
        from aisp.screening.stock_scorer import _compute_position_info

        closes = list(range(80, 101))  # 80..100, 21 bars
        turnovers = [3.0] * 21
        info = _compute_position_info(closes, turnovers)

        assert info["dist_60d_high_pct"] == 0.0  # current IS the high
        assert info["dist_60d_low_pct"] > 0  # above the low
        assert info["ytd_pct"] > 0  # went up
        assert info["turnover_5d"] == 15.0
        assert info["turnover_10d"] == 30.0
        assert info["turnover_20d"] == 60.0

    def test_empty(self):
        from aisp.screening.stock_scorer import _compute_position_info

        assert _compute_position_info([], []) == {}
        assert _compute_position_info([10], []) == {}

    def test_120d(self):
        from aisp.screening.stock_scorer import _compute_position_info

        closes = list(range(1, 121))  # 1..120
        info = _compute_position_info(closes, [])
        assert "dist_120d_high_pct" in info
        assert info["dist_120d_high_pct"] == 0.0  # last value is the high
