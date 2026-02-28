"""Sentiment data source adapter registry."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aisp.data.sources.base import DataSourceAdapter

_registry: dict[str, type[DataSourceAdapter]] = {}


def register_adapter(cls: type[DataSourceAdapter]) -> type[DataSourceAdapter]:
    """Class decorator to register a data source adapter."""
    _registry[cls.source_name] = cls
    return cls


def get_all_adapters() -> dict[str, type[DataSourceAdapter]]:
    """Return all registered adapters."""
    return dict(_registry)
