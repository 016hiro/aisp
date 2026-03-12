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
uv run aisp fetch-cn [--trade-date YYYY-MM-DD]

# Analysis pipeline
uv run aisp screen [--trade-date YYYY-MM-DD]
uv run aisp analyze [--trade-date YYYY-MM-DD]
uv run aisp briefing [--trade-date YYYY-MM-DD]

# Combined pipelines
uv run aisp run-analysis-pipeline [--trade-date YYYY-MM-DD] [--codes 002709,002463]  # screen → analyze → briefing (3-in-1, e2e verification)
uv run aisp run-morning [--trade-date YYYY-MM-DD]   # fetch-us → commodities → btc → screen → analyze → briefing (uses latest CN data)
uv run aisp run-close [--trade-date YYYY-MM-DD]     # fetch-cn (watchlist) → update pools → track performance
uv run aisp show [--trade-date YYYY-MM-DD]             # TUI briefing viewer (interactive dashboard)
uv run aisp status                                    # Show active pools and recent signals

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

Prompts externalized in `config/prompts.toml`, loaded by `engine/prompts.py`. JSON parsing has three fallback layers: direct parse → markdown code block extraction → brace-matching regex. Parsing failure triggers auto-retry asking LLM for JSON reformat.

### Sentiment Adapter Pattern (`data/sources/`)

New data sources implement `DataSourceAdapter` and use `@register_adapter` decorator. The registry auto-discovers adapters at import time.

### Database (10 tables in `db/models.py`)

All writes use SQLite upsert (`on_conflict_do_update`) for idempotency. Batch inserts use 500-row chunks. Async via `aiosqlite` + `greenlet`.

Key enums (all `StrEnum`): `Direction` (strong_buy/buy/weak_buy/hold/watch/weak_sell/sell/strong_sell), `Sentiment` (7 values including pending), `PoolType` (core/momentum/opportunity), `Evaluation` (correct/wrong/neutral/pending).

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
