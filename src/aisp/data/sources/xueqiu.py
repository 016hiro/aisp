"""Xueqiu (雪球) data source adapter.

This is a placeholder adapter for integrating with user's existing
Xueqiu scraping infrastructure. Implement the fetch_comments method
to connect to your data source.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aisp.data.sources import register_adapter
from aisp.data.sources.base import DataSourceAdapter, RawComment

logger = logging.getLogger(__name__)


@register_adapter
class XueqiuAdapter(DataSourceAdapter):
    """Xueqiu comment data source adapter (placeholder)."""

    source_name = "xueqiu"

    async def fetch_comments(
        self, codes: list[str], since: datetime | None = None
    ) -> list[RawComment]:
        """Fetch comments from Xueqiu.

        TODO: Connect to user's existing Xueqiu scraping Skill/service.
        """
        logger.info("Xueqiu adapter not yet configured, returning empty results")
        return []
