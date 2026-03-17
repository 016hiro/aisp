"""A-share market data fetcher.

Individual stock data: BaoStock (TCP socket, bypasses HTTP proxy, fast batch).
Sector daily data: AkShare stock_board_industry_summary_ths (THS source).
Sector-stock mapping: pre-populated by scripts/scrape_ths_sectors.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from aisp.data import with_retry
from aisp.data.calendar import init_trading_calendar
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import SectorDaily, StkDaily, TradingCalendar

logger = logging.getLogger(__name__)


# ── Pure helper functions ─────────────────────────────────────────


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


def _safe_float(val) -> float | None:
    """Convert value to float, returning None for missing/invalid data."""
    if val is None or val == "-" or val == "":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _to_bs_code(code: str) -> str:
    """Convert plain code to BaoStock format: 600519 → sh.600519, 000001 → sz.000001."""
    if code.startswith(("6", "9")):
        return f"sh.{code}"
    return f"sz.{code}"


def _from_bs_code(bs_code: str) -> str:
    """Convert BaoStock code to plain code: sh.600519 → 600519."""
    return bs_code.split(".", 1)[-1] if "." in bs_code else bs_code


# ── BaoStock context manager ─────────────────────────────────────


@contextlib.contextmanager
def _bs_login_context():
    """BaoStock login/logout context manager."""
    import baostock as bs

    login_result = bs.login()
    if login_result.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login_result.error_msg}")
    try:
        yield bs
    finally:
        bs.logout()


# ── BaoStock data fetching (synchronous, run in background thread) ──


def _fetch_stock_name_map_sync() -> dict[str, str]:
    """Fetch code→name mapping for all A-share stocks via AkShare.

    Returns: {code: stock_name}
    """
    import akshare as ak

    try:
        df = ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            return dict(zip(df["code"].astype(str).str.zfill(6), df["name"], strict=False))
    except Exception:
        logger.warning("AkShare stock_info_a_code_name failed, falling back to BaoStock")

    # Fallback: BaoStock industry map (for name mapping only)
    import baostock as bs

    rs = bs.query_stock_industry()
    if rs.error_code != "0":
        return {}

    name_map: dict[str, str] = {}
    while rs.next():
        row = rs.get_row_data()
        if len(row) >= 3:
            code = _from_bs_code(row[1])
            name_map[code] = row[2]
    return name_map


def _fetch_all_codes_sync() -> list[str]:
    """Fetch all A-share stock codes via BaoStock industry classification.

    Used for full-market fetch mode.
    """
    import baostock as bs

    rs = bs.query_stock_industry()
    if rs.error_code != "0":
        logger.error("Failed to query stock industry: %s", rs.error_msg)
        return []

    codes: list[str] = []
    while rs.next():
        row = rs.get_row_data()
        if len(row) >= 2:
            codes.append(_from_bs_code(row[1]))
    return codes


def _fetch_stock_daily_sync(
    codes: list[str],
    trade_date: date,
    lookback_days: int = 50,
) -> list[dict]:
    """Fetch daily K-line data for given codes.

    Returns records for ALL dates in the lookback range (for historical indicator
    computation). volume_ratio is computed for all rows (today_vol / avg of prev 5 days).
    """
    import baostock as bs

    start_str = (trade_date - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end_str = trade_date.strftime("%Y-%m-%d")
    fields = "date,code,open,high,low,close,preclose,volume,amount,pctChg,turn,isST,peTTM,pbMRQ"

    results: list[dict] = []
    total = len(codes)

    for i, code in enumerate(codes):
        if (i + 1) % 500 == 0:
            logger.info("Fetching stock data: %d/%d", i + 1, total)

        bs_code = _to_bs_code(code)
        try:
            rs = bs.query_history_k_data_plus(
                bs_code,
                fields,
                start_date=start_str,
                end_date=end_str,
                frequency="d",
                adjustflag="3",  # 不复权
            )
            if rs.error_code != "0":
                continue

            rows: list[list[str]] = []
            while rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                continue

            # Collect all volumes for volume_ratio computation on target date
            all_volumes: list[tuple[str, float]] = []
            for row in rows:
                vol = _safe_float(row[7])
                if vol is not None and vol > 0:
                    all_volumes.append((row[0], vol))

            # Return ALL rows so historical closes are stored in DB
            for row in rows:
                close = _safe_float(row[5]) or 0.0
                if not close or close <= 0:
                    continue

                row_date_str = row[0]
                change_pct = _safe_float(row[9]) or 0.0
                is_st_flag = row[11] == "1" if len(row) > 11 else False
                st_name = "ST" if is_st_flag else ""

                # volume_ratio: today_vol / avg of previous 5 trading days
                volume_ratio = None
                today_vol = _safe_float(row[7])
                prev_vols = [v for d, v in all_volumes if d < row_date_str]
                if today_vol and prev_vols:
                    recent = prev_vols[-5:]
                    avg_vol = sum(recent) / len(recent)
                    if avg_vol > 0:
                        volume_ratio = today_vol / avg_vol

                results.append({
                    "code": _from_bs_code(row[1]),
                    "name": "",
                    "trade_date": date.fromisoformat(row_date_str),
                    "open": _safe_float(row[2]) or 0.0,
                    "high": _safe_float(row[3]) or 0.0,
                    "low": _safe_float(row[4]) or 0.0,
                    "close": close,
                    "volume": _safe_float(row[7]) or 0.0,
                    "amount": _safe_float(row[8]) or 0.0,
                    "change_pct": change_pct,
                    "turnover_rate": _safe_float(row[10]),
                    "volume_ratio": volume_ratio,
                    "net_inflow": None,
                    "market_cap": None,
                    "pe_ttm": _safe_float(row[12]) if len(row) > 12 else None,
                    "pb_mrq": _safe_float(row[13]) if len(row) > 13 else None,
                    "is_st": is_st_flag,
                    "is_limit_up": _is_limit_up(change_pct, st_name),
                    "is_limit_down": _is_limit_down(change_pct, st_name),
                })
        except Exception:
            logger.debug("Failed to fetch data for %s", code)

    return results


@with_retry(max_retries=2)
async def _fetch_bs_data(
    trade_date: date,
    codes: list[str] | None = None,
) -> tuple[dict[str, str], list[dict]]:
    """Run BaoStock queries in a background thread.

    Args:
        trade_date: Target trading date.
        codes: If provided, only fetch these stock codes (skip full market scan).

    Returns (name_map, stock_daily_records).
    """

    def _sync():
        with _bs_login_context():
            name_map = _fetch_stock_name_map_sync()
            fetch_codes = codes if codes is not None else _fetch_all_codes_sync()
            logger.info(
                "Name map: %d stocks, fetching %d stocks from BaoStock",
                len(name_map),
                len(fetch_codes),
            )
            stock_data = _fetch_stock_daily_sync(fetch_codes, trade_date)
            return name_map, stock_data

    return await asyncio.to_thread(_sync)


# ── THS sector daily data ────────────────────────────────────────


@with_retry(max_retries=2)
async def _fetch_ths_sector_daily() -> list[dict]:
    """Fetch THS industry board daily summary via AkShare.

    Returns list of dicts ready for sector_daily upsert (without trade_date/close/MAs).
    """
    import akshare as ak

    def _sync():
        df = ak.stock_board_industry_summary_ths()
        if df is None or df.empty:
            return []

        results = []
        for _, row in df.iterrows():
            results.append({
                "sector_name": row["板块"],
                "change_pct": float(row["涨跌幅"]),
                "volume": float(row["总成交量"]),
                "amount": float(row["总成交额"]),
                "net_inflow": float(row["净流入"]) if row["净流入"] else None,
                "stock_count": int(row["上涨家数"]) + int(row["下跌家数"]),
                "up_count": int(row["上涨家数"]),
                "down_count": int(row["下跌家数"]),
            })
        return results

    return await asyncio.to_thread(_sync)


# ── DB upsert helpers (unchanged) ────────────────────────────────


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


def _compute_ma_from_closes(closes: list[float]) -> dict[str, float | None]:
    """Compute moving averages from a list of closes (most recent first)."""
    mas: dict[str, float | None] = {}
    for period, key in [(5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60")]:
        if len(closes) >= period:
            mas[key] = sum(closes[:period]) / period
        else:
            mas[key] = None
    return mas


async def _compute_and_update_sector_mas(
    sector_names: list[str], trade_date: date
) -> None:
    """Compute and update moving averages for all sectors in a single session."""
    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        for sector_name in sector_names:
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
            mas = _compute_ma_from_closes(closes)

            if any(v is not None for v in mas.values()):
                await session.execute(
                    update(SectorDaily)
                    .where(
                        SectorDaily.trade_date == trade_date,
                        SectorDaily.sector_name == sector_name,
                    )
                    .values(**mas)
                )
        await session.commit()
    await engine.dispose()


# ── Main entry point ─────────────────────────────────────────────


async def fetch_cn_market(
    trade_date: date | None = None,
    *,
    codes: list[str] | None = None,
) -> dict:
    """Fetch A-share market data.

    Individual stocks: BaoStock (TCP, fast batch, proxy-immune).
    Sector daily: AkShare stock_board_industry_summary_ths (THS source).

    Args:
        trade_date: Target date, defaults to today.
        codes: If provided, only fetch these stock codes instead of full market.

    Returns a summary dict with counts.
    """
    target_date = trade_date or date.today()
    result = {"stocks": 0, "sectors": 0, "calendar": 0}

    # 1. Ensure trading calendar is populated
    logger.info("Checking trading calendar...")
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

    # 2. Fetch individual stock data from BaoStock
    mode_label = f"{len(codes)} codes" if codes else "full market"
    logger.info("Fetching A-share stock data from BaoStock (%s)...", mode_label)
    name_map, stock_data = await _fetch_bs_data(target_date, codes=codes)

    if not name_map:
        logger.warning("No stock name map available, stock names may be empty")

    stock_records: list[dict] = []
    for rec in stock_data:
        code = rec["code"]
        name = name_map.get(code, "")

        close = rec["close"]
        if not close or close <= 0:
            continue

        rec["name"] = name
        if not rec["is_st"]:
            rec["is_st"] = _is_st(name)
        rec["is_limit_up"] = _is_limit_up(rec["change_pct"], name)
        rec["is_limit_down"] = _is_limit_down(rec["change_pct"], name)
        # trade_date already set per-row by _fetch_stock_daily_sync
        stock_records.append(rec)

    result["stocks"] = await _upsert_stocks(stock_records, target_date)
    logger.info("Upserted %d stock records", result["stocks"])

    # 3. Fetch THS sector daily summary (independent of stock data)
    logger.info("Fetching THS sector daily summary...")
    sector_aggregated = await _fetch_ths_sector_daily()

    sector_names: list[str] = []
    if sector_aggregated:
        # Compute running sector close from previous DB values
        engine = get_engine()
        session_factory = get_session_factory(engine)
        async with session_factory() as session:
            subq = (
                select(
                    SectorDaily.sector_name,
                    func.max(SectorDaily.trade_date).label("max_date"),
                )
                .where(SectorDaily.trade_date < target_date)
                .group_by(SectorDaily.sector_name)
                .subquery()
            )
            prev_result = await session.execute(
                select(SectorDaily.sector_name, SectorDaily.close).join(
                    subq,
                    (SectorDaily.sector_name == subq.c.sector_name)
                    & (SectorDaily.trade_date == subq.c.max_date),
                )
            )
            prev_closes = {row[0]: row[1] for row in prev_result.all()}
        await engine.dispose()

        sector_records: list[dict] = []
        for rec in sector_aggregated:
            sname = rec["sector_name"]
            sector_names.append(sname)

            prev = prev_closes.get(sname)
            if prev and prev > 0:
                rec["close"] = round(prev * (1 + rec["change_pct"] / 100.0), 2)
            else:
                rec["close"] = 1000.0  # Initial value for new sectors

            rec["trade_date"] = target_date
            rec["ma5"] = None
            rec["ma10"] = None
            rec["ma20"] = None
            rec["ma60"] = None
            sector_records.append(rec)

        result["sectors"] = await _upsert_sectors(sector_records)
        logger.info("Upserted %d sector records", result["sectors"])

    # 4. Compute MAs for sectors (after data is in DB)
    if sector_names:
        logger.info("Computing sector moving averages...")
        await _compute_and_update_sector_mas(sector_names, target_date)

    return result
