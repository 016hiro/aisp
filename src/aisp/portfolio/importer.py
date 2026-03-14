"""DB import for positions and trades — upsert via SQLite ON CONFLICT."""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

from aisp.db.engine import get_engine, get_session_factory
from aisp.db.models import (
    ImportSource,
    PositionSnapshot,
    TradeDirection,
    TradeRecord,
)

logger = logging.getLogger(__name__)


async def import_positions(data: dict, source: ImportSource = ImportSource.OCR) -> int:
    """Upsert position snapshots, return row count."""
    positions = data.get("positions") or []
    if not positions:
        return 0

    snapshot_date = date.fromisoformat(data["snapshot_date"])
    now = datetime.now()

    engine = get_engine()
    sf = get_session_factory(engine)

    count = 0
    async with sf() as session:
        for pos in positions:
            rec = {
                "snapshot_date": snapshot_date,
                "code": pos["code"],
                "name": pos.get("name", ""),
                "quantity": pos["quantity"],
                "available_quantity": pos.get("available_quantity"),
                "avg_cost": pos["avg_cost"],
                "current_price": pos.get("current_price"),
                "market_value": pos.get("market_value"),
                "profit_loss": pos.get("profit_loss"),
                "profit_loss_pct": pos.get("profit_loss_pct"),
                "today_profit_loss": pos.get("today_profit_loss"),
                "import_source": source,
                "imported_at": now,
            }
            stmt = (
                sqlite_upsert(PositionSnapshot)
                .values(**rec)
                .on_conflict_do_update(
                    index_elements=["snapshot_date", "code"],
                    set_={k: v for k, v in rec.items() if k not in ("snapshot_date", "code")},
                )
            )
            await session.execute(stmt)
            count += 1
        await session.commit()

    await engine.dispose()
    logger.info("Imported %d position snapshots for %s", count, snapshot_date)
    return count


async def import_trades(data: dict, source: ImportSource = ImportSource.OCR) -> int:
    """Upsert trade records, return row count."""
    trades = data.get("trades") or []
    if not trades:
        return 0

    now = datetime.now()

    engine = get_engine()
    sf = get_session_factory(engine)

    count = 0
    async with sf() as session:
        for trade in trades:
            rec = {
                "trade_date": date.fromisoformat(trade["trade_date"]),
                "code": trade["code"],
                "name": trade.get("name", ""),
                "trade_direction": TradeDirection(trade["trade_direction"]),
                "price": trade["price"],
                "quantity": trade["quantity"],
                "amount": trade["amount"],
                "commission": trade.get("commission"),
                "stamp_tax": trade.get("stamp_tax"),
                "transfer_fee": trade.get("transfer_fee"),
                "other_fees": trade.get("other_fees"),
                "total_cost": trade.get("total_cost"),
                "net_amount": trade.get("net_amount"),
                "settlement_date": (
                    date.fromisoformat(trade["settlement_date"])
                    if trade.get("settlement_date")
                    else None
                ),
                "import_source": source,
                "imported_at": now,
            }
            stmt = (
                sqlite_upsert(TradeRecord)
                .values(**rec)
                .on_conflict_do_update(
                    index_elements=[
                        "trade_date", "code", "trade_direction", "price", "quantity",
                    ],
                    set_={
                        k: v
                        for k, v in rec.items()
                        if k not in ("trade_date", "code", "trade_direction", "price", "quantity")
                    },
                )
            )
            await session.execute(stmt)
            count += 1
        await session.commit()

    await engine.dispose()
    logger.info("Imported %d trade records", count)
    return count
