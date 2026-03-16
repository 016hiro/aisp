"""Deep Agent for stock analysis with search tool access."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

from aisp.config import get_settings
from aisp.engine.prompts import get_template

logger = logging.getLogger(__name__)


# ── Search tools ──────────────────────────────────────────────────


def _ddg_search(query: str, max_results: int = 5) -> str:
    """Shared DuckDuckGo search with proxy support."""
    import os

    from ddgs import DDGS

    proxy = os.environ.get("AISP_SEARCH_PROXY") or os.environ.get("https_proxy")
    try:
        with DDGS(proxy=proxy) as ddgs:
            results = list(ddgs.text(query, region="cn-zh", max_results=max_results))
        if not results:
            return "未找到相关信息。"
        lines = []
        for r in results:
            lines.append(f"- [{r['title']}] {r['body'][:200]}")
        return "\n".join(lines)
    except Exception as e:
        logger.warning("Search failed for '%s': %s", query, e)
        return f"搜索失败: {e}"


@tool
def search_news(query: str) -> str:
    """搜索最新财经新闻和市场事件。用于获取与股票、板块或宏观经济相关的实时信息。

    Args:
        query: 搜索关键词，如"云铝股份 最新消息"、"中东局势 铝价影响"、"工业金属板块 资金流向"
    """
    return _ddg_search(query)


@tool
def search_sector_news(sector_name: str) -> str:
    """搜索特定板块的最新新闻和政策动态。

    Args:
        sector_name: A股板块名称，如"工业金属"、"半导体"、"光伏设备"
    """
    return _ddg_search(f"{sector_name}板块 最新消息 A股")


@tool
def search_macro_events(topic: str) -> str:
    """搜索宏观经济事件和地缘政治动态对市场的影响。

    Args:
        topic: 宏观话题，如"美联储加息"、"中东冲突 大宗商品"、"中国经济数据"
    """
    return _ddg_search(f"{topic} 股市影响")


AGENT_TOOLS = [search_news, search_sector_news, search_macro_events]


# ── Result dataclass ──────────────────────────────────────────────


@dataclass
class AgentResult:
    """Result from the stock analysis agent."""

    direction: str
    confidence: float
    reasoning: str
    key_risks: list[str]
    catalysts: list[str]
    raw: dict
    trading_plan: dict | None = None


# ── Agent creation ────────────────────────────────────────────────


def _create_llm() -> ChatOpenAI:
    """Create LangChain ChatOpenAI pointed at OpenRouter."""
    settings = get_settings().openrouter
    return ChatOpenAI(
        model=settings.analysis_model,
        openai_api_key=settings.api_key,
        openai_api_base=settings.base_url,
        temperature=0.3,
        max_tokens=2000,
        default_headers={
            "HTTP-Referer": "https://github.com/aisp",
        },
    )


def _create_agent():
    """Create a Deep Agent with search tools and analysis prompt."""
    from deepagents import create_deep_agent

    return create_deep_agent(
        model=_create_llm(),
        tools=AGENT_TOOLS,
        system_prompt=get_template("agent_system"),
    )


# ── Analysis entry point ─────────────────────────────────────────


async def analyze_stock(prompt: str) -> AgentResult | None:
    """Run the Deep Agent to analyze a single stock.

    The agent autonomously decides whether to search for news based on
    anomalies in the quantitative data, then produces a structured JSON result.

    Args:
        prompt: The full stock context (data, factors, sector, global market).

    Returns:
        AgentResult or None if analysis fails.
    """
    agent = _create_agent()

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": prompt}]},
        config={"recursion_limit": 30},
    )

    # Extract the final AI message
    final_text = _extract_last_ai_text(result)
    if not final_text:
        logger.warning("Agent returned empty response")
        return None

    parsed = _parse_agent_json(final_text)

    # Retry: ask LLM directly for JSON reformat if agent returned non-JSON
    if not parsed:
        logger.info("Agent returned non-JSON, requesting JSON reformat...")
        retry_messages = result["messages"] + [
            {"role": "user", "content": get_template("agent_json_retry")}
        ]
        retry_result = await agent.ainvoke(
            {"messages": retry_messages},
            config={"recursion_limit": 5},
        )
        retry_text = _extract_last_ai_text(retry_result)
        if retry_text:
            parsed = _parse_agent_json(retry_text)

    if not parsed:
        logger.warning("Agent JSON parsing failed after retry")
        return None

    return AgentResult(
        direction=parsed.get("direction", "hold"),
        confidence=float(parsed.get("confidence", 0.5)),
        reasoning=parsed.get("reasoning", "Agent 分析完成"),
        key_risks=parsed.get("key_risks", []),
        catalysts=parsed.get("catalysts", []),
        raw=parsed,
        trading_plan=parsed.get("trading_plan"),
    )


# ── Helpers ───────────────────────────────────────────────────────


def _extract_last_ai_text(result: dict) -> str | None:
    """Extract text content from the last AI message in agent result."""
    messages = result.get("messages", [])
    for msg in reversed(messages):
        content = getattr(msg, "content", None) or (msg.get("content") if isinstance(msg, dict) else None)
        role = getattr(msg, "type", None) or (msg.get("role") if isinstance(msg, dict) else None)
        if role in ("ai", "assistant") and content:
            return content
    return None


def _parse_agent_json(text: str) -> dict | None:
    """Parse JSON from agent response with fallbacks."""
    # Strip thinking blocks from reasoning models (e.g. Qwen3)
    text = re.sub(r"(?:<think>)?.*?</think>", "", text, flags=re.DOTALL).strip()

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

    logger.warning("Failed to parse agent JSON: %s...", text[:200])
    return None
