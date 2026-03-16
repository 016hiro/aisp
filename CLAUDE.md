# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Run Commands

```bash
uv sync --extra all              # Install all deps (data sources + dev tools)
uv run aisp --help               # Show CLI commands
uv run aisp init-db              # Create/migrate SQLite tables

# Data fetching
uv run aisp fetch-us [--trade-date YYYY-MM-DD]
uv run aisp fetch-commodities [--trade-date YYYY-MM-DD]
uv run aisp fetch-cn [--trade-date YYYY-MM-DD]          # 默认 watchlist 模式，--mode full 全量

# Analysis pipeline
uv run aisp screen [--trade-date YYYY-MM-DD]
uv run aisp analyze [--trade-date YYYY-MM-DD]
uv run aisp briefing [--trade-date YYYY-MM-DD]

# Combined pipelines
uv run aisp run-analysis-pipeline [--trade-date YYYY-MM-DD] [--codes 002709,002463]  # auto-fetch → screen → analyze → evaluate → briefing (auto-补全CN/全球数据)
uv run aisp run-morning [--trade-date YYYY-MM-DD]   # fetch-us → commodities → btc → screen → analyze → briefing (uses latest CN data)
uv run aisp run-close [--trade-date YYYY-MM-DD]     # fetch-cn (watchlist) → update pools → track performance
uv run aisp show [--trade-date YYYY-MM-DD]             # TUI briefing viewer (interactive dashboard)
uv run aisp status                                    # Show active pools and recent signals

# Portfolio (OCR import from broker screenshots)
uv run aisp import-positions <screenshot>... [--yes]  # OCR → confirm → write position snapshots
uv run aisp import-trades <screenshot>... [--yes]     # OCR → confirm → write trade records
uv run aisp positions [--date YYYY-MM-DD]             # View position snapshots (default: latest)
uv run aisp trades [--date YYYY-MM-DD] [--days 7]     # View trade records (default: last 7 days)

# Telegram Bot
uv run aisp telegram                              # Start Telegram bot (long polling)

# Watchlist management
uv run aisp watch "添加天赐材料"                       # Natural language (LLM-powered)
uv run aisp watch-add 002709 天赐材料                  # Structured add (default: A-share)
uv run aisp watch-add GOOG Alphabet --type us          # US market
uv run aisp watch-rm 002709                            # Remove from A-share watchlist
uv run aisp watch-ls                                   # List A-share watchlist
uv run aisp watch-ls --type us                         # List US market symbols
```

## Testing & Linting

```bash
uv run pytest tests/ -v                        # Run all tests
uv run pytest tests/test_integration.py::test_config_loads -v  # Run single test
uv run ruff check src/ tests/                  # Lint
uv run ruff check --fix src/ tests/            # Auto-fix lint issues
```

## Architecture

**V1 is observation-mode only**: generates signals and tracks performance, no simulated positions or capital management.

### Data Pipeline Flow

```
US/Commodity data (yfinance) ──┐
                               ├→ Three-Pool Sector Filter → Multi-Factor Stock Scorer
A-Share data (AkShare) ────────┘         │                          │
                                         ▼                          ▼
Sentiment (adapter pattern) ──→ LLM Sentiment Classification   LLM Deep Analysis
                                         │                          │
                                         └──────────┬───────────────┘
                                                     ▼
                                              Signal Generation (daily_signals)
                                                     │
                                         ┌───────────┴───────────┐
                                         ▼                       ▼
                                  T+1 Performance          Markdown Briefing
                                    Tracking               (5 sections)
```

### Three-Pool Sector Management (`screening/sector_pools.py`)

- **Core**: Static sectors from config, never removed
- **Momentum**: Top N sectors by daily change; removed after N consecutive trading days outside top N (derived from `sector_pool_history`, not a counter)
- **Opportunity**: Worst performers with MA60 uptrend + low volume; observed for N days

### 8-Factor Elastic-Weight Scoring (`screening/stock_scorer.py`)

Stocks ranked within sector using 8 factors with elastic weights (extreme factors auto-boosted):
- Fund(0.20), Momentum(0.10), Technical(0.10), Quality(0.05), Indicators/RSI+MACD+MA(0.20), Macro(0.10), Sentiment(0.15), Sector(0.10)
- Elastic weight: `raw = base × (1 + α × |score-0.5| × 2)`, normalized. Default α=2.0
- Veto rules: macro<0.15 or sentiment<0.10 → block buy; sentiment>0.95 → warn
- Two paths: `score_all_pools()` (batch, top-N per sector) vs `score_by_codes()` (targeted, no top-N)
- Trend detection: `_compute_trend(closes)` stores consecutive down days, cumulative decline %, MA5 vs MA20 in `raw_data["_trend"]`

Supporting modules: `screening/indicators.py` (RSI/MACD/MA pure functions), `screening/factor_engine.py` (elastic weights + veto engine)

### Wyckoff Calibration Layer (`screening/wyckoff.py`)

Post-scoring calibration (not a 9th factor): detects Accumulation/Distribution phases from 60+ OHLCV bars, applies a multiplier to `total_score`.
- Consolidation detection: ATR(20)/close < threshold + tight overall range
- Prior trend: MA20 vs MA60 before consolidation → down (accumulation) or up (distribution)
- Event detection: Spring/SOS/LPS (accumulation) or UT/SOW/LPSY (distribution) in last 5 bars
- Multiplier: accumulation lerp(1.0→1.25), distribution lerp(1.0→0.60), LPSY confirmed → 0.0 (veto)
- Config: `WyckoffConfig` in `config.py`, env prefix `AISP_WYCKOFF__*`
- `WyckoffResult` includes `support`/`resistance` fields, reused by breakout detection
- Wyckoff context injected into LLM prompt via `extra_instructions` in `analyzer.py`

### Trading Plan Generation (`screening/trading_plan.py`)

Post-breakout layer: synthesizes existing price data into a structured trading plan with A-share-specific rules.
- Pure functions, no DB dependency — called at the end of `_score_sector()` in `stock_scorer.py`
- Price limits: auto-detects board type (普通±10%, 创业板/科创板±20%, ST±5%)
- Key levels: collects from Wyckoff support/resistance, breakout levels, MAs (5/10/20/60), ATR, prev high/low, N-day extremes
- Entry zone: nearest support below → close, width ≤ 1.5×ATR(20); `None` when limit up/down
- Stop loss: `max(support - 1% buffer, entry_low - 1×ATR)`, floored at limit down
- Targets: first two resistance levels above close; fallback to ATR-based (1.5/2.5×ATR); capped at limit up
- Risk/reward: `(target1 - entry_mid) / (entry_mid - stop_loss)` → aggressive(≥3)/normal(≥1.5)/conservative
- Two-layer integration: quant plan injected into LLM prompt → Agent validates/adjusts → merged result stored in `factor_scores["_trading_plan"]`
- R:R protection: stop loss minimum distance 1.5% of close, R:R clamped to [0, 10.0]
- Config: `TradingPlanConfig` in `config.py`, env prefix `AISP_TRADING_PLAN__*`

### Direction Guardrails (`engine/analyzer.py`)

Post-LLM direction override layer: prevents bullish signals from contradicting quantitative data.
- R:R consistency: BUY + R:R<1.0 → downgrade one level (BUY→WEAK_BUY→HOLD)
- Trend filter: 3+ consecutive down days AND cumulative decline >5% AND MA5<MA20 → force HOLD
- Applied in `_apply_direction_guardrails()` after LLM result parsing, before signal storage

### Breakout Signal Detection (`screening/breakout.py`)

Post-Wyckoff detection layer: identifies breakout events and generates Chinese text descriptions.
- Three sub-detectors: consolidation breakout (reuses Wyckoff support/resistance), MA crossover (MA20/MA60), N-day new high/low
- Strength scoring (0-1): volume(0.30) + close_pos(0.20) + body_ratio(0.15) + consolidation(0.20) + gap(0.15)
- Strong (>=0.60) → Chinese description injected into LLM prompt via `extra_instructions` + stored in `factor_scores["_breakout"]` for briefing
- Weak (>=0.35) → multiplier adjustment only (bullish +5%, bearish -8%)
- Config: `BreakoutConfig` in `config.py`, env prefix `AISP_BREAKOUT__*`
- Data flow: `stock_scorer.py` runs detection → stores in `raw_data["_breakout"]` → `analyzer.py` injects into prompt + passes to `factor_scores` → `signals.py` persists to DB → `briefing.py` renders in signal card

### Deep Agent Analysis (`engine/agent.py`)

Stock analysis uses LangChain Deep Agents (`deepagents` package) with `create_deep_agent`:
- Custom tools: `search_news`, `search_sector_news`, `search_macro_events` (DuckDuckGo via `ddgs`)
- Built-in: task planning (`write_todos`), context management, subagent spawning
- Agent autonomously decides when to search for news based on data anomalies
- Concurrent analysis with `Semaphore(5)`, search proxy via `AISP_SEARCH_PROXY` env var

### Dual-Model LLM Strategy (`engine/llm_client.py`)

OpenRouter API with `asyncio.Semaphore(5)` concurrency control:
- `sentiment_model` (cheap/fast): batch sentiment classification + watchlist NLP
- `analysis_model` (strong): Agent-based per-stock deep analysis with tool access

**Local-first fallback** (`LocalLLMConfig` in `config.py`, env prefix `AISP_LOCAL_LLM__*`):
- Lightweight tasks (sentiment, watchlist NLP, OCR) try local LAN server first, fall back to remote OpenRouter on failure
- httpx path: `LLMClient.chat(use_local=True)` → `_try_local()` → remote `_chat_with_retry()`
- LangChain path (OCR): `_ocr_image_with_fallback()` / `_ocr_bytes_with_fallback()` → local `ChatOpenAI` → remote `ChatOpenAI`
- Circuit breaker (`CircuitBreaker` / `local_breaker`): shared across httpx and LangChain paths, TTL-based (default 30s), prevents repeated connection waits in batch
- Deep Agent analysis always uses remote (requires Claude-level reasoning + tool use)

Prompts externalized in `config/prompts.toml`, loaded by `engine/prompts.py`. JSON parsing has three fallback layers: direct parse → markdown code block extraction → brace-matching regex. Parsing failure triggers auto-retry asking LLM for JSON reformat.

### Sentiment Adapter Pattern (`data/sources/`)

New data sources implement `DataSourceAdapter` and use `@register_adapter` decorator. The registry auto-discovers adapters at import time.

### Portfolio OCR Import (`portfolio/`)

LLM multimodal OCR to extract positions/trades from broker app screenshots.
- `ocr.py`: `ChatOpenAI` via OpenRouter (same pattern as `engine/agent.py`), multimodal image messages, three-layer JSON fallback
- `importer.py`: SQLite upsert for `position_snapshot` and `trade_record` tables
- OCR model config: `OcrConfig` in `config.py`, env prefix `AISP_OCR__*` (default: `google/gemini-3.1-flash-lite-preview`)
- API key/base_url reused from `openrouter` config section
- Multi-screenshot: each image OCR'd separately, results merged (positions by code, trades by natural key)
- CLI flow: validate images → OCR extract → Rich table preview + confidence → user confirm → DB upsert

### Telegram Bot (`telegram/`)

Screenshot OCR import via Telegram — replaces manual `import-positions`/`import-trades` CLI flow.
- `bot.py`: `ConversationHandler` with states WAITING_PHOTOS → CONFIRMING → CHANGING_DATE
- `dedup.py`: SHA256 image dedup against `image_hash` table
- `formatter.py`: HTML message formatting + inline keyboard
- OCR uses `analysis_model` (Claude Sonnet) via bytes-based `extract_positions_from_bytes`/`extract_trades_from_bytes`
- Security: `filters.User(user_id=allowed_user_ids)` when configured
- Config: `TelegramConfig` in `config.py`, env prefix `AISP_TELEGRAM__*`
- Daemon: `com.aisp.telegram.plist` with `KeepAlive: true`

### Database (13 tables in `db/models.py`)

All writes use SQLite upsert (`on_conflict_do_update`) for idempotency. Batch inserts use 500-row chunks. Async via `aiosqlite` + `greenlet`.

Key enums (all `StrEnum`): `Direction` (strong_buy/buy/weak_buy/hold/watch/weak_sell/sell/strong_sell), `Sentiment` (7 values including pending), `PoolType` (core/momentum/opportunity), `Evaluation` (correct/wrong/neutral/pending), `TradeDirection` (buy/sell), `ImportSource` (ocr/manual/telegram).

New tables: `position_snapshot` (daily snapshot, unique on `(snapshot_date, code)`), `trade_record` (unique on `(trade_date, code, direction, price, quantity)`), and `image_hash` (SHA256 dedup for Telegram bot, unique on `hash`).

### Configuration (`config.py`)

Pydantic Settings with env prefix `AISP_` and `__` as nested delimiter. Example: `AISP_OPENROUTER__API_KEY`. Loads from `.env` file automatically.

## End-to-End Acceptance Testing

When the user asks to run an "端到端验收测试" (end-to-end acceptance test / e2e test), you MUST:

1. Run `uv run aisp run-analysis-pipeline --trade-date <date>` (optionally with `--codes`) to execute the full screen → analyze → briefing pipeline.
2. **Use the Read tool to read the generated briefing file** (`briefings/<date>.md`), printing the full content back to the user so they can visually inspect the result (the "颜值测试").
3. Do NOT just say "looks good" — the user needs to **see the actual rendered output** themselves to judge quality.
4. If the pipeline fails or data is missing, diagnose and report the issue clearly.

The goal is to verify the entire data → analysis → briefing chain produces correct and well-formatted output, with real Chinese text, tables, and all sections populated.

## Conventions

- **README sync** — Every code change (except pure bugfix) must also update `README.md` to keep documentation in sync with the implementation
- **Python 3.13** — `.python-version` pins 3.13 (3.14 alpha has greenlet crashes)
- **Async everywhere** — all DB access, HTTP calls, and data fetching are async; CLI bridges with `asyncio.run()`
- **Chinese prompts for LLM** — all prompt templates in `config/prompts.toml` (Chinese); `engine/prompts.py` is just a loader; `RUF001` (ambiguous Unicode) is suppressed in ruff config
- **Ruff config**: target py313, line-length 100, isort with `aisp` as first-party
- **Data dependencies are optional** — `yfinance` and `akshare` are in `[project.optional-dependencies].data`
- **Graceful degradation** — fund flow and AkShare commodity fetches fail silently with warnings; missing `net_inflow` defaults to 0.5 factor score

## Compact Instructions

When compressing, preserve in priority order:

1. Architecture decisions (NEVER summarize)
2. Modified files and their key changes
3. Current verification status (pass/fail)
4. Open TODOs and rollback notes
5. Tool outputs (can delete, keep pass/fail only)