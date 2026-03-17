"""Load and manage market symbol definitions from config/*.toml files.

Config layout:
  config/markets.toml   — us_market, yf_commodities, ak_commodities
  config/watchlist.toml  — cn_watchlist
  config/sectors.toml    — core_sectors
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

import tomlkit

from aisp.db.models import AssetType

logger = logging.getLogger(__name__)

# ── Config file paths ─────────────────────────────────────────
_CONFIG_DIR = Path("config")
_MARKETS_PATH = _CONFIG_DIR / "markets.toml"
_WATCHLIST_PATH = _CONFIG_DIR / "watchlist.toml"
_SECTORS_PATH = _CONFIG_DIR / "sectors.toml"

# Section → which file it lives in
_SECTION_FILE: dict[str, Path] = {
    "us_market": _MARKETS_PATH,
    "yf_commodities": _MARKETS_PATH,
    "ak_commodities": _MARKETS_PATH,
    "cn_watchlist": _WATCHLIST_PATH,
}

_ASSET_TYPE_MAP = {
    "index": AssetType.INDEX,
    "stock": AssetType.STOCK,
    "commodity": AssetType.COMMODITY,
}


def _load_toml(path: Path) -> dict:
    """Load and parse a TOML config file."""
    if not path.exists():
        logger.warning("Config file %s not found, using empty config", path)
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


# ── Read-only loaders ─────────────────────────────────────────


def load_us_symbols() -> dict[str, tuple[str, AssetType]]:
    """Load US market symbols. Returns: {symbol: (name, AssetType)}"""
    data = _load_toml(_MARKETS_PATH)
    result: dict[str, tuple[str, AssetType]] = {}
    for item in data.get("us_market", []):
        symbol = item["symbol"]
        name = item["name"]
        asset_type = _ASSET_TYPE_MAP.get(item["asset_type"], AssetType.STOCK)
        result[symbol] = (name, asset_type)
    return result


def load_yf_commodities() -> dict[str, tuple[str, AssetType]]:
    """Load yfinance commodity symbols. Returns: {symbol: (name, AssetType.COMMODITY)}"""
    data = _load_toml(_MARKETS_PATH)
    result: dict[str, tuple[str, AssetType]] = {}
    for item in data.get("yf_commodities", []):
        result[item["symbol"]] = (item["name"], AssetType.COMMODITY)
    return result


def load_ak_commodities() -> dict[str, str]:
    """Load AkShare domestic commodity symbols. Returns: {symbol: display_name}"""
    data = _load_toml(_MARKETS_PATH)
    result: dict[str, str] = {}
    for item in data.get("ak_commodities", []):
        result[item["symbol"]] = item["name"]
    return result


def load_core_sectors() -> list[str]:
    """Load core sector names. Returns: ["半导体", "电机", ...]"""
    data = _load_toml(_SECTORS_PATH)
    return data.get("core_sectors", [])


def load_cn_watchlist() -> list[dict[str, str]]:
    """Load A-share watchlist. Returns: [{"code": "600519", "name": "贵州茅台"}, ...]"""
    data = _load_toml(_WATCHLIST_PATH)
    return [
        {"code": item["code"], "name": item["name"]}
        for item in data.get("cn_watchlist", [])
    ]


# ── Read-write helpers (tomlkit preserves formatting) ─────────


def _load_tomlkit(path: Path) -> tomlkit.TOMLDocument:
    """Load TOML preserving formatting and comments."""
    if not path.exists():
        return tomlkit.document()
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def _save_tomlkit(path: Path, doc: tomlkit.TOMLDocument) -> None:
    """Write TOML document back to file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")


def add_cn_watchlist(code: str, name: str) -> bool:
    """Add a stock to cn_watchlist. Returns False if already exists."""
    return add_symbol("cn_watchlist", "code", code, {"name": name})


def remove_cn_watchlist(code: str) -> bool:
    """Remove a stock from cn_watchlist by code. Returns False if not found."""
    return remove_symbol("cn_watchlist", "code", code)


# ── Generic symbol management ──────────────────────────────

# Section metadata: (key_field, required_fields)
_SECTION_META: dict[str, tuple[str, list[str]]] = {
    "cn_watchlist": ("code", ["code", "name"]),
    "us_market": ("symbol", ["symbol", "name", "asset_type"]),
    "yf_commodities": ("symbol", ["symbol", "name"]),
    "ak_commodities": ("symbol", ["symbol", "name"]),
}


def _get_section_path(section: str) -> Path:
    """Resolve the config file path for a given section."""
    path = _SECTION_FILE.get(section)
    if not path:
        raise ValueError(f"Unknown section: {section}. Valid: {list(_SECTION_FILE)}")
    return path


def list_section(section: str) -> list[dict[str, str]]:
    """List all entries in a TOML section."""
    if section not in _SECTION_META:
        raise ValueError(f"Unknown section: {section}. Valid: {list(_SECTION_META)}")
    path = _get_section_path(section)
    data = _load_toml(path)
    return [dict(item) for item in data.get(section, [])]


def add_symbol(section: str, key_field: str, key_value: str, fields: dict) -> bool:
    """Add an entry to a TOML array-of-tables section. Returns False if key already exists."""
    path = _get_section_path(section)
    doc = _load_tomlkit(path)

    for item in doc.get(section, []):
        if item[key_field] == key_value:
            return False

    entry = tomlkit.table()
    entry.add(key_field, key_value)
    for k, v in fields.items():
        if k != key_field:
            entry.add(k, v)

    if section not in doc:
        aot = tomlkit.aot()
        doc.add(section, aot)

    doc[section].append(entry)
    _save_tomlkit(path, doc)
    return True


def remove_symbol(section: str, key_field: str, key_value: str) -> bool:
    """Remove an entry from a TOML array-of-tables section. Returns False if not found."""
    path = _get_section_path(section)
    doc = _load_tomlkit(path)
    items = doc.get(section)
    if not items:
        return False

    for i, item in enumerate(items):
        if item[key_field] == key_value:
            del items[i]
            _save_tomlkit(path, doc)
            return True

    return False
