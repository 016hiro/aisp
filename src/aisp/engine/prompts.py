"""LLM prompt templates — loaded from config/prompts.toml."""

from __future__ import annotations

import functools
from pathlib import Path

import tomlkit

_PROMPTS_PATH = Path(__file__).resolve().parents[3] / "config" / "prompts.toml"


@functools.lru_cache(maxsize=1)
def _load_prompts() -> dict:
    """Load and cache all prompt templates from TOML."""
    with open(_PROMPTS_PATH, encoding="utf-8") as f:
        return tomlkit.load(f)


def get_template(name: str) -> str:
    """Get a raw prompt template by section name."""
    return _load_prompts()[name]["template"]


# ── Formatting helpers ──


def _fmt_score(val: float | str, fmt: str = ".4f") -> str:
    if isinstance(val, str):
        return val
    return format(val, fmt)


def _fmt_weight(val: float | str) -> str:
    if isinstance(val, str):
        return val
    return f"{val:.0%}"


def format_stock_analysis(**kwargs) -> str:
    """Format stock analysis prompt with defaults for optional fields."""
    kwargs.setdefault("veto_warning", "")
    kwargs.setdefault("extra_instructions", "")
    for prefix in ("f_", "w_"):
        for key in list(kwargs):
            if key.startswith(prefix):
                val = kwargs[key]
                if prefix == "f_":
                    kwargs[key] = _fmt_score(val)
                else:
                    kwargs[key] = _fmt_weight(val)
    return get_template("stock_analysis").format(**kwargs)


def format_sentiment_classification(**kwargs) -> str:
    """Format sentiment classification prompt."""
    return get_template("sentiment_classification").format(**kwargs)


def format_watchlist_nlp(**kwargs) -> str:
    """Format watchlist NLP prompt."""
    return get_template("watchlist_nlp").format(**kwargs)
