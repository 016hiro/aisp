"""Load market symbol definitions from config/symbols.toml."""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from aisp.db.models import AssetType

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path("config/symbols.toml")

_ASSET_TYPE_MAP = {
    "index": AssetType.INDEX,
    "stock": AssetType.STOCK,
    "commodity": AssetType.COMMODITY,
}


def _load_toml() -> dict:
    """Load and parse the symbols config file."""
    if not _CONFIG_PATH.exists():
        logger.warning("Config file %s not found, using empty config", _CONFIG_PATH)
        return {}
    with _CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def load_us_symbols() -> dict[str, tuple[str, AssetType]]:
    """Load US market symbols.

    Returns: {symbol: (name, AssetType)}
    """
    data = _load_toml()
    result: dict[str, tuple[str, AssetType]] = {}
    for item in data.get("us_market", []):
        symbol = item["symbol"]
        name = item["name"]
        asset_type = _ASSET_TYPE_MAP.get(item["asset_type"], AssetType.STOCK)
        result[symbol] = (name, asset_type)
    return result


def load_yf_commodities() -> dict[str, tuple[str, AssetType]]:
    """Load yfinance commodity symbols.

    Returns: {symbol: (name, AssetType.COMMODITY)}
    """
    data = _load_toml()
    result: dict[str, tuple[str, AssetType]] = {}
    for item in data.get("yf_commodities", []):
        result[item["symbol"]] = (item["name"], AssetType.COMMODITY)
    return result


def load_ak_commodities() -> dict[str, str]:
    """Load AkShare domestic commodity symbols.

    Returns: {symbol: display_name}
    """
    data = _load_toml()
    result: dict[str, str] = {}
    for item in data.get("ak_commodities", []):
        result[item["symbol"]] = item["name"]
    return result


def load_cn_watchlist() -> list[dict[str, str]]:
    """Load A-share watchlist for selective fetching.

    Returns: [{"code": "600519", "name": "贵州茅台"}, ...]
    """
    data = _load_toml()
    return [
        {"code": item["code"], "name": item["name"]}
        for item in data.get("cn_watchlist", [])
    ]
