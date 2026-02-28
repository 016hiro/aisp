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
    weight_fund: float = 0.4
    weight_momentum: float = 0.3
    weight_technical: float = 0.2
    weight_quality: float = 0.1


class PoolConfig(BaseModel):
    core_sectors: list[str] = Field(
        default_factory=lambda: ["半导体", "汽车整车", "光伏设备", "白酒", "银行"]
    )
    momentum_top_n: int = 10
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
    pool: PoolConfig = Field(default_factory=PoolConfig)
    asset_linkage: AssetLinkageConfig = Field(default_factory=AssetLinkageConfig)

    briefing_dir: Path = Path("briefings")


def get_settings() -> Settings:
    return Settings()
