"""Extended market data fetchers — market sentiment, fund flow, profile, quarterly, LHB, margin.

All functions follow the existing pattern:
- asyncio.to_thread() for blocking API calls
- @with_retry for transient failures
- sqlite_upsert for idempotent writes
- 500-row batch inserts
- Graceful degradation: API failure → warning + return 0
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import date, datetime

from sqlalchemy import update
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from aisp.data import with_retry
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import (
    MarketSentiment,
    StkDaily,
    StkLhb,
    StkMargin,
    StkProfile,
    StkQuarterly,
)

logger = logging.getLogger(__name__)


def _safe_float(val) -> float | None:
    if val is None or val == "" or val == "-":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _market_code(code: str) -> str:
    """Infer market prefix from code: 6/9→sh, others→sz."""
    return "sh" if code.startswith(("6", "9")) else "sz"


def _to_bs_code(code: str) -> str:
    return f"{_market_code(code)}.{code}"


# ── 1. Market Sentiment ─────────────────────────────────


def _fetch_market_sentiment_sync(trade_date: date) -> dict | None:
    """Fetch market-level sentiment indicators via AkShare."""
    import akshare as ak

    result: dict = {"trade_date": trade_date}
    date_str = trade_date.strftime("%Y%m%d")

    # Total market amount + activity (stock_market_activity_legu returns key-value pairs)
    with contextlib.suppress(Exception):
        df = ak.stock_market_activity_legu()
        if df is not None and not df.empty:
            kv = dict(zip(df.iloc[:, 0], df.iloc[:, 1], strict=False))
            # 成交额 may be in different formats
            for key in ("A股总成交额", "成交额", "total_amount"):
                val = kv.get(key)
                if val is not None:
                    # Parse "1.2万亿" or numeric
                    val_str = str(val).replace(",", "")
                    if "万亿" in val_str:
                        result["total_amount"] = _safe_float(val_str.replace("万亿", "")) * 10000
                    elif "亿" in val_str:
                        result["total_amount"] = _safe_float(val_str.replace("亿", ""))
                    else:
                        result["total_amount"] = _safe_float(val_str)
                    break
            activity = kv.get("活跃度")
            if activity is not None:
                val_str = str(activity).replace("%", "")
                result["activity_rate"] = _safe_float(val_str)

    # Limit up pool (涨停池)
    df_zt = None
    with contextlib.suppress(Exception):
        df_zt = ak.stock_zt_pool_em(date=date_str)
        if df_zt is not None and not df_zt.empty:
            result["limit_up_count"] = len(df_zt)
            if "连板数" in df_zt.columns:
                result["max_streak"] = int(df_zt["连板数"].max())

    # Limit down count (跌停)
    with contextlib.suppress(Exception):
        df_dt = ak.stock_zt_pool_dtgc_em(date=date_str)
        if df_dt is not None and not df_dt.empty:
            result["limit_down_count"] = len(df_dt)

    # Blast rate (炸板) and real limit up
    with contextlib.suppress(Exception):
        df_zb = ak.stock_zt_pool_zbgc_em(date=date_str)
        if df_zb is not None and not df_zb.empty:
            blast_count = len(df_zb)
            zt_count = result.get("limit_up_count", 0)
            result["real_limit_up"] = zt_count
            if zt_count + blast_count > 0:
                result["blast_rate"] = round(blast_count / (zt_count + blast_count) * 100, 1)

    # Previous day ZT premium (昨日涨停溢价)
    with contextlib.suppress(Exception):
        df_prev = ak.stock_zt_pool_previous_em(date=date_str)
        if df_prev is not None and not df_prev.empty and "涨跌幅" in df_prev.columns:
            result["prev_zt_premium"] = round(float(df_prev["涨跌幅"].mean()), 2)

    # Validate: at least some data was fetched
    if len(result) <= 1:  # only trade_date
        return None
    return result


@with_retry(max_retries=2)
async def fetch_market_sentiment(trade_date: date) -> int:
    """Fetch and store market sentiment data for a given date."""
    data = await asyncio.to_thread(_fetch_market_sentiment_sync, trade_date)
    if not data:
        logger.warning("No market sentiment data for %s", trade_date)
        return 0

    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        stmt = (
            sqlite_upsert(MarketSentiment)
            .values(**data)
            .on_conflict_do_update(
                index_elements=["trade_date"],
                set_={k: v for k, v in data.items() if k != "trade_date"},
            )
        )
        await session.execute(stmt)
        await session.commit()
    await engine.dispose()
    logger.info("Market sentiment stored for %s", trade_date)
    return 1


# ── 2. Individual Fund Flow ──────────────────────────────


def _fetch_fund_flow_one_sync(code: str) -> list[dict]:
    """Fetch individual stock fund flow history via AkShare."""
    import akshare as ak

    market = _market_code(code)
    stock_id = f"{code}"
    try:
        df = ak.stock_individual_fund_flow(stock=stock_id, market=market)
        if df is None or df.empty:
            return []
    except Exception:
        logger.debug("Fund flow fetch failed for %s", code)
        return []

    records = []
    for _, row in df.iterrows():
        trade_date_val = row.get("日期")
        if trade_date_val is None:
            continue
        if isinstance(trade_date_val, str):
            try:
                td = date.fromisoformat(trade_date_val[:10])
            except ValueError:
                continue
        else:
            td = trade_date_val.date() if hasattr(trade_date_val, "date") else trade_date_val

        records.append({
            "trade_date": td,
            "code": code,
            "main_net": _safe_float(row.get("主力净流入-净额")),
            "main_pct": _safe_float(row.get("主力净流入-净占比")),
            "super_large_net": _safe_float(row.get("超大单净流入-净额")),
            "super_large_pct": _safe_float(row.get("超大单净流入-净占比")),
            "large_net": _safe_float(row.get("大单净流入-净额")),
            "large_pct": _safe_float(row.get("大单净流入-净占比")),
            "medium_net": _safe_float(row.get("中单净流入-净额")),
            "medium_pct": _safe_float(row.get("中单净流入-净占比")),
            "small_net": _safe_float(row.get("小单净流入-净额")),
            "small_pct": _safe_float(row.get("小单净流入-净占比")),
        })
    return records


@with_retry(max_retries=2)
async def fetch_fund_flow(codes: list[str]) -> int:
    """Fetch fund flow data for given codes and update stk_daily rows."""
    total = 0
    engine = get_engine()
    session_factory = get_session_factory(engine)

    for i, code in enumerate(codes):
        records = await asyncio.to_thread(_fetch_fund_flow_one_sync, code)
        if not records:
            continue

        async with session_factory() as session:
            batch_size = 500
            for j in range(0, len(records), batch_size):
                batch = records[j : j + batch_size]
                for rec in batch:
                    fund_fields = {
                        k: v for k, v in rec.items()
                        if k not in ("trade_date", "code") and v is not None
                    }
                    if fund_fields:
                        await session.execute(
                            update(StkDaily)
                            .where(
                                StkDaily.trade_date == rec["trade_date"],
                                StkDaily.code == rec["code"],
                            )
                            .values(**fund_fields)
                        )
                await session.flush()
            await session.commit()
            total += len(records)

        # Rate limit between stocks
        if i < len(codes) - 1:
            await asyncio.sleep(0.3)

    await engine.dispose()
    logger.info("Updated fund flow for %d records across %d stocks", total, len(codes))
    return total


# ── 3. Stock Profile ─────────────────────────────────────


def _detect_board_type(code: str) -> str:
    """Detect board type from stock code prefix."""
    if code.startswith("30"):
        return "gem"  # 创业板
    if code.startswith("688"):
        return "star"  # 科创板
    if code.startswith("6"):
        return "main_sh"  # 沪市主板
    if code.startswith(("00", "001")):
        return "main_sz"  # 深市主板
    return "other"


def _fetch_stk_profile_sync(codes: list[str]) -> list[dict]:
    """Fetch stock profile data via BaoStock.

    query_stock_basic → code_name (1), ipoDate (2)
    query_profit_data → totalShare (9), liqaShare (10)
    """
    import baostock as bs

    login_result = bs.login()
    if login_result.error_code != "0":
        logger.error("BaoStock login failed: %s", login_result.error_msg)
        return []

    # Determine latest quarter for share count query
    now = datetime.now()
    quarter = (now.month - 1) // 3
    year = now.year
    if quarter == 0:
        year -= 1
        quarter = 4

    results = []
    try:
        for code in codes:
            bs_code = _to_bs_code(code)
            name = None
            listing_date_val = None
            total_shares = None
            liq_shares = None

            # query_stock_basic: fields=[code, code_name, ipoDate, outDate, type, status]
            with contextlib.suppress(Exception):
                rs = bs.query_stock_basic(code=bs_code)
                if rs.error_code == "0":
                    while rs.next():
                        row = rs.get_row_data()
                        name = row[1] if len(row) > 1 and row[1] else None
                        if len(row) > 2 and row[2]:
                            with contextlib.suppress(ValueError):
                                listing_date_val = date.fromisoformat(row[2])

            # query_profit_data: fields=[..., totalShare(9), liqaShare(10)]
            # Try current quarter first, fall back to previous
            for y, q in [(year, quarter), (year if quarter > 1 else year - 1, quarter - 1 if quarter > 1 else 4)]:
                with contextlib.suppress(Exception):
                    rs2 = bs.query_profit_data(code=bs_code, year=y, quarter=q)
                    if rs2.error_code == "0":
                        while rs2.next():
                            row2 = rs2.get_row_data()
                            if len(row2) > 10:
                                total_shares = _safe_float(row2[9])
                                liq_shares = _safe_float(row2[10])
                if total_shares is not None:
                    break

            results.append({
                "code": code,
                "name": name,
                "board_type": _detect_board_type(code),
                "total_shares": total_shares,
                "liq_shares": liq_shares,
                "listing_date": listing_date_val,
                "updated_at": datetime.now(),
            })
    finally:
        bs.logout()

    return results


@with_retry(max_retries=2)
async def fetch_stk_profile(codes: list[str]) -> int:
    """Fetch and store stock profile data."""
    records = await asyncio.to_thread(_fetch_stk_profile_sync, codes)
    if not records:
        return 0

    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        for rec in records:
            stmt = (
                sqlite_upsert(StkProfile)
                .values(**rec)
                .on_conflict_do_update(
                    index_elements=["code"],
                    set_={k: v for k, v in rec.items() if k != "code"},
                )
            )
            await session.execute(stmt)
        await session.commit()
    await engine.dispose()
    logger.info("Stored %d stock profiles", len(records))
    return len(records)


# ── 4. Quarterly Financials ──────────────────────────────


def _fetch_stk_quarterly_sync(codes: list[str], year: int, quarter: int) -> list[dict]:
    """Fetch quarterly financial data via BaoStock.

    query_profit_data fields:
      [0]code [1]pubDate [2]statDate [3]roeAvg [4]npMargin [5]gpMargin
      [6]netProfit [7]epsTTM [8]MBRevenue [9]totalShare [10]liqaShare

    query_growth_data fields:
      [0]code [1]pubDate [2]statDate [3]YOYEquity [4]YOYAsset
      [5]YOYNI [6]YOYEPSBasic [7]YOYPNI
    """
    import baostock as bs

    login_result = bs.login()
    if login_result.error_code != "0":
        logger.error("BaoStock login failed: %s", login_result.error_msg)
        return []

    results = []
    try:
        for code in codes:
            bs_code = _to_bs_code(code)

            # Profit data
            profit_data: dict = {}
            with contextlib.suppress(Exception):
                rs = bs.query_profit_data(code=bs_code, year=year, quarter=quarter)
                if rs.error_code == "0":
                    while rs.next():
                        row = rs.get_row_data()
                        if len(row) >= 7:
                            profit_data = {
                                "roe": _safe_float(row[3]),
                                "np_margin": _safe_float(row[4]),
                                "gp_margin": _safe_float(row[5]),
                                "net_profit": _safe_float(row[6]),
                                "eps_ttm": _safe_float(row[7]) if len(row) > 7 else None,
                                "pub_date": None,
                            }
                            if row[1]:
                                with contextlib.suppress(ValueError):
                                    profit_data["pub_date"] = date.fromisoformat(row[1])

            # Growth data
            growth_data: dict = {}
            with contextlib.suppress(Exception):
                rs2 = bs.query_growth_data(code=bs_code, year=year, quarter=quarter)
                if rs2.error_code == "0":
                    while rs2.next():
                        row2 = rs2.get_row_data()
                        if len(row2) >= 6:
                            growth_data = {
                                "yoy_equity": _safe_float(row2[3]),
                                "yoy_profit": _safe_float(row2[5]),
                                "yoy_eps": _safe_float(row2[6]) if len(row2) > 6 else None,
                            }

            if profit_data or growth_data:
                results.append({
                    "code": code,
                    "year": year,
                    "quarter": quarter,
                    **profit_data,
                    **growth_data,
                    "updated_at": datetime.now(),
                })
    finally:
        bs.logout()

    return results


@with_retry(max_retries=2)
async def fetch_stk_quarterly(codes: list[str], year: int, quarter: int) -> int:
    """Fetch and store quarterly financial data."""
    records = await asyncio.to_thread(_fetch_stk_quarterly_sync, codes, year, quarter)
    if not records:
        return 0

    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        for rec in records:
            stmt = (
                sqlite_upsert(StkQuarterly)
                .values(**rec)
                .on_conflict_do_update(
                    index_elements=["code", "year", "quarter"],
                    set_={k: v for k, v in rec.items() if k not in ("code", "year", "quarter")},
                )
            )
            await session.execute(stmt)
        await session.commit()
    await engine.dispose()
    logger.info("Stored %d quarterly records (Q%d %d)", len(records), quarter, year)
    return len(records)


# ── 5. LHB (龙虎榜) ─────────────────────────────────────


def _fetch_lhb_sync(trade_date: date) -> list[dict]:
    """Fetch LHB detail data via AkShare (东方财富 机构买卖明细)."""
    import akshare as ak

    date_str = trade_date.strftime("%Y%m%d")

    # Try stock_lhb_jgmmtj_em (机构买卖每日统计, reliable)
    try:
        df = ak.stock_lhb_jgmmtj_em(start_date=date_str, end_date=date_str)
        if df is None or df.empty:
            return []
    except Exception:
        logger.warning("LHB fetch failed for %s", trade_date)
        return []

    records = []
    for _, row in df.iterrows():
        code = str(row.get("代码", "")).zfill(6)
        buy = _safe_float(row.get("机构买入总额")) or 0
        sell = _safe_float(row.get("机构卖出总额")) or 0
        records.append({
            "trade_date": trade_date,
            "code": code,
            "name": str(row.get("名称", "")),
            "reason": str(row.get("上榜原因", ""))[:200],
            "net_buy": _safe_float(row.get("机构买入净额")),
            "buy_amount": buy,
            "sell_amount": sell,
            "turnover_rate": _safe_float(row.get("换手率")),
            "liq_market_cap": _safe_float(row.get("流通市值")),
            "post_1d": None,
            "post_2d": None,
            "post_5d": None,
            "post_10d": None,
        })

    return records


@with_retry(max_retries=2)
async def fetch_lhb(trade_date: date) -> int:
    """Fetch and store LHB data for a given date."""
    records = await asyncio.to_thread(_fetch_lhb_sync, trade_date)
    if not records:
        logger.info("No LHB data for %s", trade_date)
        return 0

    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        batch_size = 500
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            for rec in batch:
                stmt = (
                    sqlite_upsert(StkLhb)
                    .values(**rec)
                    .on_conflict_do_update(
                        index_elements=["trade_date", "code"],
                        set_={k: v for k, v in rec.items() if k not in ("trade_date", "code")},
                    )
                )
                await session.execute(stmt)
            await session.flush()
        await session.commit()
    await engine.dispose()
    logger.info("Stored %d LHB records for %s", len(records), trade_date)
    return len(records)


# ── 6. Margin (融资融券) ─────────────────────────────────


def _fetch_margin_sync(codes: list[str], trade_date: date) -> list[dict]:
    """Fetch margin trading data via EastMoney datacenter API.

    Supports both SSE and SZSE stocks in a single batch request.
    API: RPTA_WEB_RZRQ_GGMX, filtered by SCODE, sorted by DATE desc.
    When trade_date is given, fetches that single date's data (pageSize=len(codes)).
    """
    import httpx


    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    results = []

    # Fetch per-stock to avoid pageSize limits (each stock ~120 rows for 6 months)
    for code in codes:
        try:
            params = {
                "reportName": "RPTA_WEB_RZRQ_GGMX",
                "columns": "DATE,SCODE,RZYE,RZJME,RQYL,RQJMG,RZRQYE",
                "filter": f'(SCODE="{code}")',
                "pageSize": "200",
                "sortColumns": "DATE",
                "sortTypes": "-1",
                "source": "WEB",
                "client": "WEB",
            }
            r = httpx.get(url, params=params, timeout=15)
            data = r.json()
        except Exception:
            logger.debug("EastMoney margin API failed for %s", code)
            continue

        if not data.get("success"):
            continue

        for item in data.get("result", {}).get("data") or []:
            item_date_str = str(item.get("DATE", ""))[:10]
            try:
                item_date = date.fromisoformat(item_date_str)
            except ValueError:
                continue

            results.append({
                "trade_date": item_date,
                "code": str(item.get("SCODE", "")).zfill(6),
                "rzye": _safe_float(item.get("RZYE")),
                "rzjme": _safe_float(item.get("RZJME")),
                "rqyl": _safe_float(item.get("RQYL")),
                "rqjmg": _safe_float(item.get("RQJMG")),
                "rzrqye": _safe_float(item.get("RZRQYE")),
            })

    return results


@with_retry(max_retries=2)
async def fetch_margin(codes: list[str], trade_date: date) -> int:
    """Fetch and store margin data for given codes (batch, all available history)."""
    records = await asyncio.to_thread(_fetch_margin_sync, codes, trade_date)
    if not records:
        logger.info("No margin data")
        return 0

    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        batch_size = 500
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            for rec in batch:
                stmt = (
                    sqlite_upsert(StkMargin)
                    .values(**rec)
                    .on_conflict_do_update(
                        index_elements=["trade_date", "code"],
                        set_={k: v for k, v in rec.items() if k not in ("trade_date", "code")},
                    )
                )
                await session.execute(stmt)
            await session.flush()
        await session.commit()
    await engine.dispose()
    logger.info("Stored %d margin records for %s", len(records), trade_date)
    return len(records)
