"""Natural language watchlist management via LLM."""

from __future__ import annotations

import logging

from aisp.data.symbols import (
    _SECTION_META,
    add_symbol,
    list_section,
    remove_symbol,
)
from aisp.engine.llm_client import LLMClient
from aisp.engine.prompts import format_watchlist_nlp

logger = logging.getLogger(__name__)


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
    prompt = format_watchlist_nlp(
        current_symbols=current_symbols,
        user_query=query,
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
        action = action_item.get("action")
        section = action_item.get("section")
        confident = action_item.get("confident", True)
        msg = action_item.get("message", "")

        if not confident:
            messages.append(f"[yellow]不确定: {msg}[/yellow]")
            continue

        if action == "list":
            items = list_section(section)
            if not items:
                messages.append(f"[dim][{section}] 为空[/dim]")
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
                messages.append(f"[yellow]{key_value} 已在 [{section}] 中[/yellow]")

        elif action == "remove":
            key_field = action_item.get("key_field", _SECTION_META[section][0])
            key_value = action_item.get("key_value")

            if remove_symbol(section, key_field, key_value):
                messages.append(f"[green]{msg}[/green]")
            else:
                messages.append(f"[yellow]{key_value} 不在 [{section}] 中[/yellow]")

        else:
            messages.append(f"[red]未知操作: {action}[/red]")

    return messages or ["未识别到有效操作，请换个说法试试"]
