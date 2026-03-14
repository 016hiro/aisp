"""Telegram Bot — screenshot OCR import for positions/trades.

Flow:
  /positions → send photos → /done → OCR → [Confirm] [Cancel] [Change Date] → DB
  /trades    → send photos → /done → OCR → [Confirm] [Cancel] [Change Date] → DB
  Duplicate images auto-skipped via SHA256 dedup.
"""

from __future__ import annotations

import logging
from datetime import date

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from aisp.config import get_settings
from aisp.telegram.dedup import check_duplicate, compute_hash, record_hash
from aisp.telegram.formatter import (
    build_confirm_keyboard,
    format_positions_message,
    format_trades_message,
)

logger = logging.getLogger(__name__)

# Conversation states
WAITING_PHOTOS, CONFIRMING, CHANGING_DATE = range(3)

# MIME type for Telegram photos (always JPEG)
_TG_PHOTO_MIME = "image/jpeg"

# File extension → MIME
_EXT_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _user_filter() -> filters.User | filters.ALL:
    """Build user filter from config. If no IDs configured, allow all."""
    settings = get_settings()
    user_ids = settings.telegram.allowed_user_ids
    if user_ids:
        return filters.User(user_id=user_ids)
    return filters.ALL


# ── Handlers ──────────────────────────────────────────


async def _start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logger.info("User %s (id=%d) sent /start", user.username or user.first_name, user.id)
    await update.message.reply_text(
        f"A-ISP Telegram Bot\n\n"
        f"Your user ID: <code>{user.id}</code>\n\n"
        "Commands:\n"
        "/positions — Import positions from screenshots\n"
        "/trades — Import trades from screenshots\n"
        "\nSend screenshots after choosing a mode, then /done to process.",
        parse_mode="HTML",
    )


async def _begin_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["mode"] = "positions"
    context.user_data["images"] = []
    context.user_data["hashes"] = []
    await update.message.reply_text(
        "Position import mode. Send screenshots now.\n"
        "When done, send /done to start OCR processing.\n"
        "Send /cancel to abort."
    )
    return WAITING_PHOTOS


async def _begin_trades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["mode"] = "trades"
    context.user_data["images"] = []
    context.user_data["hashes"] = []
    await update.message.reply_text(
        "Trade import mode. Send screenshots now.\n"
        "When done, send /done to start OCR processing.\n"
        "Send /cancel to abort."
    )
    return WAITING_PHOTOS


async def _receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle incoming photo (compressed) — download highest resolution."""
    photo = update.message.photo[-1]  # highest resolution
    file = await photo.get_file()
    image_bytes = await file.download_as_bytearray()
    image_bytes = bytes(image_bytes)

    # SHA256 dedup
    h = compute_hash(image_bytes)
    try:
        existing = await check_duplicate(h)
    except Exception:
        logger.exception("Dedup check failed, treating as new image")
        existing = None
    if existing:
        await update.message.reply_text(
            f"Duplicate image skipped (processed {existing.processed_at:%Y-%m-%d %H:%M})."
        )
        return WAITING_PHOTOS

    if h in context.user_data.get("hashes", []):
        await update.message.reply_text("Duplicate image in this batch, skipped.")
        return WAITING_PHOTOS

    context.user_data["images"].append((image_bytes, _TG_PHOTO_MIME))
    context.user_data["hashes"].append(h)
    count = len(context.user_data["images"])
    await update.message.reply_text(f"Received ({count} image{'s' if count > 1 else ''}).")
    return WAITING_PHOTOS


async def _receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle incoming document (uncompressed image file)."""
    doc = update.message.document
    file_name = (doc.file_name or "").lower()
    ext = ""
    for e in _EXT_MIME:
        if file_name.endswith(e):
            ext = e
            break

    if not ext:
        await update.message.reply_text("Unsupported file type. Send PNG/JPG/WEBP images.")
        return WAITING_PHOTOS

    mime = _EXT_MIME[ext]
    file = await doc.get_file()
    image_bytes = await file.download_as_bytearray()
    image_bytes = bytes(image_bytes)

    # SHA256 dedup
    h = compute_hash(image_bytes)
    try:
        existing = await check_duplicate(h)
    except Exception:
        logger.exception("Dedup check failed, treating as new image")
        existing = None
    if existing:
        await update.message.reply_text(
            f"Duplicate image skipped (processed {existing.processed_at:%Y-%m-%d %H:%M})."
        )
        return WAITING_PHOTOS

    if h in context.user_data.get("hashes", []):
        await update.message.reply_text("Duplicate image in this batch, skipped.")
        return WAITING_PHOTOS

    context.user_data["images"].append((image_bytes, mime))
    context.user_data["hashes"].append(h)
    count = len(context.user_data["images"])
    await update.message.reply_text(f"Received ({count} image{'s' if count > 1 else ''}).")
    return WAITING_PHOTOS


async def _done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process collected images with OCR."""
    images = context.user_data.get("images") or []
    if not images:
        await update.message.reply_text("No images received. Send screenshots first.")
        return WAITING_PHOTOS

    mode = context.user_data.get("mode", "positions")
    await update.message.reply_text(
        f"Processing {len(images)} image{'s' if len(images) > 1 else ''} with OCR..."
    )

    settings = get_settings()
    model = settings.openrouter.analysis_model

    try:
        if mode == "positions":
            from aisp.portfolio.ocr import extract_positions_from_bytes

            data = await extract_positions_from_bytes(images, model_override=model)
            items = data.get("positions") or []
        else:
            from aisp.portfolio.ocr import extract_trades_from_bytes

            data = await extract_trades_from_bytes(images, model_override=model)
            items = data.get("trades") or []
    except Exception:
        logger.exception("OCR failed")
        await update.message.reply_text("OCR processing failed. Please try again.")
        return ConversationHandler.END

    if not items:
        await update.message.reply_text("No data extracted from screenshots.")
        return ConversationHandler.END

    # Store OCR result for confirmation
    context.user_data["ocr_data"] = data

    # Format preview
    if mode == "positions":
        snapshot_date = data.get("snapshot_date", date.today().isoformat())
        msg = format_positions_message(data)
        keyboard = build_confirm_keyboard("positions", snapshot_date)
    else:
        # Use first trade's date or today
        trades = data.get("trades") or []
        first_date = trades[0].get("trade_date", date.today().isoformat()) if trades else ""
        msg = format_trades_message(data)
        keyboard = build_confirm_keyboard("trades", first_date)

    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)
    return CONFIRMING


async def _callback_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle confirm button — write to DB."""
    query = update.callback_query
    await query.answer()

    data_type = query.data.split(":", 1)[1]  # "positions" or "trades"
    ocr_data = context.user_data.get("ocr_data")
    if not ocr_data:
        await query.edit_message_text("No data to import. Session expired.")
        return ConversationHandler.END

    from aisp.db.models import ImportSource
    from aisp.portfolio.importer import import_positions, import_trades

    try:
        if data_type == "positions":
            count = await import_positions(ocr_data, source=ImportSource.TELEGRAM)
        else:
            count = await import_trades(ocr_data, source=ImportSource.TELEGRAM)
    except Exception:
        logger.exception("DB import failed")
        await query.edit_message_text("Import failed. Please try again.")
        return ConversationHandler.END

    # Record image hashes
    hashes = context.user_data.get("hashes") or []
    summary = f"{data_type}: {count} records"
    for h in hashes:
        try:
            await record_hash(h, data_type, summary)
        except Exception:
            logger.warning("Failed to record hash %s", h[:8])

    await query.edit_message_text(f"Imported {count} {data_type} records.")
    _clear_user_data(context)
    return ConversationHandler.END


async def _callback_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle cancel button."""
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Cancelled.")
    _clear_user_data(context)
    return ConversationHandler.END


async def _callback_change_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle change date button — prompt for new date."""
    query = update.callback_query
    await query.answer()
    context.user_data["change_date_type"] = query.data.split(":", 1)[1]
    await query.edit_message_text("Send the new date (YYYY-MM-DD):")
    return CHANGING_DATE


async def _receive_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle new date input."""
    text = update.message.text.strip()
    try:
        new_date = date.fromisoformat(text)
    except ValueError:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD:")
        return CHANGING_DATE

    ocr_data = context.user_data.get("ocr_data", {})
    data_type = context.user_data.get("change_date_type", "positions")
    new_date_str = new_date.isoformat()

    if data_type == "positions":
        ocr_data["snapshot_date"] = new_date_str
        msg = format_positions_message(ocr_data)
        keyboard = build_confirm_keyboard("positions", new_date_str)
    else:
        for t in ocr_data.get("trades") or []:
            t["trade_date"] = new_date_str
        msg = format_trades_message(ocr_data)
        keyboard = build_confirm_keyboard("trades", new_date_str)

    context.user_data["ocr_data"] = ocr_data
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=keyboard)
    return CONFIRMING


async def _cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /cancel command."""
    await update.message.reply_text("Cancelled.")
    _clear_user_data(context)
    return ConversationHandler.END


def _clear_user_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    for key in ("mode", "images", "hashes", "ocr_data", "change_date_type"):
        context.user_data.pop(key, None)


# ── Application builder ──────────────────────────────


def create_application() -> Application:
    """Build and return the Telegram Application (not yet running)."""
    settings = get_settings()
    token = settings.telegram.bot_token
    if not token:
        raise RuntimeError(
            "AISP_TELEGRAM__BOT_TOKEN not set. "
            "Configure it in .env or environment variables."
        )

    user_filter = _user_filter()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("positions", _begin_positions, filters=user_filter),
            CommandHandler("trades", _begin_trades, filters=user_filter),
        ],
        states={
            WAITING_PHOTOS: [
                MessageHandler(filters.PHOTO & user_filter, _receive_photo),
                MessageHandler(filters.Document.ALL & user_filter, _receive_document),
                CommandHandler("done", _done),
            ],
            CONFIRMING: [
                CallbackQueryHandler(_callback_confirm, pattern=r"^confirm:"),
                CallbackQueryHandler(_callback_cancel, pattern=r"^cancel$"),
                CallbackQueryHandler(_callback_change_date, pattern=r"^change_date:"),
            ],
            CHANGING_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, _receive_date),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", _cancel),
        ],
    )

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", _start, filters=user_filter))
    app.add_handler(conv_handler)

    return app


async def _ensure_db() -> None:
    """Create all tables if they don't exist (idempotent)."""
    from aisp.db.engine import init_db

    await init_db()


def run_bot() -> None:
    """Create and run the bot with long polling (blocking)."""
    import asyncio

    asyncio.run(_ensure_db())
    app = create_application()
    logger.info("Starting Telegram bot (long polling)...")
    app.run_polling()
