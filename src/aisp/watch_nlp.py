"""Natural language watchlist management via LLM."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from aisp.data.symbols import (
    _SECTION_META,
    add_symbol,
    list_section,
    remove_symbol,
)
from aisp.engine.llm_client import LLMClient
from aisp.engine.prompts import format_watchlist_nlp

logger = logging.getLogger(__name__)

_CODE_CACHE_PATH = Path("data/a_stock_codes.json")


# ── Stock code cache ──────────────────────────────────────────


def _load_code_cache() -> dict[str, str]:
    """Load cached A-share name→code mapping. Returns empty dict if unavailable."""
    if not _CODE_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CODE_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def refresh_code_cache() -> int:
    """Refresh A-share code cache from AkShare. Returns number of entries."""
    import akshare as ak

    df = ak.stock_info_a_code_name()
    data = {row["name"].strip(): row["code"] for _, row in df.iterrows()}
    _CODE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CODE_CACHE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return len(data)


def _extract_cn_names(query: str) -> list[str]:
    """Extract potential Chinese stock name segments (2-6 chars) from query."""
    # Remove common action words
    cleaned = re.sub(r"(添加|加入|关注|观察|删除|移除|取消|查看|列表|显示|有哪些|和|的|到)", " ", query)
    # Extract consecutive Chinese character runs of 2-6 chars
    return re.findall(r"[\u4e00-\u9fff]{2,6}", cleaned)


def _build_code_hints(query: str) -> str:
    """Try to match Chinese stock names from query against cache.

    Returns hint text for LLM prompt, or empty string.
    """
    cache = _load_code_cache()
    if not cache:
        return "\n\n注意：A股代码缓存未建立。如果不确定代码，设 confident=false。"

    names = _extract_cn_names(query)
    if not names:
        return ""

    hints = []
    for name in names:
        # Exact match
        if name in cache:
            hints.append(f"{name} → {cache[name]} (A股)")
        else:
            # Substring match (e.g. "茅台" matches "贵州茅台")
            matches = [(k, v) for k, v in cache.items() if name in k]
            if len(matches) == 1:
                hints.append(f"{name} → {matches[0][0]}({matches[0][1]}) (A股)")
            elif len(matches) > 1 and len(matches) <= 3:
                for k, v in matches:
                    hints.append(f"{name} → {k}({v}) (A股)")

    if hints:
        return "\n\n## 代码参考（系统查询结果，请直接使用）\n" + "\n".join(f"- {h}" for h in hints)
    return ""


# ── Post-validation ───────────────────────────────────────────


def _is_cn_name(name: str) -> bool:
    """Check if a name is predominantly Chinese characters."""
    cn_chars = sum(1 for c in name if "\u4e00" <= c <= "\u9fff")
    return cn_chars >= 2


def _validate_action(action_item: dict) -> dict:
    """Post-validate and fix LLM output.

    - Chinese name in us_market → move to cn_watchlist
    - Non-6-digit code in cn_watchlist → set confident=false
    """
    action = action_item.get("action")
    section = action_item.get("section")
    fields = action_item.get("fields", {})
    name = fields.get("name", "")
    key_value = action_item.get("key_value", "")

    if action == "add" and section != "cn_watchlist" and _is_cn_name(name):
        # Chinese stock name in non-CN section — likely misclassified
        cache = _load_code_cache()
        code = cache.get(name)
        if not code:
            # Try fuzzy
            matches = [(k, v) for k, v in cache.items() if name in k]
            if len(matches) == 1:
                code = matches[0][1]
                name = matches[0][0]

        if code:
            action_item = {
                **action_item,
                "section": "cn_watchlist",
                "key_field": "code",
                "key_value": code,
                "fields": {"name": name},
                "message": f"添加{name}({code})到A股观察列表",
            }
        else:
            action_item = {
                **action_item,
                "confident": False,
                "message": f"无法确认「{name}」的A股代码，请使用: aisp watch-add <代码> {name}",
            }

    # Validate cn_watchlist code format
    if (
        action == "add"
        and action_item.get("section") == "cn_watchlist"
        and not re.match(r"^\d{6}$", action_item.get("key_value", ""))
    ):
        action_item = {
            **action_item,
            "confident": False,
            "message": f"代码格式错误「{key_value}」，A股代码必须是6位数字",
        }

    return action_item


def _build_current_symbols_text() -> str:
    """Build a summary of all current symbols for the LLM context."""
    lines: list[str] = []
    for section, (key_field, _fields) in _SECTION_META.items():
        items = list_section(section)
        if not items:
            lines.append(f"[{section}]: (empty)")
            continue
        entries = ", ".join(
            f"{item.get(key_field)}({item.get('name', '')})" for item in items
        )
        lines.append(f"[{section}]: {entries}")
    return "\n".join(lines)


async def handle_watch_query(query: str) -> list[str]:
    """Parse natural language query via LLM and execute watchlist operations.

    Returns a list of result messages for display.
    """
    current_symbols = _build_current_symbols_text()
    code_hints = _build_code_hints(query)
    prompt = format_watchlist_nlp(
        current_symbols=current_symbols,
        user_query=query,
        code_hints=code_hints,
    )

    client = LLMClient()
    try:
        result = await client.analyze_json(
            [{"role": "user", "content": prompt}],
            model=client.sentiment_model,
            use_local=True,
        )
    finally:
        await client.close()

    if not isinstance(result, dict) or "actions" not in result:
        return ["LLM 返回格式异常，请重试或使用结构化命令 (watch-add/watch-rm/watch-ls)"]

    messages: list[str] = []
    for action_item in result["actions"]:
        action_item = _validate_action(action_item)

        action = action_item.get("action")
        section = action_item.get("section")
        confident = action_item.get("confident", True)
        msg = action_item.get("message", "")

        if not confident:
            messages.append(f"[yellow]不确定: {msg}[/yellow]")
            continue

        if section not in _SECTION_META:
            messages.append(f"[red]未知列表: {section}[/red]")
            continue

        if action == "list":
            items = list_section(section)
            if not items:
                messages.append(f"[dim]\\[{section}] 为空[/dim]")
            else:
                key_field = _SECTION_META[section][0]
                lines = [f"[bold]{section}[/bold] ({len(items)} 条):"]
                for item in items:
                    lines.append(f"  {item.get(key_field)} - {item.get('name', '')}")
                messages.append("\n".join(lines))

        elif action == "add":
            key_field = action_item.get("key_field", _SECTION_META[section][0])
            key_value = action_item.get("key_value")
            fields = action_item.get("fields", {})

            if add_symbol(section, key_field, key_value, fields):
                messages.append(f"[green]{msg}[/green]")
            else:
                messages.append(
                    f"[yellow]{key_value} 已在 \\[{section}] 中[/yellow]"
                )

        elif action == "remove":
            key_field = action_item.get("key_field", _SECTION_META[section][0])
            key_value = action_item.get("key_value")

            if remove_symbol(section, key_field, key_value):
                messages.append(f"[green]{msg}[/green]")
            else:
                messages.append(
                    f"[yellow]{key_value} 不在 \\[{section}] 中[/yellow]"
                )

        else:
            messages.append(f"[red]未知操作: {action}[/red]")

    return messages or ["未识别到有效操作，请换个说法试试"]
