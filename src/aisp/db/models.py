"""SQLAlchemy 2.0 async models — 18 tables for A-ISP."""

from __future__ import annotations

import enum
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── Enums ──────────────────────────────────────────────


class AssetType(enum.StrEnum):
    INDEX = "index"
    STOCK = "stock"
    COMMODITY = "commodity"


class Sentiment(enum.StrEnum):
    PENDING = "pending"
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"
    EUPHORIC = "euphoric"
    PANIC = "panic"
    NOISE = "noise"


class Direction(enum.StrEnum):
    STRONG_BUY = "strong_buy"
    BUY = "buy"
    WEAK_BUY = "weak_buy"
    HOLD = "hold"
    WATCH = "watch"
    WEAK_SELL = "weak_sell"
    SELL = "sell"
    STRONG_SELL = "strong_sell"


BULLISH_DIRECTIONS = {Direction.STRONG_BUY, Direction.BUY, Direction.WEAK_BUY}
BEARISH_DIRECTIONS = {Direction.STRONG_SELL, Direction.SELL, Direction.WEAK_SELL}


class PoolType(enum.StrEnum):
    CORE = "core"
    MOMENTUM = "momentum"
    OPPORTUNITY = "opportunity"


class Evaluation(enum.StrEnum):
    CORRECT = "correct"
    WRONG = "wrong"
    NEUTRAL = "neutral"
    PENDING = "pending"


class TradeDirection(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"


class ImportSource(enum.StrEnum):
    OCR = "ocr"
    MANUAL = "manual"
    TELEGRAM = "telegram"


# ── 1. stk_daily ───────────────────────────────────────


class StkDaily(Base):
    __tablename__ = "stk_daily"
    __table_args__ = (
        UniqueConstraint("trade_date", "code", name="uq_stk_daily_date_code"),
        Index("ix_stk_daily_code_date", "code", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    code: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)

    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    change_pct: Mapped[float] = mapped_column(Float, nullable=False)

    turnover_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    volume_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 资金分层 (fund flow breakdown)
    main_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    main_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    super_large_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    super_large_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    large_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    large_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    medium_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    medium_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    small_net: Mapped[float | None] = mapped_column(Float, nullable=True)
    small_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    # 估值 (valuation)
    pe_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    pb_mrq: Mapped[float | None] = mapped_column(Float, nullable=True)

    is_st: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_limit_up: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_limit_down: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


# ── 2. global_daily ────────────────────────────────────


class GlobalDaily(Base):
    __tablename__ = "global_daily"
    __table_args__ = (
        UniqueConstraint("trade_date", "symbol", name="uq_global_daily_date_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    symbol: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    asset_type: Mapped[AssetType] = mapped_column(Enum(AssetType), nullable=False)

    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    change_pct: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)


# ── 3. stk_sector_map ─────────────────────────────────


class StkSectorMap(Base):
    __tablename__ = "stk_sector_map"
    __table_args__ = (
        UniqueConstraint("code", "sector_name", "source", name="uq_stk_sector_map"),
        Index("ix_stk_sector_map_sector_active", "sector_name", "is_active"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    sector_name: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# ── 4. sector_daily ────────────────────────────────────


class SectorDaily(Base):
    __tablename__ = "sector_daily"
    __table_args__ = (
        UniqueConstraint("trade_date", "sector_name", name="uq_sector_daily_date_name"),
        Index("ix_sector_daily_name_date", "sector_name", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    sector_name: Mapped[str] = mapped_column(String(50), index=True, nullable=False)

    close: Mapped[float] = mapped_column(Float, nullable=False)
    change_pct: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    net_inflow: Mapped[float | None] = mapped_column(Float, nullable=True)

    stock_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    up_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    down_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    ma5: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma10: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma20: Mapped[float | None] = mapped_column(Float, nullable=True)
    ma60: Mapped[float | None] = mapped_column(Float, nullable=True)


# ── 5. stk_comments ────────────────────────────────────


class StkComments(Base):
    __tablename__ = "stk_comments"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_stk_comments_source"),
        Index("ix_stk_comments_sentiment_fetched", "sentiment", "fetched_at"),
        Index("ix_stk_comments_code_published", "code", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), index=True, nullable=False)
    trade_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    sentiment: Mapped[Sentiment] = mapped_column(
        Enum(Sentiment), nullable=False, default=Sentiment.PENDING
    )
    sentiment_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    sentiment_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ── 6. daily_signals ───────────────────────────────────


class DailySignals(Base):
    __tablename__ = "daily_signals"
    __table_args__ = (
        UniqueConstraint("trade_date", "code", name="uq_daily_signals_date_code"),
        Index("ix_daily_signals_date_direction", "trade_date", "direction"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, index=True, nullable=False)
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    sector: Mapped[str] = mapped_column(String(50), nullable=False)
    pool_type: Mapped[PoolType | None] = mapped_column(Enum(PoolType), nullable=True)
    direction: Mapped[Direction] = mapped_column(Enum(Direction), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    factor_scores: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[str] = mapped_column(Text, nullable=False)

    performance: Mapped[SignalPerformance | None] = relationship(
        back_populates="signal", uselist=False
    )


# ── 7. signal_performance ──────────────────────────────


class SignalPerformance(Base):
    __tablename__ = "signal_performance"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("daily_signals.id", ondelete="CASCADE"), nullable=False
    )
    signal_date: Mapped[date] = mapped_column(Date, nullable=False)
    eval_date: Mapped[date] = mapped_column(Date, nullable=False)
    code: Mapped[str] = mapped_column(String(10), nullable=False)

    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    next_open: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_close: Mapped[float | None] = mapped_column(Float, nullable=True)
    next_change_pct: Mapped[float | None] = mapped_column(Float, nullable=True)

    evaluation: Mapped[Evaluation] = mapped_column(
        Enum(Evaluation), nullable=False, default=Evaluation.PENDING
    )
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    signal: Mapped[DailySignals] = relationship(back_populates="performance")


# ── 8. sector_pool_state ───────────────────────────────


class SectorPoolState(Base):
    __tablename__ = "sector_pool_state"
    __table_args__ = (
        UniqueConstraint(
            "sector_name",
            "pool_type",
            name="uq_sector_pool_active",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sector_name: Mapped[str] = mapped_column(String(50), index=True, nullable=False)
    pool_type: Mapped[PoolType] = mapped_column(Enum(PoolType), nullable=False)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    exit_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


# ── 9. sector_pool_history ─────────────────────────────


class SectorPoolHistory(Base):
    __tablename__ = "sector_pool_history"
    __table_args__ = (
        UniqueConstraint(
            "trade_date", "sector_name", "pool_type", name="uq_sector_pool_history"
        ),
        Index(
            "ix_sector_pool_history_name_type_date",
            "sector_name",
            "pool_type",
            "trade_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    sector_name: Mapped[str] = mapped_column(String(50), nullable=False)
    pool_type: Mapped[PoolType] = mapped_column(Enum(PoolType), nullable=False)
    is_qualified: Mapped[bool] = mapped_column(Boolean, nullable=False)
    metric_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ── 10. trading_calendar ───────────────────────────────


class TradingCalendar(Base):
    __tablename__ = "trading_calendar"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cal_date: Mapped[date] = mapped_column(Date, unique=True, index=True, nullable=False)
    is_trading_day: Mapped[bool] = mapped_column(Boolean, nullable=False)
    prev_trading_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_trading_date: Mapped[date | None] = mapped_column(Date, nullable=True)


# ── 11. position_snapshot ────────────────────────────────


class PositionSnapshot(Base):
    __tablename__ = "position_snapshot"
    __table_args__ = (
        UniqueConstraint("snapshot_date", "code", name="uq_position_snapshot_date_code"),
        Index("ix_position_snapshot_date", "snapshot_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)

    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    available_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_cost: Mapped[float] = mapped_column(Float, nullable=False)
    current_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    market_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    profit_loss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    today_profit_loss: Mapped[float | None] = mapped_column(Float, nullable=True)

    import_source: Mapped[ImportSource] = mapped_column(Enum(ImportSource), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# ── 12. trade_record ─────────────────────────────────────


class TradeRecord(Base):
    __tablename__ = "trade_record"
    __table_args__ = (
        UniqueConstraint(
            "trade_date", "code", "trade_direction", "price", "quantity",
            name="uq_trade_record_natural_key",
        ),
        Index("ix_trade_record_date", "trade_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    trade_direction: Mapped[TradeDirection] = mapped_column(Enum(TradeDirection), nullable=False)

    price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)

    commission: Mapped[float | None] = mapped_column(Float, nullable=True)
    stamp_tax: Mapped[float | None] = mapped_column(Float, nullable=True)
    transfer_fee: Mapped[float | None] = mapped_column(Float, nullable=True)
    other_fees: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_amount: Mapped[float | None] = mapped_column(Float, nullable=True)

    settlement_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    import_source: Mapped[ImportSource] = mapped_column(Enum(ImportSource), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# ── 13. image_hash ──────────────────────────────────────


class ImageHash(Base):
    __tablename__ = "image_hash"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# ── 14. market_sentiment ─────────────────────────────────


class MarketSentiment(Base):
    __tablename__ = "market_sentiment"
    __table_args__ = (
        UniqueConstraint("trade_date", name="uq_market_sentiment_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    limit_up_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    limit_down_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    real_limit_up: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blast_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_streak: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prev_zt_premium: Mapped[float | None] = mapped_column(Float, nullable=True)
    activity_rate: Mapped[float | None] = mapped_column(Float, nullable=True)


# ── 15. stk_profile ──────────────────────────────────────


class StkProfile(Base):
    __tablename__ = "stk_profile"
    __table_args__ = (
        UniqueConstraint("code", name="uq_stk_profile_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    board_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    total_shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    liq_shares: Mapped[float | None] = mapped_column(Float, nullable=True)
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# ── 16. stk_quarterly ────────────────────────────────────


class StkQuarterly(Base):
    __tablename__ = "stk_quarterly"
    __table_args__ = (
        UniqueConstraint("code", "year", "quarter", name="uq_stk_quarterly_code_yq"),
        Index("ix_stk_quarterly_code", "code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    quarter: Mapped[int] = mapped_column(Integer, nullable=False)
    net_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    np_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    gp_margin: Mapped[float | None] = mapped_column(Float, nullable=True)
    roe: Mapped[float | None] = mapped_column(Float, nullable=True)
    eps_ttm: Mapped[float | None] = mapped_column(Float, nullable=True)
    yoy_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    yoy_eps: Mapped[float | None] = mapped_column(Float, nullable=True)
    yoy_equity: Mapped[float | None] = mapped_column(Float, nullable=True)
    pub_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# ── 17. stk_lhb ─────────────────────────────────────────


class StkLhb(Base):
    __tablename__ = "stk_lhb"
    __table_args__ = (
        UniqueConstraint("trade_date", "code", name="uq_stk_lhb_date_code"),
        Index("ix_stk_lhb_trade_date", "trade_date"),
        Index("ix_stk_lhb_code", "code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    net_buy: Mapped[float | None] = mapped_column(Float, nullable=True)
    buy_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    turnover_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    liq_market_cap: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_1d: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_2d: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_5d: Mapped[float | None] = mapped_column(Float, nullable=True)
    post_10d: Mapped[float | None] = mapped_column(Float, nullable=True)


# ── 18. stk_margin ───────────────────────────────────────


class StkMargin(Base):
    __tablename__ = "stk_margin"
    __table_args__ = (
        UniqueConstraint("trade_date", "code", name="uq_stk_margin_date_code"),
        Index("ix_stk_margin_trade_date", "trade_date"),
        Index("ix_stk_margin_code", "code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    code: Mapped[str] = mapped_column(String(10), nullable=False)
    rzye: Mapped[float | None] = mapped_column(Float, nullable=True)
    rzjme: Mapped[float | None] = mapped_column(Float, nullable=True)
    rqyl: Mapped[float | None] = mapped_column(Float, nullable=True)
    rqjmg: Mapped[float | None] = mapped_column(Float, nullable=True)
    rzrqye: Mapped[float | None] = mapped_column(Float, nullable=True)
