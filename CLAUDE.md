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
uv run aisp run-morning [--trade-date YYYY-MM-DD]   # fetch-us → commodities → screen → analyze → briefing
uv run aisp run-close [--trade-date YYYY-MM-DD]     # fetch-cn → update pools → track performance
uv run aisp status                                    # Show active pools and recent signals
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

### Four-Factor Stock Scoring (`screening/stock_scorer.py`)

Stocks are ranked within their sector using percentile ranks (0-1):
- Fund flow (0.4): net_inflow / amount ratio. `None` → default 0.5
- Momentum (0.3): daily change_pct rank
- Technical (0.2): volume_ratio × 0.6 + turnover suitability × 0.4
- Quality (0.1): log(market_cap) rank

### Dual-Model LLM Strategy (`engine/llm_client.py`)

OpenRouter API with `asyncio.Semaphore(5)` concurrency control:
- `sentiment_model` (cheap/fast): batch sentiment classification of comments
- `analysis_model` (strong): per-stock deep analysis with full context

JSON parsing has three fallback layers: direct parse → markdown code block extraction → brace-matching regex.

### Sentiment Adapter Pattern (`data/sources/`)

New data sources implement `DataSourceAdapter` and use `@register_adapter` decorator. The registry auto-discovers adapters at import time.

### Database (10 tables in `db/models.py`)

All writes use SQLite upsert (`on_conflict_do_update`) for idempotency. Batch inserts use 500-row chunks. Async via `aiosqlite` + `greenlet`.

Key enums (all `StrEnum`): `Direction` (buy/sell/hold/watch), `Sentiment` (7 values including pending), `PoolType` (core/momentum/opportunity), `Evaluation` (correct/wrong/neutral/pending).

### Configuration (`config.py`)

Pydantic Settings with env prefix `AISP_` and `__` as nested delimiter. Example: `AISP_OPENROUTER__API_KEY`. Loads from `.env` file automatically.

## Conventions

- **Python 3.13** — `.python-version` pins 3.13 (3.14 alpha has greenlet crashes)
- **Async everywhere** — all DB access, HTTP calls, and data fetching are async; CLI bridges with `asyncio.run()`
- **Chinese prompts for LLM** — prompts in `analyzer.py` are in Chinese; `RUF001` (ambiguous Unicode) is suppressed in ruff config
- **Ruff config**: target py313, line-length 100, isort with `aisp` as first-party
- **Data dependencies are optional** — `yfinance` and `akshare` are in `[project.optional-dependencies].data`
- **Graceful degradation** — fund flow and AkShare commodity fetches fail silently with warnings; missing `net_inflow` defaults to 0.5 factor score
