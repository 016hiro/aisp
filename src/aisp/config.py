"""Pydantic Settings configuration with .env and environment variable support."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseConfig(BaseModel):
    url: str = "sqlite+aiosqlite:///data/aisp.db"


class OpenRouterConfig(BaseModel):
    api_key: str = ""
    base_url: str = "https://openrouter.ai/api/v1"
    analysis_model: str = "anthropic/claude-sonnet-4"
    sentiment_model: str = "google/gemini-2.0-flash-001"
    max_concurrent: int = 5
    timeout: int = 60


class ScoringWeights(BaseModel):
    weight_fund: float = 0.20
    weight_momentum: float = 0.10
    weight_technical: float = 0.10
    weight_quality: float = 0.05
    weight_indicators: float = 0.20
    weight_macro: float = 0.10
    weight_sentiment: float = 0.15
    weight_sector: float = 0.10
    elasticity: float = 2.0
    indicator_lookback_days: int = 90


class VetoConfig(BaseModel):
    """Hard veto thresholds — override via env vars."""

    macro_floor: float = 0.15
    sentiment_floor: float = 0.10
    sentiment_ceiling: float = 0.95


class WyckoffConfig(BaseModel):
    """Wyckoff phase detection calibration layer."""

    enabled: bool = True
    min_bars: int = 60
    atr_ratio_threshold: float = 0.05  # A股日内波动较大, 0.03 太严
    min_consolidation_days: int = 20
    consolidation_max_range: float = 0.30  # 盘整区间最大价格幅度(30%)
    breach_threshold: float = 0.02
    vol_low_ratio: float = 0.8
    vol_high_ratio: float = 1.5
    close_position_threshold: float = 0.5
    # 事件置信度权重
    spring_weight: float = 0.4
    sos_weight: float = 0.4
    lps_weight: float = 0.2
    ut_weight: float = 0.4
    sow_weight: float = 0.4
    lpsy_weight: float = 0.2
    # 乘数边界
    acc_max_multiplier: float = 1.25
    dist_min_multiplier: float = 0.60
    markup_multiplier: float = 1.05
    markdown_multiplier: float = 0.0


class BreakoutConfig(BaseModel):
    """Breakout signal detection layer — runs after Wyckoff calibration."""

    enabled: bool = True
    strong_threshold: float = 0.60
    weak_threshold: float = 0.35
    vol_confirm_ratio: float = 1.5
    ma_periods: list[int] = Field(default_factory=lambda: [20, 60])
    new_high_low_period: int = 60
    fallback_lookback: int = 40
    bullish_multiplier_adj: float = 0.05
    bearish_multiplier_adj: float = -0.08
    # strength score weights
    w_volume: float = 0.30
    w_close_pos: float = 0.20
    w_body_ratio: float = 0.15
    w_consolidation: float = 0.20
    w_gap: float = 0.15


class TradingPlanConfig(BaseModel):
    """Quantitative trading plan generation — runs after Wyckoff + breakout."""

    enabled: bool = True
    atr_period: int = 20
    entry_zone_max_atr_width: float = 1.5
    stop_buffer_pct: float = 0.01  # 止损位低于支撑的缓冲比例
    st_atr_multiplier: float = 0.5  # ST 更紧止损
    normal_atr_multiplier: float = 1.0


class OcrConfig(BaseModel):
    """OCR extraction via multimodal LLM."""

    model: str = "google/gemini-3.1-flash-lite-preview"
    confidence_threshold: float = 0.7
    max_image_size_mb: int = 10


class TelegramConfig(BaseModel):
    bot_token: str = ""
    allowed_user_ids: list[int] = Field(default_factory=list)


class PoolConfig(BaseModel):
    core_sectors: list[str] = Field(default_factory=list)
    momentum_top_n: int = 5
    momentum_exit_days: int = 5
    opportunity_top_n: int = 10
    opportunity_observe_days: int = 3
    top_stocks_per_sector: int = 5


class AssetLinkageConfig(BaseModel):
    """Global asset → A-share sector mapping."""

    mapping: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "GC=F": ["贵金属"],
            "HG=F": ["有色金属", "铜缆"],
            "CL=F": ["石油开采", "石油化工"],
            "^IXIC": ["半导体", "消费电子"],
            "KWEB": ["互联网服务", "游戏"],
        }
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AISP_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db: DatabaseConfig = Field(default_factory=DatabaseConfig)
    openrouter: OpenRouterConfig = Field(default_factory=OpenRouterConfig)
    scoring: ScoringWeights = Field(default_factory=ScoringWeights)
    veto: VetoConfig = Field(default_factory=VetoConfig)
    wyckoff: WyckoffConfig = Field(default_factory=WyckoffConfig)
    breakout: BreakoutConfig = Field(default_factory=BreakoutConfig)
    trading_plan: TradingPlanConfig = Field(default_factory=TradingPlanConfig)
    ocr: OcrConfig = Field(default_factory=OcrConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    pool: PoolConfig = Field(default_factory=PoolConfig)
    asset_linkage: AssetLinkageConfig = Field(default_factory=AssetLinkageConfig)

    briefing_dir: Path = Path("briefings")


def get_settings() -> Settings:
    return Settings()
