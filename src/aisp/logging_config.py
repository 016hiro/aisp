"""Structured logging configuration — Rich console + file output + JSONL error log."""

from __future__ import annotations

import json
import logging
import traceback
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler


class _JSONLErrorFormatter(logging.Formatter):
    """Format ERROR+ records as single-line JSON for machine parsing."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC)
            .astimezone()
            .strftime("%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1] is not None:
            entry["exception"] = type(record.exc_info[1]).__qualname__
            entry["traceback"] = "".join(
                traceback.format_exception(*record.exc_info)
            ).strip()
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    error_log_dir: str | None = "data/logs/errors",
    error_log_retain_days: int = 30,
) -> None:
    """Configure logging with Rich console, optional file output, and JSONL error log.

    Args:
        level: Console log level (INFO/DEBUG).
        log_file: Optional full-debug log file path.
        error_log_dir: Directory for daily JSONL error logs. None to disable.
        error_log_retain_days: Number of daily error log files to keep.
    """
    handlers: list[logging.Handler] = []

    # Rich console handler
    console = Console(stderr=True)
    rich_handler = RichHandler(
        console=console,
        show_time=True,
        show_path=False,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
    )
    rich_handler.setLevel(getattr(logging, level.upper(), logging.INFO))
    handlers.append(rich_handler)

    # File handler (if specified)
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handlers.append(file_handler)

    # Always-on JSONL error handler (ERROR+)
    if error_log_dir:
        err_dir = Path(error_log_dir)
        err_dir.mkdir(parents=True, exist_ok=True)
        err_file = err_dir / "errors.jsonl"
        err_handler = TimedRotatingFileHandler(
            err_file,
            when="midnight",
            backupCount=error_log_retain_days,
            encoding="utf-8",
        )
        err_handler.setLevel(logging.ERROR)
        err_handler.setFormatter(_JSONLErrorFormatter())
        # Rotated files: errors.jsonl.2026-03-17, errors.jsonl.2026-03-16, ...
        err_handler.namer = lambda name: name  # keep default naming
        handlers.append(err_handler)

    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    # Quiet down noisy dependencies
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
