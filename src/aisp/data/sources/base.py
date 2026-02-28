"""Abstract base class for sentiment data source adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class RawComment:
    """Standardized comment from any data source."""

    code: str
    source: str
    source_id: str | None = None
    title: str | None = None
    content: str = ""
    published_at: datetime = field(default_factory=datetime.now)


class DataSourceAdapter(ABC):
    """Base class for all sentiment data source adapters."""

    source_name: str = ""

    @abstractmethod
    async def fetch_comments(
        self, codes: list[str], since: datetime | None = None
    ) -> list[RawComment]:
        """Fetch comments/announcements for given stock codes since a given time."""
        ...
