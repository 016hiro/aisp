"""OpenRouter LLM client with dual-model support."""

from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx

from aisp.config import get_settings

logger = logging.getLogger(__name__)


class LLMClient:
    """Async LLM client via OpenRouter API.

    Supports two models:
    - sentiment_model: cheaper/faster for sentiment classification
    - analysis_model: stronger model for deep analysis
    """

    def __init__(self):
        settings = get_settings().openrouter
        self.api_key = settings.api_key
        self.base_url = settings.base_url
        self.analysis_model = settings.analysis_model
        self.sentiment_model = settings.sentiment_model
        self.timeout = settings.timeout
        self._semaphore = asyncio.Semaphore(settings.max_concurrent)
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/aisp",
                },
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2000,
    ) -> str:
        """Send a chat completion request. Returns the response text."""
        async with self._semaphore:
            return await self._chat_with_retry(
                messages, model or self.analysis_model, temperature, max_tokens
            )

    async def analyze_json(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2000,
    ) -> dict | list:
        """Send a chat request expecting JSON response.

        Attempts to parse JSON from response, with regex fallback.
        """
        response = await self.chat(messages, model, temperature, max_tokens)
        return _parse_json_response(response)

    async def _chat_with_retry(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
        max_retries: int = 3,
    ) -> str:
        """Chat with exponential backoff retry for rate limits."""
        client = await self._get_client()
        last_exc = None

        for attempt in range(max_retries):
            try:
                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }

                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                )

                if response.status_code == 429:
                    delay = 2 ** (attempt + 1)
                    logger.warning(
                        "Rate limited (429), retrying in %ds (attempt %d/%d)",
                        delay,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(delay)
                    continue

                response.raise_for_status()
                data = response.json()
                return data["choices"][0]["message"]["content"]

            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code == 429:
                    delay = 2 ** (attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                raise
            except (httpx.TimeoutException, httpx.ConnectError) as e:
                last_exc = e
                if attempt < max_retries - 1:
                    delay = 2**attempt
                    logger.warning(
                        "Request failed: %s. Retrying in %ds...", e, delay
                    )
                    await asyncio.sleep(delay)
                else:
                    raise

        raise last_exc  # type: ignore[misc]


def _parse_json_response(text: str) -> dict | list:
    """Parse JSON from LLM response, with fallback extraction."""
    # Try direct parse first
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting JSON from markdown code block
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except json.JSONDecodeError:
            pass

    # Try finding first { or [ to last } or ]
    for start_char, end_char in [("{", "}"), ("[", "]")]:
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass

    logger.warning("Failed to parse JSON from LLM response: %s...", text[:200])
    return {}
