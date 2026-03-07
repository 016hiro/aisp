"""US market data fetcher using yfinance."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from aisp.data import with_retry
from aisp.data.symbols import load_us_symbols
from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import AssetType, GlobalDaily

logger = logging.getLogger(__name__)

# Loaded from config/symbols.toml, can be overridden in tests via monkeypatch
US_SYMBOLS: dict[str, tuple[str, AssetType]] = load_us_symbols()


@with_retry(max_retries=3)
async def _download_yfinance(symbols: list[str], start: date, end: date) -> dict:
    """Download data from yfinance in a thread (it's synchronous)."""
    import yfinance as yf

    def _dl():
        data = yf.download(
            tickers=symbols,
            start=start.isoformat(),
            end=end.isoformat(),
            group_by="ticker",
            auto_adjust=True,
            progress=False,
        )
        return data

    return await asyncio.to_thread(_dl)


async def fetch_us_market(trade_date: date | None = None) -> int:
    """Fetch US market data and upsert into global_daily.

    Returns the number of records upserted.
    """
    target_date = trade_date or date.today()
    # yfinance needs start < end, and uses [start, end) range
    start = target_date - timedelta(days=5)
    end = target_date + timedelta(days=1)

    symbols = list(US_SYMBOLS.keys())
    logger.info("Fetching US market data for %s: %s", target_date, symbols)

    data = await _download_yfinance(symbols, start, end)

    if data is None or data.empty:
        logger.warning("No US market data returned for %s", target_date)
        return 0

    records: list[dict] = []

    for symbol, (name, asset_type) in US_SYMBOLS.items():
        try:
            if len(symbols) > 1:
                # Multi-ticker: data is multi-level columns (ticker, field)
                sym_data = data[symbol] if symbol in data.columns.get_level_values(0) else None
            else:
                sym_data = data

            if sym_data is None or sym_data.empty:
                logger.warning("No data for %s", symbol)
                continue

            # Get the latest row up to target_date
            sym_data = sym_data.dropna(subset=["Close"])
            if sym_data.empty:
                continue

            for idx, row in sym_data.iterrows():
                row_date = idx.date() if hasattr(idx, "date") else idx
                if row_date > target_date:
                    continue

                prev_close = None
                # Try to get previous close for change_pct
                row_idx = sym_data.index.get_loc(idx)
                if row_idx > 0:
                    prev_close = float(sym_data.iloc[row_idx - 1]["Close"])

                close_val = float(row["Close"])
                change_pct = (
                    ((close_val - prev_close) / prev_close * 100) if prev_close else 0.0
                )

                import pandas as pd

                vol = row.get("Volume")
                volume = float(vol) if vol is not None and pd.notna(vol) else None

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
                        "volume": volume,
                    }
                )
        except Exception:
            logger.exception("Error processing %s", symbol)

    if not records:
        logger.warning("No records to insert")
        return 0

    # Upsert into database
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
    logger.info("Upserted %d US market records", len(records))
    return len(records)
