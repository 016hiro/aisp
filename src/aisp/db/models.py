"""SQLAlchemy 2.0 async models — 10 tables for A-ISP."""

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
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    WATCH = "watch"


class PoolType(enum.StrEnum):
    CORE = "core"
    MOMENTUM = "momentum"
    OPPORTUNITY = "opportunity"


class Evaluation(enum.StrEnum):
    CORRECT = "correct"
    WRONG = "wrong"
    NEUTRAL = "neutral"
    PENDING = "pending"


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
