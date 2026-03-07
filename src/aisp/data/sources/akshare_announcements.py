"""AkShare announcement data source adapter.

Uses ak.stock_notice_report() which fetches announcements by report type and date.
The API returns columns: 代码, 名称, 公告标题, 公告类型, 公告日期, 网址
"""

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
        """Fetch recent announcements and filter by stock codes.

        ak.stock_notice_report(symbol, date) fetches announcements by type, not by stock.
        We fetch all recent announcements and filter locally by code.
        """
        import akshare as ak

        code_set = set(codes)
        target_date = (since or datetime.now()).strftime("%Y%m%d")

        def _fetch():
            try:
                df = ak.stock_notice_report(symbol="全部", date=target_date)
                if df is None or df.empty:
                    return []
                return df.to_dict("records")
            except Exception:
                logger.debug("No announcements available for date %s", target_date)
                return []

        rows = await asyncio.to_thread(_fetch)
        results: list[RawComment] = []

        for row in rows:
            code = str(row.get("代码", ""))
            if not code or code not in code_set:
                continue

            title = str(row.get("公告标题", ""))
            # API returns 公告日期 as date object, 公告类型 as category string
            pub_date_raw = row.get("公告日期")

            if pub_date_raw:
                if isinstance(pub_date_raw, str):
                    try:
                        pub_date = datetime.fromisoformat(pub_date_raw)
                    except ValueError:
                        pub_date = datetime.now()
                elif hasattr(pub_date_raw, "to_pydatetime"):
                    pub_date = pub_date_raw.to_pydatetime()
                elif hasattr(pub_date_raw, "year"):
                    # date object → datetime
                    pub_date = datetime(
                        pub_date_raw.year, pub_date_raw.month, pub_date_raw.day
                    )
                else:
                    pub_date = datetime.now()
            else:
                pub_date = datetime.now()

            if since and pub_date < since:
                continue

            announce_type = str(row.get("公告类型", ""))
            content = f"[{announce_type}] {title}" if announce_type else title
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

        logger.info(
            "Fetched %d announcements for %d codes (from %d total)",
            len(results),
            len(codes),
            len(rows),
        )
        return results
