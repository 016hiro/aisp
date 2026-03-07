"""Typer CLI entry point for A-ISP."""

from __future__ import annotations

import asyncio
from datetime import date

import typer
from rich.console import Console

app = typer.Typer(name="aisp", help="A-Share Intelligence & Strategy Pilot")
console = Console()


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable verbose logging"),
    log_file: str | None = typer.Option(None, "--log-file", help="Log to file"),
):
    """A-Share Intelligence & Strategy Pilot."""
    from aisp.logging_config import setup_logging

    setup_logging(level="DEBUG" if verbose else "INFO", log_file=log_file)


def _run(coro):
    """Run an async coroutine from synchronous CLI context."""
    return asyncio.run(coro)


@app.command()
def init_db():
    """Initialize the database (create all tables)."""
    from aisp.db.engine import init_db as _init_db

    _run(_init_db())
    console.print("[green]Database initialized successfully.[/green]")


@app.command()
def fetch_us(trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today")):
    """Fetch US market data (S&P500, Nasdaq, Dow, key stocks)."""
    from aisp.data.us_market import fetch_us_market

    dt = date.fromisoformat(trade_date) if trade_date else None
    count = _run(fetch_us_market(dt))
    console.print(f"[green]Fetched {count} US market records.[/green]")


@app.command()
def fetch_commodities(
    trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today"),
):
    """Fetch commodity data (gold, copper, oil, etc.)."""
    from aisp.data.commodities import fetch_commodities as _fetch

    dt = date.fromisoformat(trade_date) if trade_date else None
    count = _run(_fetch(dt))
    console.print(f"[green]Fetched {count} commodity records.[/green]")


@app.command()
def fetch_cn(
    trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today"),
    mode: str = typer.Option("full", help="Fetch mode: full | watchlist | codes"),
    codes: str | None = typer.Option(None, help="Comma-separated stock codes (for mode=codes)"),
):
    """Fetch A-share market data (stocks, sectors, fund flow).

    Modes:
      full      - Fetch all stocks from BaoStock industry classification (slow, ~5000 stocks)
      watchlist - Only fetch stocks defined in config/symbols.toml [[cn_watchlist]]
      codes     - Only fetch specified codes, e.g. --codes 600519,000858,601398
    """
    from aisp.data.cn_market import fetch_cn_market

    dt = date.fromisoformat(trade_date) if trade_date else None
    code_list: list[str] | None = None

    if mode == "codes":
        if not codes:
            console.print("[red]--codes is required when mode=codes[/red]")
            raise typer.Exit(1)
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
    elif mode == "watchlist":
        from aisp.data.symbols import load_cn_watchlist

        watchlist = load_cn_watchlist()
        if not watchlist:
            console.print("[red]No stocks in config/symbols.toml [[cn_watchlist]][/red]")
            raise typer.Exit(1)
        code_list = [item["code"] for item in watchlist]
        console.print(f"[dim]Watchlist: {len(code_list)} stocks[/dim]")
    elif mode != "full":
        console.print(f"[red]Unknown mode: {mode}. Use full/watchlist/codes[/red]")
        raise typer.Exit(1)

    result = _run(fetch_cn_market(dt, codes=code_list))
    console.print(f"[green]Fetched CN market ({mode}): {result}[/green]")


@app.command()
def screen(trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today")):
    """Run sector pool filtering and stock scoring."""
    from aisp.screening.sector_pools import SectorPoolManager
    from aisp.screening.stock_scorer import StockScorer

    dt = date.fromisoformat(trade_date) if trade_date else date.today()

    async def _screen():
        pool_mgr = SectorPoolManager()
        pools = await pool_mgr.update_pools(dt)
        scorer = StockScorer()
        results = await scorer.score_all_pools(pools, dt)
        return pools, results

    pools, results = _run(_screen())
    console.print(f"[green]Screening complete: {len(results)} candidates from {len(pools)} pools[/green]")


@app.command()
def analyze(trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today")):
    """Run LLM analysis and generate trading signals."""
    from aisp.engine.analyzer import run_analysis

    dt = date.fromisoformat(trade_date) if trade_date else date.today()
    count = _run(run_analysis(dt))
    console.print(f"[green]Generated {count} signals.[/green]")


@app.command()
def briefing(trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today")):
    """Generate daily briefing report."""
    from aisp.report.briefing import generate_briefing

    dt = date.fromisoformat(trade_date) if trade_date else date.today()
    path = _run(generate_briefing(dt))
    console.print(f"[green]Briefing saved to {path}[/green]")


@app.command()
def run_morning(
    trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today"),
):
    """Morning pipeline: fetch-us → fetch-commodities → fetch-btc → screen → analyze → briefing."""
    from aisp.data.btc_risk import fetch_btc_risk_metrics
    from aisp.data.commodities import fetch_commodities as _fetch_commodities
    from aisp.data.us_market import fetch_us_market
    from aisp.engine.analyzer import run_analysis
    from aisp.report.briefing import generate_briefing
    from aisp.screening.sector_pools import SectorPoolManager
    from aisp.screening.stock_scorer import StockScorer

    dt_parsed = date.fromisoformat(trade_date) if trade_date else None
    dt = dt_parsed or date.today()

    async def _pipeline():
        console.print("[bold]Step 1/6: Fetching US market data...[/bold]")
        await fetch_us_market(dt_parsed)

        console.print("[bold]Step 2/6: Fetching commodity data...[/bold]")
        await _fetch_commodities(dt_parsed)

        console.print("[bold]Step 3/6: Fetching BTC risk metrics...[/bold]")
        btc_metrics = await fetch_btc_risk_metrics()
        if btc_metrics:
            console.print(f"[dim]  BTC ${btc_metrics.price:,.0f} | score={btc_metrics.risk_score:.2f} ({btc_metrics.sentiment_label})[/dim]")
        else:
            console.print("[dim]  BTC data unavailable, continuing without.[/dim]")

        console.print("[bold]Step 4/6: Running sector screening...[/bold]")
        pool_mgr = SectorPoolManager()
        pools = await pool_mgr.update_pools(dt)
        scorer = StockScorer()
        await scorer.score_all_pools(pools, dt)

        console.print("[bold]Step 5/6: Running LLM analysis...[/bold]")
        await run_analysis(dt, btc_metrics=btc_metrics)

        console.print("[bold]Step 6/6: Generating briefing...[/bold]")
        path = await generate_briefing(dt, btc_metrics=btc_metrics)
        return path

    path = _run(_pipeline())
    console.print(f"[bold green]Morning pipeline complete. Briefing: {path}[/bold green]")


@app.command()
def run_close(
    trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today"),
):
    """Close pipeline: fetch-cn → update pools → track performance."""
    from aisp.data.cn_market import fetch_cn_market
    from aisp.review.tracker import PerformanceTracker
    from aisp.screening.sector_pools import SectorPoolManager

    dt_parsed = date.fromisoformat(trade_date) if trade_date else None
    dt = dt_parsed or date.today()

    async def _pipeline():
        console.print("[bold]Step 1/3: Fetching A-share data...[/bold]")
        await fetch_cn_market(dt_parsed)

        console.print("[bold]Step 2/3: Updating sector pools...[/bold]")
        pool_mgr = SectorPoolManager()
        await pool_mgr.update_pools(dt)

        console.print("[bold]Step 3/3: Tracking performance...[/bold]")
        tracker = PerformanceTracker()
        await tracker.evaluate_signals(dt)

    _run(_pipeline())
    console.print("[bold green]Close pipeline complete.[/bold green]")


@app.command()
def status():
    """Show current pool state and active signals."""
    from aisp.engine.signals import show_status

    _run(show_status())
