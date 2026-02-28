"""A-share market data fetcher using AkShare."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from aisp.data import with_retry
from aisp.data.calendar import init_trading_calendar
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import SectorDaily, StkDaily, StkSectorMap

logger = logging.getLogger(__name__)

# AkShare column name → our model field mappings
STK_COLUMN_MAP = {
    "代码": "code",
    "名称": "name",
    "今开": "open",
    "最高": "high",
    "最低": "low",
    "最新价": "close",
    "成交量": "volume",
    "成交额": "amount",
    "涨跌幅": "change_pct",
    "换手率": "turnover_rate",
    "量比": "volume_ratio",
    "流通市值": "market_cap",
}


def _is_st(name: str) -> bool:
    return "ST" in name.upper() if name else False


def _is_limit_up(change_pct: float, name: str) -> bool:
    """Check if a stock hit the daily limit up."""
    if _is_st(name):
        return change_pct >= 4.9  # ST stocks have 5% limit
    return change_pct >= 9.9  # Normal stocks have 10% limit


def _is_limit_down(change_pct: float, name: str) -> bool:
    if _is_st(name):
        return change_pct <= -4.9
    return change_pct <= -9.9


@with_retry(max_retries=3)
async def _fetch_stock_spot() -> list[dict]:
    """Fetch all A-share stock spot data."""
    import akshare as ak

    def _fetch():
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            return []
        return df.to_dict("records")

    return await asyncio.to_thread(_fetch)


@with_retry(max_retries=3)
async def _fetch_sector_list() -> list[dict]:
    """Fetch industry sector ranking."""
    import akshare as ak

    def _fetch():
        df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            return []
        return df.to_dict("records")

    return await asyncio.to_thread(_fetch)


@with_retry(max_retries=3)
async def _fetch_sector_constituents(sector_name: str) -> list[dict]:
    """Fetch constituent stocks of a sector."""
    import akshare as ak

    def _fetch():
        try:
            df = ak.stock_board_industry_cons_em(symbol=sector_name)
            if df is None or df.empty:
                return []
            return df.to_dict("records")
        except Exception:
            logger.warning("Failed to fetch constituents for sector: %s", sector_name)
            return []

    return await asyncio.to_thread(_fetch)


@with_retry(max_retries=3)
async def _fetch_fund_flow() -> list[dict]:
    """Fetch today's main fund flow ranking. Gracefully degrades on failure."""
    import akshare as ak

    def _fetch():
        try:
            df = ak.stock_individual_fund_flow_rank(indicator="今日")
            if df is None or df.empty:
                return []
            return df.to_dict("records")
        except Exception:
            logger.warning("Fund flow data not available, degrading gracefully")
            return []

    return await asyncio.to_thread(_fetch)


async def _upsert_stocks(records: list[dict], trade_date: date) -> int:
    """Batch upsert stock records into stk_daily."""
    if not records:
        return 0

    engine = get_engine()
    session_factory = get_session_factory(engine)
    count = 0
    async with session_factory() as session:
        batch_size = 500
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            for rec in batch:
                stmt = (
                    sqlite_upsert(StkDaily)
                    .values(**rec)
                    .on_conflict_do_update(
                        index_elements=["trade_date", "code"],
                        set_={
                            k: v
                            for k, v in rec.items()
                            if k not in ("trade_date", "code")
                        },
                    )
                )
                await session.execute(stmt)
            await session.flush()
            count += len(batch)
        await session.commit()
    await engine.dispose()
    return count


async def _upsert_sectors(records: list[dict]) -> int:
    """Batch upsert sector records into sector_daily."""
    if not records:
        return 0

    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        for rec in records:
            stmt = (
                sqlite_upsert(SectorDaily)
                .values(**rec)
                .on_conflict_do_update(
                    index_elements=["trade_date", "sector_name"],
                    set_={
                        k: v
                        for k, v in rec.items()
                        if k not in ("trade_date", "sector_name")
                    },
                )
            )
            await session.execute(stmt)
        await session.commit()
    await engine.dispose()
    return len(records)


async def _compute_sector_ma(sector_name: str, trade_date: date) -> dict[str, float | None]:
    """Compute moving averages for a sector from historical data."""
    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        result = await session.execute(
            select(SectorDaily.close)
            .where(
                SectorDaily.sector_name == sector_name,
                SectorDaily.trade_date <= trade_date,
            )
            .order_by(SectorDaily.trade_date.desc())
            .limit(60)
        )
        closes = [row[0] for row in result.all()]
    await engine.dispose()

    mas = {}
    for period, key in [(5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60")]:
        if len(closes) >= period:
            mas[key] = sum(closes[:period]) / period
        else:
            mas[key] = None
    return mas


async def _update_sector_maps(sector_constituents: dict[str, list[dict]]) -> int:
    """Update stk_sector_map with current sector memberships."""
    if not sector_constituents:
        return 0

    engine = get_engine()
    session_factory = get_session_factory(engine)
    now = datetime.now()
    count = 0

    async with session_factory() as session:
        for sector_name, constituents in sector_constituents.items():
            for stock in constituents:
                code = stock.get("代码", "")
                if not code:
                    continue

                rec = {
                    "code": code,
                    "sector_name": sector_name,
                    "source": "ths",
                    "is_active": True,
                    "updated_at": now,
                }
                stmt = (
                    sqlite_upsert(StkSectorMap)
                    .values(**rec)
                    .on_conflict_do_update(
                        index_elements=["code", "sector_name", "source"],
                        set_={"is_active": True, "updated_at": now},
                    )
                )
                await session.execute(stmt)
                count += 1

            # Mark stocks no longer in this sector as inactive
            active_codes = [s.get("代码", "") for s in constituents if s.get("代码")]
            if active_codes:
                from sqlalchemy import update

                await session.execute(
                    update(StkSectorMap)
                    .where(
                        StkSectorMap.sector_name == sector_name,
                        StkSectorMap.source == "ths",
                        StkSectorMap.code.not_in(active_codes),
                    )
                    .values(is_active=False, updated_at=now)
                )

        await session.commit()
    await engine.dispose()
    return count


async def fetch_cn_market(trade_date: date | None = None) -> dict:
    """Fetch A-share market data: stocks, sectors, fund flow, calendar.

    Returns a summary dict with counts.
    """
    target_date = trade_date or date.today()
    result = {"stocks": 0, "sectors": 0, "sector_maps": 0, "calendar": 0}

    # 1. Initialize trading calendar (first run only if empty)
    logger.info("Checking trading calendar...")
    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        await session.scalar(
            select(func.count()).select_from(
                select(1)
                .select_from(StkDaily)
                .limit(1)
                .correlate(None)
                .subquery()
            )
        )
    await engine.dispose()

    # Always ensure calendar is populated
    from aisp.db.models import TradingCalendar
    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        cal_exists = await session.scalar(
            select(func.count()).select_from(TradingCalendar)
        )
    await engine.dispose()

    if not cal_exists:
        logger.info("Initializing trading calendar...")
        result["calendar"] = await init_trading_calendar()

    # 2. Fetch stock spot data
    logger.info("Fetching A-share stock data...")
    spot_data = await _fetch_stock_spot()

    # 3. Fetch fund flow (best-effort)
    logger.info("Fetching fund flow data...")
    fund_flow_data = await _fetch_fund_flow()
    fund_flow_map: dict[str, float] = {}
    for row in fund_flow_data:
        code = row.get("代码", "")
        inflow = row.get("主力净流入-净额")
        if code and inflow is not None:
            with contextlib.suppress(ValueError, TypeError):
                fund_flow_map[code] = float(inflow)

    # 4. Transform and upsert stock records
    stock_records: list[dict] = []
    for row in spot_data:
        try:
            code = str(row.get("代码", ""))
            name = str(row.get("名称", ""))
            if not code or not name:
                continue

            close_val = row.get("最新价")
            if close_val is None or close_val == "-":
                continue

            change_pct = float(row.get("涨跌幅", 0) or 0)

            stock_records.append(
                {
                    "trade_date": target_date,
                    "code": code,
                    "name": name,
                    "open": float(row.get("今开", 0) or 0),
                    "high": float(row.get("最高", 0) or 0),
                    "low": float(row.get("最低", 0) or 0),
                    "close": float(close_val),
                    "volume": float(row.get("成交量", 0) or 0),
                    "amount": float(row.get("成交额", 0) or 0),
                    "change_pct": change_pct,
                    "turnover_rate": _safe_float(row.get("换手率")),
                    "volume_ratio": _safe_float(row.get("量比")),
                    "net_inflow": fund_flow_map.get(code),
                    "market_cap": _safe_float(row.get("流通市值")),
                    "is_st": _is_st(name),
                    "is_limit_up": _is_limit_up(change_pct, name),
                    "is_limit_down": _is_limit_down(change_pct, name),
                }
            )
        except Exception:
            logger.exception("Error processing stock row: %s", row.get("代码"))

    result["stocks"] = await _upsert_stocks(stock_records, target_date)
    logger.info("Upserted %d stock records", result["stocks"])

    # 5. Fetch sector data
    logger.info("Fetching sector data...")
    sector_data = await _fetch_sector_list()

    sector_records: list[dict] = []
    sector_names: list[str] = []
    for row in sector_data:
        try:
            sector_name = str(row.get("板块名称", ""))
            if not sector_name:
                continue
            sector_names.append(sector_name)

            rec = {
                "trade_date": target_date,
                "sector_name": sector_name,
                "close": float(row.get("最新价", 0) or 0),
                "change_pct": float(row.get("涨跌幅", 0) or 0),
                "volume": float(row.get("总成交量", 0) or 0),
                "amount": float(row.get("总成交额", 0) or 0),
                "net_inflow": _safe_float(row.get("主力净流入")),
                "stock_count": int(row.get("股票数", 0) or 0),
                "up_count": int(row.get("上涨家数", 0) or 0),
                "down_count": int(row.get("下跌家数", 0) or 0),
                "ma5": None,
                "ma10": None,
                "ma20": None,
                "ma60": None,
            }
            sector_records.append(rec)
        except Exception:
            logger.exception("Error processing sector row: %s", row.get("板块名称"))

    result["sectors"] = await _upsert_sectors(sector_records)
    logger.info("Upserted %d sector records", result["sectors"])

    # 6. Compute MAs for sectors (after data is in DB)
    logger.info("Computing sector moving averages...")
    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        for sector_name in sector_names:
            mas = await _compute_sector_ma(sector_name, target_date)
            if any(v is not None for v in mas.values()):
                from sqlalchemy import update

                await session.execute(
                    update(SectorDaily)
                    .where(
                        SectorDaily.trade_date == target_date,
                        SectorDaily.sector_name == sector_name,
                    )
                    .values(**mas)
                )
        await session.commit()
    await engine.dispose()

    # 7. Fetch sector constituents (top sectors only to limit API calls)
    logger.info("Fetching sector constituents...")
    top_sectors = sector_names[:30]  # Limit to top 30 sectors
    sector_constituents: dict[str, list[dict]] = {}
    for sector_name in top_sectors:
        try:
            constituents = await _fetch_sector_constituents(sector_name)
            if constituents:
                sector_constituents[sector_name] = constituents
        except Exception:
            logger.warning("Failed to get constituents for %s", sector_name)

    result["sector_maps"] = await _update_sector_maps(sector_constituents)
    logger.info("Updated %d sector mappings", result["sector_maps"])

    return result


def _safe_float(val) -> float | None:
    """Convert value to float, returning None for missing/invalid data."""
    if val is None or val == "-" or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
