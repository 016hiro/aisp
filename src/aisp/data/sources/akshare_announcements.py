"""AkShare announcement data source adapter."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aisp.data import with_retry
from aisp.data.sources import register_adapter
from aisp.data.sources.base import DataSourceAdapter, RawComment

logger = logging.getLogger(__name__)


@register_adapter
class AkShareAnnouncementAdapter(DataSourceAdapter):
    """Fetches stock announcements via AkShare."""

    source_name = "akshare"

    @with_retry(max_retries=2)
    async def fetch_comments(
        self, codes: list[str], since: datetime | None = None
    ) -> list[RawComment]:
        """Fetch recent announcements for given stock codes."""
        import akshare as ak

        results: list[RawComment] = []

        for code in codes:
            try:

                def _fetch(c=code):
                    try:
                        df = ak.stock_notice_report(symbol=c)
                        if df is None or df.empty:
                            return []
                        return df.to_dict("records")
                    except Exception:
                        logger.debug("No announcements for %s", c)
                        return []

                rows = await asyncio.to_thread(_fetch)

                for row in rows:
                    title = str(row.get("公告标题", "") or row.get("title", ""))
                    content = str(row.get("公告内容", "") or row.get("content", title))
                    pub_date_raw = row.get("公告日期") or row.get("date")

                    if pub_date_raw:
                        if isinstance(pub_date_raw, str):
                            try:
                                pub_date = datetime.fromisoformat(pub_date_raw)
                            except ValueError:
                                pub_date = datetime.now()
                        elif hasattr(pub_date_raw, "to_pydatetime"):
                            pub_date = pub_date_raw.to_pydatetime()
                        else:
                            pub_date = datetime.now()
                    else:
                        pub_date = datetime.now()

                    if since and pub_date < since:
                        continue

                    source_id = f"{code}_{pub_date.isoformat()}_{hash(title) % 10000}"

                    results.append(
                        RawComment(
                            code=code,
                            source=self.source_name,
                            source_id=source_id,
                            title=title[:500] if title else None,
                            content=content or title,
                            published_at=pub_date,
                        )
                    )
            except Exception:
                logger.exception("Error fetching announcements for %s", code)

        return results
