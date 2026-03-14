"""LLM multimodal OCR — extract positions/trades from broker app screenshots."""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from aisp.config import get_settings
from aisp.engine.prompts import format_ocr_positions, format_ocr_trades

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def _create_ocr_llm(model_override: str | None = None) -> ChatOpenAI:
    """Create LangChain ChatOpenAI for OCR (multimodal, low temperature)."""
    settings = get_settings()
    return ChatOpenAI(
        model=model_override or settings.ocr.model,
        openai_api_key=settings.openrouter.api_key,
        openai_api_base=settings.openrouter.base_url,
        temperature=0.1,
        max_tokens=4000,
        default_headers={"HTTP-Referer": "https://github.com/aisp"},
    )


def _parse_json(text: str) -> dict | None:
    """Parse JSON with three-layer fallback (same as agent.py pattern)."""
    text = text.strip()

    # Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Markdown code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Brace extraction
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    logger.warning("Failed to parse OCR JSON: %s...", text[:200])
    return None


def validate_image(path: Path, max_mb: int = 10) -> None:
    """Validate image file exists, size, and format."""
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Unsupported format: {path.suffix} (use png/jpg/jpeg/webp)")
    size_mb = path.stat().st_size / (1024 * 1024)
    if size_mb > max_mb:
        raise ValueError(f"File too large: {size_mb:.1f}MB (max {max_mb}MB)")


def _encode_image(path: Path) -> tuple[str, str]:
    """Read and base64-encode an image, return (b64_data, mime_type)."""
    ext = path.suffix.lower()
    mime = _EXT_TO_MIME[ext]
    b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
    return b64, mime


async def _ocr_single_image(llm: ChatOpenAI, prompt: str, path: Path) -> dict | None:
    """Send a single image to the LLM and parse the JSON response."""
    b64, mime = _encode_image(path)
    msg = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
        ]
    )
    response = await llm.ainvoke([msg])
    return _parse_json(response.content)


async def extract_positions(image_paths: list[Path]) -> dict:
    """Extract positions from multiple screenshots, merge by code (last wins)."""
    from datetime import date

    settings = get_settings()
    llm = _create_ocr_llm()
    prompt = format_ocr_positions(today=date.today().isoformat())

    merged: dict[str, dict] = {}
    snapshot_date = date.today().isoformat()
    all_warnings: list[str] = []
    min_confidence = 1.0

    for path in image_paths:
        validate_image(path, max_mb=settings.ocr.max_image_size_mb)
        result = await _ocr_single_image(llm, prompt, path)
        if not result:
            all_warnings.append(f"Failed to parse OCR result from {path.name}")
            continue

        if result.get("snapshot_date"):
            snapshot_date = result["snapshot_date"]
        if result.get("confidence", 1.0) < min_confidence:
            min_confidence = result["confidence"]
        all_warnings.extend(result.get("warnings") or [])

        for pos in result.get("positions") or []:
            code = pos.get("code")
            if code:
                merged[code] = pos

    return {
        "snapshot_date": snapshot_date,
        "positions": list(merged.values()),
        "confidence": min_confidence,
        "warnings": all_warnings,
    }


async def extract_trades(image_paths: list[Path]) -> dict:
    """Extract trades from multiple screenshots, merge by natural key."""
    settings = get_settings()
    llm = _create_ocr_llm()
    prompt = format_ocr_trades()

    seen: set[tuple] = set()
    merged: list[dict] = []
    all_warnings: list[str] = []
    min_confidence = 1.0

    for path in image_paths:
        validate_image(path, max_mb=settings.ocr.max_image_size_mb)
        result = await _ocr_single_image(llm, prompt, path)
        if not result:
            all_warnings.append(f"Failed to parse OCR result from {path.name}")
            continue

        if result.get("confidence", 1.0) < min_confidence:
            min_confidence = result["confidence"]
        all_warnings.extend(result.get("warnings") or [])

        for trade in result.get("trades") or []:
            key = (
                trade.get("trade_date"),
                trade.get("code"),
                trade.get("trade_direction"),
                trade.get("price"),
                trade.get("quantity"),
            )
            if key not in seen:
                seen.add(key)
                merged.append(trade)

    return {
        "trades": merged,
        "confidence": min_confidence,
        "warnings": all_warnings,
    }


# ── Bytes-based interfaces (for Telegram bot) ──────────


async def _ocr_single_bytes(
    llm: ChatOpenAI, prompt: str, image_bytes: bytes, mime_type: str
) -> dict | None:
    """Send in-memory image bytes to the LLM and parse the JSON response."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    msg = HumanMessage(
        content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
        ]
    )
    response = await llm.ainvoke([msg])
    return _parse_json(response.content)


async def extract_positions_from_bytes(
    images: list[tuple[bytes, str]],
    snapshot_date: str | None = None,
    model_override: str | None = None,
) -> dict:
    """Extract positions from in-memory images (bytes, mime_type), merge by code."""
    from datetime import date as date_cls

    llm = _create_ocr_llm(model_override)
    today = snapshot_date or date_cls.today().isoformat()
    prompt = format_ocr_positions(today=today)

    merged: dict[str, dict] = {}
    result_date = today
    all_warnings: list[str] = []
    min_confidence = 1.0

    for image_bytes, mime_type in images:
        result = await _ocr_single_bytes(llm, prompt, image_bytes, mime_type)
        if not result:
            all_warnings.append("Failed to parse OCR result from image")
            continue

        if result.get("snapshot_date"):
            result_date = result["snapshot_date"]
        if result.get("confidence", 1.0) < min_confidence:
            min_confidence = result["confidence"]
        all_warnings.extend(result.get("warnings") or [])

        for pos in result.get("positions") or []:
            code = pos.get("code")
            if code:
                merged[code] = pos

    return {
        "snapshot_date": result_date,
        "positions": list(merged.values()),
        "confidence": min_confidence,
        "warnings": all_warnings,
    }


async def extract_trades_from_bytes(
    images: list[tuple[bytes, str]],
    trade_date: str | None = None,
    model_override: str | None = None,
) -> dict:
    """Extract trades from in-memory images (bytes, mime_type), merge by natural key."""
    llm = _create_ocr_llm(model_override)
    prompt = format_ocr_trades()

    seen: set[tuple] = set()
    merged: list[dict] = []
    all_warnings: list[str] = []
    min_confidence = 1.0

    for image_bytes, mime_type in images:
        result = await _ocr_single_bytes(llm, prompt, image_bytes, mime_type)
        if not result:
            all_warnings.append("Failed to parse OCR result from image")
            continue

        if result.get("confidence", 1.0) < min_confidence:
            min_confidence = result["confidence"]
        all_warnings.extend(result.get("warnings") or [])

        for t in result.get("trades") or []:
            if trade_date:
                t["trade_date"] = trade_date
            key = (
                t.get("trade_date"),
                t.get("code"),
                t.get("trade_direction"),
                t.get("price"),
                t.get("quantity"),
            )
            if key not in seen:
                seen.add(key)
                merged.append(t)

    return {
        "trades": merged,
        "confidence": min_confidence,
        "warnings": all_warnings,
    }
