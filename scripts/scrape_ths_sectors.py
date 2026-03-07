"""Sync THS industry board constituents into stk_sector_map (source='ths').

Data source priority:
1. thsdk GitHub repo CSV (panghu11033/thsdk) — complete data via THS TCP protocol,
   auto-updated by GitHub Actions. This is the primary source.
2. THS website scraping (headed browser) — fallback, only gets page-1 (top 20 stocks
   per board) due to anti-scraping on AJAX pagination.

thsdk uses a native C library (hq.dylib/hq.so) that speaks THS's private TCP protocol
directly — it's NOT HTTP scraping. The CSV in their repo is generated via GitHub Actions
on a Linux runner. Since the native lib doesn't support Apple M-series (arm64), we
download the pre-built CSV instead.

Usage:
    uv run python scripts/scrape_ths_sectors.py              # auto (GitHub CSV first)
    uv run python scripts/scrape_ths_sectors.py --github     # force GitHub CSV
    uv run python scripts/scrape_ths_sectors.py --browser    # force browser scraping
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import logging
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOARDS_FILE = Path("config/ths_boards.json")
PROGRESS_FILE = Path("config/ths_scrape_progress.json")

# GitHub raw URL for thsdk's auto-generated industry constituents CSV
THSDK_CSV_URL = (
    "https://raw.githubusercontent.com/panghu11033/thsdk/main/data/industry_constituents.csv"
)


# ---------------------------------------------------------------------------
# Source 1: thsdk GitHub CSV (primary)
# ---------------------------------------------------------------------------

def fetch_from_github() -> dict[str, list[str]]:
    """Download industry_constituents.csv from thsdk repo via gh CLI.

    The CSV has columns: 行业代码, 行业名称, 成分股
    Stock codes are prefixed: USHA600519 (SH), USZA000858 (SZ),
    USTM920xxx (BJ/北交所), USHT6xxxxx (delisted).

    Returns:
        dict mapping sector_name -> list of 6-digit stock codes (SH/SZ only).
    """
    logger.info("Fetching industry constituents CSV from thsdk GitHub repo...")

    # Try gh CLI first (handles auth, avoids proxy issues)
    try:
        result = subprocess.run(
            ["gh", "api", "repos/panghu11033/thsdk/contents/data/industry_constituents.csv",
             "-q", ".content"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            import base64
            csv_text = base64.b64decode(result.stdout.strip()).decode("utf-8-sig")
            logger.info("Downloaded CSV via gh CLI (%d bytes)", len(csv_text))
            return _parse_thsdk_csv(csv_text)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Fallback: direct HTTP download
    try:
        import urllib.request
        with urllib.request.urlopen(THSDK_CSV_URL, timeout=30) as resp:
            csv_text = resp.read().decode("utf-8-sig")
            logger.info("Downloaded CSV via HTTP (%d bytes)", len(csv_text))
            return _parse_thsdk_csv(csv_text)
    except Exception as e:
        logger.error("Failed to download CSV: %s", e)
        return {}


def _parse_thsdk_csv(csv_text: str) -> dict[str, list[str]]:
    """Parse thsdk CSV text into {sector_name: [code, ...]}."""
    boards: dict[str, list[str]] = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        board_name = row["行业名称"]
        constituents = row["成分股"]
        codes = []
        for raw_code in constituents.split(","):
            m = re.search(r"(\d{6})", raw_code.strip())
            if m:
                code = m.group(1)
                # Keep SH (6xxxxx) and SZ (0xxxxx/3xxxxx/00xxxx) stocks
                # Skip BJ/北交所 (9xxxxx, 8xxxxx) codes
                if not code.startswith(("9", "8")):
                    codes.append(code)
        boards[board_name] = codes

    total = sum(len(v) for v in boards.values())
    logger.info("Parsed %d boards, %d stock mappings from CSV", len(boards), total)
    return boards


# ---------------------------------------------------------------------------
# Source 2: THS website browser scraping (fallback)
# ---------------------------------------------------------------------------

def fetch_from_browser() -> dict[str, list[str]]:
    """Scrape THS website using agent-browser (headed mode).

    Only gets page 1 (top 20 stocks) per board due to THS anti-scraping
    blocking AJAX pagination. Requires agent-browser CLI installed.

    Returns:
        dict mapping sector_name -> list of 6-digit stock codes.
    """
    boards = _load_boards()
    progress = _load_browser_progress()
    remaining = {n: c for n, c in boards.items() if n not in progress}
    total = len(remaining)

    logger.info("Browser scraping: %d boards, %d already done, %d remaining",
                len(boards), len(progress), total)

    if not remaining:
        return progress

    JS_EXTRACT = (
        'JSON.stringify(Array.from(document.querySelectorAll(".m-table tbody tr"))'
        '.map(tr=>{const tds=tr.querySelectorAll("td");'
        'return{code:tds[1]?.textContent?.trim(),name:tds[2]?.textContent?.trim()}})'
        '.filter(r=>r.code&&/^\\d{6}$/.test(r.code)))'
    )

    # Open headed browser
    _browser_cmd("open", "about:blank", timeout=15)
    time.sleep(1)

    for i, (name, code) in enumerate(remaining.items(), 1):
        logger.info("[%d/%d] %s (%s)...", i, total, name, code)

        url = f"https://q.10jqka.com.cn/thshy/detail/code/{code}/"
        _browser_cmd("open", url, timeout=20)
        time.sleep(1.2)
        _browser_cmd("wait", "--load", "networkidle", timeout=15)
        time.sleep(1)

        raw = _browser_cmd("eval", JS_EXTRACT, timeout=15)
        stocks = _parse_browser_result(raw)

        if stocks:
            progress[name] = [s["code"] for s in stocks]
            _save_browser_progress(progress)
            logger.info("  -> %d stocks", len(stocks))
        else:
            logger.warning("  -> 0 stocks (blocked or empty)")
            time.sleep(2)
            # Retry once
            raw = _browser_cmd("eval", JS_EXTRACT, timeout=15)
            stocks = _parse_browser_result(raw)
            if stocks:
                progress[name] = [s["code"] for s in stocks]
                _save_browser_progress(progress)
                logger.info("  Retry -> %d stocks", len(stocks))

        time.sleep(0.8)

    # Close browser
    try:
        subprocess.run(["agent-browser", "close"], timeout=10, capture_output=True)
    except Exception:
        pass

    return progress


def _browser_cmd(*args: str, timeout: int = 20) -> str:
    try:
        r = subprocess.run(
            ["agent-browser", "--headed", *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _parse_browser_result(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
        if isinstance(data, str):
            data = json.loads(data)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _load_boards() -> dict[str, str]:
    with BOARDS_FILE.open() as f:
        return json.load(f)


def _load_browser_progress() -> dict[str, list[str]]:
    if PROGRESS_FILE.exists():
        with PROGRESS_FILE.open() as f:
            raw = json.load(f)
        # Normalize: progress may store [{"code": "x", "name": "y"}] or ["x", "y"]
        result = {}
        for name, stocks in raw.items():
            if stocks and isinstance(stocks[0], dict):
                result[name] = [s["code"] for s in stocks]
            else:
                result[name] = stocks
        return result
    return {}


def _save_browser_progress(progress: dict[str, list[str]]):
    with PROGRESS_FILE.open("w") as f:
        json.dump(progress, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# DB writer
# ---------------------------------------------------------------------------

async def write_to_db(boards: dict[str, list[str]]):
    """Write sector-stock mappings to stk_sector_map (source='ths')."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

    from aisp.db.engine import get_engine, get_session_factory
    from aisp.db.models import StkSectorMap

    engine = get_engine()
    session_factory = get_session_factory(engine)
    now = datetime.now()
    total = 0

    async with session_factory() as session:
        for sector_name, codes in boards.items():
            for code in codes:
                rec = {
                    "code": code,
                    "sector_name": sector_name,
                    "source": "ths",
                    "is_active": True,
                    "updated_at": now,
                }
                stmt = (
                    sqlite_upsert(StkSectorMap)
                    .values(**rec)
                    .on_conflict_do_update(
                        index_elements=["code", "sector_name", "source"],
                        set_={"is_active": True, "updated_at": now},
                    )
                )
                await session.execute(stmt)
                total += 1

            if total % 500 == 0:
                await session.flush()

        await session.commit()

    await engine.dispose()
    logger.info("Written %d sector-stock mappings (source=ths) to DB", total)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sync THS industry board constituents to DB")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--github", action="store_true", help="Force GitHub CSV source")
    group.add_argument("--browser", action="store_true", help="Force browser scraping")
    args = parser.parse_args()

    boards: dict[str, list[str]] = {}

    if args.browser:
        boards = fetch_from_browser()
    elif args.github:
        boards = fetch_from_github()
    else:
        # Auto: try GitHub first, fallback to browser
        boards = fetch_from_github()
        if not boards:
            logger.warning("GitHub source failed, falling back to browser scraping")
            boards = fetch_from_browser()

    total = sum(len(v) for v in boards.values())
    unique = len({c for codes in boards.values() for c in codes})
    logger.info("Result: %d boards, %d mappings, %d unique stocks", len(boards), total, unique)

    if total > 0:
        logger.info("Writing to database...")
        asyncio.run(write_to_db(boards))
        logger.info("Done!")
    else:
        logger.error("No data fetched, nothing to write")


if __name__ == "__main__":
    main()
