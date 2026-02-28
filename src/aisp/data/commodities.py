"""Commodity data fetcher using yfinance + AkShare."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from aisp.data import with_retry
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import AssetType, GlobalDaily

logger = logging.getLogger(__name__)

# International commodities via yfinance
YF_COMMODITIES: dict[str, tuple[str, AssetType]] = {
    "GC=F": ("Gold Futures", AssetType.COMMODITY),
    "HG=F": ("Copper Futures", AssetType.COMMODITY),
    "CL=F": ("Crude Oil WTI", AssetType.COMMODITY),
    "SI=F": ("Silver Futures", AssetType.COMMODITY),
}

# Domestic commodities via AkShare (symbol → display name)
AK_COMMODITIES: dict[str, str] = {
    "碳酸锂": "碳酸锂",
    "铁矿石": "铁矿石",
}


@with_retry(max_retries=3)
async def _fetch_yf_commodities(symbols: list[str], start: date, end: date) -> dict:
    """Fetch commodity data from yfinance."""
    import yfinance as yf

    def _dl():
        return yf.download(
            tickers=symbols,
            start=start.isoformat(),
            end=end.isoformat(),
            group_by="ticker",
            auto_adjust=True,
            progress=False,
        )

    return await asyncio.to_thread(_dl)


@with_retry(max_retries=3)
async def _fetch_ak_commodity(symbol: str) -> list[dict]:
    """Fetch a domestic commodity from AkShare."""
    import akshare as ak

    def _fetch():
        try:
            df = ak.futures_main_sina(symbol=symbol)
            if df is None or df.empty:
                return []
            return df.to_dict("records")
        except Exception:
            logger.warning("AkShare commodity %s not available, skipping", symbol)
            return []

    return await asyncio.to_thread(_fetch)


async def fetch_commodities(trade_date: date | None = None) -> int:
    """Fetch commodity data and upsert into global_daily.

    Returns the number of records upserted.
    """
    target_date = trade_date or date.today()
    start = target_date - timedelta(days=5)
    end = target_date + timedelta(days=1)

    records: list[dict] = []

    # 1. yfinance commodities
    yf_symbols = list(YF_COMMODITIES.keys())
    logger.info("Fetching yfinance commodities: %s", yf_symbols)

    try:
        data = await _fetch_yf_commodities(yf_symbols, start, end)
        if data is not None and not data.empty:
            for symbol, (name, asset_type) in YF_COMMODITIES.items():
                try:
                    if len(yf_symbols) > 1:
                        sym_data = (
                            data[symbol]
                            if symbol in data.columns.get_level_values(0)
                            else None
                        )
                    else:
                        sym_data = data

                    if sym_data is None or sym_data.empty:
                        continue

                    sym_data = sym_data.dropna(subset=["Close"])

                    for idx, row in sym_data.iterrows():
                        row_date = idx.date() if hasattr(idx, "date") else idx
                        if row_date > target_date:
                            continue

                        prev_close = None
                        row_idx = sym_data.index.get_loc(idx)
                        if row_idx > 0:
                            prev_close = float(sym_data.iloc[row_idx - 1]["Close"])

                        close_val = float(row["Close"])
                        change_pct = (
                            ((close_val - prev_close) / prev_close * 100)
                            if prev_close
                            else 0.0
                        )

                        records.append(
                            {
                                "trade_date": row_date,
                                "symbol": symbol,
                                "name": name,
                                "asset_type": asset_type.value,
                                "open": float(row["Open"]),
                                "high": float(row["High"]),
                                "low": float(row["Low"]),
                                "close": close_val,
                                "change_pct": round(change_pct, 4),
                                "volume": (
                                    float(row["Volume"])
                                    if row.get("Volume")
                                    else None
                                ),
                            }
                        )
                except Exception:
                    logger.exception("Error processing commodity %s", symbol)
    except Exception:
        logger.exception("Failed to fetch yfinance commodities")

    # 2. AkShare domestic commodities (best-effort)
    for ak_symbol, display_name in AK_COMMODITIES.items():
        try:
            rows = await _fetch_ak_commodity(ak_symbol)
            if not rows:
                continue
            # AkShare futures data has varying column names
            for row in rows[-5:]:  # only last 5 records
                try:
                    row_date_val = row.get("date") or row.get("日期")
                    if row_date_val is None:
                        continue
                    if hasattr(row_date_val, "date"):
                        row_date = row_date_val.date()
                    elif isinstance(row_date_val, str):
                        row_date = date.fromisoformat(row_date_val)
                    else:
                        row_date = row_date_val

                    if row_date > target_date:
                        continue

                    close_val = float(row.get("close") or row.get("收盘价", 0))
                    open_val = float(row.get("open") or row.get("开盘价", 0))
                    high_val = float(row.get("high") or row.get("最高价", 0))
                    low_val = float(row.get("low") or row.get("最低价", 0))
                    vol_val = float(row.get("volume") or row.get("成交量", 0))

                    records.append(
                        {
                            "trade_date": row_date,
                            "symbol": f"AK_{ak_symbol}",
                            "name": display_name,
                            "asset_type": AssetType.COMMODITY.value,
                            "open": open_val,
                            "high": high_val,
                            "low": low_val,
                            "close": close_val,
                            "change_pct": 0.0,
                            "volume": vol_val if vol_val else None,
                        }
                    )
                except Exception:
                    logger.exception("Error processing AkShare row for %s", ak_symbol)
        except Exception:
            logger.exception("Failed to fetch AkShare commodity %s", ak_symbol)

    if not records:
        logger.warning("No commodity records to insert")
        return 0

    # Upsert
    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        for rec in records:
            stmt = (
                sqlite_upsert(GlobalDaily)
                .values(**rec)
                .on_conflict_do_update(
                    index_elements=["trade_date", "symbol"],
                    set_={k: v for k, v in rec.items() if k not in ("trade_date", "symbol")},
                )
            )
            await session.execute(stmt)
        await session.commit()

    await engine.dispose()
    logger.info("Upserted %d commodity records", len(records))
    return len(records)
