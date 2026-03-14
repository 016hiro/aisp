"""Telegram message formatting — HTML mode."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def format_positions_message(data: dict) -> str:
    """Format extracted positions as Telegram HTML message."""
    positions = data.get("positions") or []
    confidence = data.get("confidence", 0)
    snapshot_date = data.get("snapshot_date", "?")

    lines = [
        f"<b>持仓识别结果</b>  ({len(positions)} 条)",
        f"日期: {snapshot_date}  置信度: {confidence:.0%}",
        "",
    ]

    for p in positions:
        code = p.get("code", "?")
        name = p.get("name", "?")
        qty = p.get("quantity", "?")
        cost = p.get("avg_cost")
        pl_pct = p.get("profit_loss_pct")
        cost_str = f"  成本:{cost:.2f}" if cost else ""
        pl_str = f"  盈亏:{pl_pct:+.2f}%" if pl_pct is not None else ""
        lines.append(f"<code>{code}</code> {name}  {qty}股{cost_str}{pl_str}")

    for w in data.get("warnings") or []:
        lines.append(f"\n{w}")

    return "\n".join(lines)


def format_trades_message(data: dict) -> str:
    """Format extracted trades as Telegram HTML message."""
    trades = data.get("trades") or []
    confidence = data.get("confidence", 0)

    lines = [
        f"<b>交割单识别结果</b>  ({len(trades)} 条)",
        f"置信度: {confidence:.0%}",
        "",
    ]

    for t in trades:
        code = t.get("code", "?")
        name = t.get("name", "?")
        direction = "买入" if t.get("trade_direction") == "buy" else "卖出"
        price = t.get("price")
        qty = t.get("quantity", "?")
        trade_date = t.get("trade_date", "?")
        price_str = f"@{price:.2f}" if price else ""
        lines.append(f"<code>{trade_date}</code> {direction} {code} {name} {qty}股{price_str}")

    for w in data.get("warnings") or []:
        lines.append(f"\n{w}")

    return "\n".join(lines)


def build_confirm_keyboard(
    data_type: str, current_date: str
) -> InlineKeyboardMarkup:
    """Build inline keyboard: [Confirm] [Cancel] [Change Date]."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Confirm", callback_data=f"confirm:{data_type}"),
            InlineKeyboardButton("Cancel", callback_data="cancel"),
        ],
        [
            InlineKeyboardButton(
                f"Change Date ({current_date})",
                callback_data=f"change_date:{data_type}",
            ),
        ],
    ])
