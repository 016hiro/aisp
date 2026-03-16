"""OpenRouter LLM client with dual-model support and local-first fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import httpx

from aisp.config import get_settings

logger = logging.getLogger(__name__)


# ── Circuit breaker for local LLM server ─────────────────────────


class CircuitBreaker:
    """Simple TTL-based circuit breaker for local LLM server.

    Shared across httpx (LLMClient) and LangChain (OCR) paths.
    """

    def __init__(self) -> None:
        self._fail_until: float = 0.0
        self._ttl: int = 30

    def configure(self, ttl: int) -> None:
        self._ttl = ttl

    def is_open(self) -> bool:
        """True = circuit is open = skip local server."""
        return time.monotonic() < self._fail_until

    def record_failure(self) -> None:
        self._fail_until = time.monotonic() + self._ttl

    def record_success(self) -> None:
        self._fail_until = 0.0


local_breaker = CircuitBreaker()


# ── LLM Client ───────────────────────────────────────────────────


class LLMClient:
    """Async LLM client via OpenRouter API.

    Supports two models:
    - sentiment_model: cheaper/faster for sentiment classification
    - analysis_model: stronger model for deep analysis

    When local LLM is configured, lightweight tasks (use_local=True)
    try the local server first and fall back to remote on failure.
    """

    def __init__(self):
        settings = get_settings()
        or_cfg = settings.openrouter
        local_cfg = settings.local_llm

        # Remote (OpenRouter)
        self.api_key = or_cfg.api_key
        self.base_url = or_cfg.base_url
        self.analysis_model = or_cfg.analysis_model
        self.sentiment_model = or_cfg.sentiment_model
        self.timeout = or_cfg.timeout
        self._semaphore = asyncio.Semaphore(or_cfg.max_concurrent)
        self._client: httpx.AsyncClient | None = None

        # Local
        self._local_enabled = local_cfg.enabled and bool(local_cfg.sentiment_model)
        self._local_base_url = local_cfg.base_url
        self._local_api_key = local_cfg.api_key
        self._local_sentiment_model = local_cfg.sentiment_model
        self._local_connect_timeout = local_cfg.connect_timeout
        self._local_request_timeout = local_cfg.request_timeout
        self._local_client: httpx.AsyncClient | None = None

        local_breaker.configure(local_cfg.circuit_breaker_ttl)

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

    async def _get_local_client(self) -> httpx.AsyncClient:
        if self._local_client is None or self._local_client.is_closed:
            self._local_client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self._local_request_timeout,
                    connect=self._local_connect_timeout,
                ),
                headers={
                    "Authorization": f"Bearer {self._local_api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._local_client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        if self._local_client and not self._local_client.is_closed:
            await self._local_client.aclose()

    async def chat(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2000,
        *,
        use_local: bool = False,
    ) -> str:
        """Send a chat completion request. Returns the response text."""
        async with self._semaphore:
            if use_local and self._local_enabled:
                result = await self._try_local(
                    messages, self._local_sentiment_model, temperature, max_tokens
                )
                if result is not None:
                    return result

            return await self._chat_with_retry(
                messages, model or self.analysis_model, temperature, max_tokens
            )

    async def analyze_json(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 2000,
        *,
        use_local: bool = False,
    ) -> dict | list:
        """Send a chat request expecting JSON response.

        Attempts to parse JSON from response, with regex fallback.
        """
        response = await self.chat(
            messages, model, temperature, max_tokens, use_local=use_local
        )
        return _parse_json_response(response)

    async def _try_local(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> str | None:
        """Try local LLM server. Returns response text or None on failure."""
        if local_breaker.is_open():
            return None

        try:
            client = await self._get_local_client()
            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "chat_template_kwargs": {"enable_thinking": False},
            }
            response = await client.post(
                f"{self._local_base_url}/chat/completions",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            text = data["choices"][0]["message"]["content"]
            local_breaker.record_success()
            logger.info("LLM response from local server (model=%s)", model)
            return text
        except (httpx.ConnectError, httpx.TimeoutException) as e:
            local_breaker.record_failure()
            logger.info("Local LLM unreachable (%s), falling back to remote", e)
            return None
        except Exception as e:
            local_breaker.record_failure()
            logger.warning("Local LLM error (%s), falling back to remote", e)
            return None

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


def _strip_thinking(text: str) -> str:
    """Strip thinking blocks from reasoning-model output (e.g. Qwen3).

    Handles both '<think>...</think>' and bare '...</think>' (no opening tag).
    """
    return re.sub(r"(?:<think>)?.*?</think>", "", text, flags=re.DOTALL).strip()


def _parse_json_response(text: str) -> dict | list:
    """Parse JSON from LLM response, with fallback extraction."""
    text = _strip_thinking(text).strip()
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
