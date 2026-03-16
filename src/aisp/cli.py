"""Typer CLI entry point for A-ISP."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Annotated

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


async def _latest_cn_trade_date() -> date | None:
    """Find the latest trade_date that has CN stock data in DB."""
    from sqlalchemy import func, select

    from aisp.db.engine import get_engine, get_session_factory
    from aisp.db.models import StkDaily

    engine = get_engine()
    session_factory = get_session_factory(engine)
    async with session_factory() as session:
        result = await session.scalar(
            select(func.max(StkDaily.trade_date))
        )
    await engine.dispose()
    return result


async def _ensure_cn_data(dt: date, code_list: list[str] | None = None) -> None:
    """Auto-fetch CN stock + sector data if missing for the target date.

    Checks stk_daily count for target codes; if zero, fetches watchlist
    stocks and sector daily data from BaoStock/AkShare.
    """
    from sqlalchemy import func, select

    from aisp.db.engine import get_engine, get_session_factory
    from aisp.db.models import StkDaily

    engine = get_engine()
    sf = get_session_factory(engine)

    # Check if we have any stock data for target date
    check_codes = code_list
    if not check_codes:
        from aisp.data.symbols import load_cn_watchlist

        wl = load_cn_watchlist()
        check_codes = [item["code"] for item in wl] if wl else None

    async with sf() as session:
        q = select(func.count(StkDaily.code)).where(StkDaily.trade_date == dt)
        if check_codes:
            q = q.where(StkDaily.code.in_(check_codes))
        count = await session.scalar(q)

    await engine.dispose()

    if count and count > 0:
        return  # data exists

    console.print(f"[yellow]No CN data for {dt}, auto-fetching...[/yellow]")

    from aisp.data.cn_market import fetch_cn_market

    # Determine fetch codes: use code_list if given, else watchlist
    fetch_codes = code_list
    if not fetch_codes:
        from aisp.data.symbols import load_cn_watchlist

        wl = load_cn_watchlist()
        fetch_codes = [item["code"] for item in wl] if wl else None

    try:
        result = await fetch_cn_market(dt, codes=fetch_codes)
        mode_label = f"watchlist ({len(fetch_codes)} stocks)" if fetch_codes else "full"
        console.print(f"[green]  Auto-fetched CN data ({mode_label}): {result}[/green]")
    except Exception as e:
        console.print(f"[red]  Auto-fetch failed: {e}[/red]")
        console.print("[yellow]  Continuing with available data...[/yellow]")


async def _ensure_global_data(dt: date) -> None:
    """Auto-fetch US market + commodity data if missing for the target date.

    Checks global_daily count for the date; if zero or very few records,
    fetches US market and commodity data before proceeding.
    """
    from sqlalchemy import func, select

    from aisp.db.engine import get_engine, get_session_factory
    from aisp.db.models import GlobalDaily

    engine = get_engine()
    sf = get_session_factory(engine)

    async with sf() as session:
        count = await session.scalar(
            select(func.count(GlobalDaily.symbol)).where(GlobalDaily.trade_date == dt)
        )

    await engine.dispose()

    if count and count >= 3:
        return  # enough data exists

    console.print(f"[yellow]Insufficient global data for {dt} ({count or 0} records), auto-fetching...[/yellow]")

    from aisp.data.commodities import fetch_commodities as _fetch_commodities
    from aisp.data.us_market import fetch_us_market

    try:
        us_count = await fetch_us_market(dt)
        console.print(f"[green]  Auto-fetched US market: {us_count} records[/green]")
    except Exception as e:
        console.print(f"[yellow]  US market fetch failed: {e}[/yellow]")

    try:
        cm_count = await _fetch_commodities(dt)
        console.print(f"[green]  Auto-fetched commodities: {cm_count} records[/green]")
    except Exception as e:
        console.print(f"[yellow]  Commodity fetch failed: {e}[/yellow]")


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
    mode: str = typer.Option("watchlist", help="Fetch mode: watchlist | full | codes"),
    codes: str | None = typer.Option(None, help="Comma-separated stock codes (for mode=codes)"),
):
    """Fetch A-share market data (stocks, sectors, fund flow).

    Modes:
      watchlist - Only fetch stocks defined in config/symbols.toml [[cn_watchlist]] (default)
      full      - Fetch all stocks from BaoStock industry classification (slow, ~5000 stocks)
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
            console.print("[red]No stocks in config/watchlist.toml [[cn_watchlist]][/red]")
            raise typer.Exit(1)
        code_list = [item["code"] for item in watchlist]
        console.print(f"[dim]Watchlist: {len(code_list)} stocks[/dim]")
    elif mode != "full":
        console.print(f"[red]Unknown mode: {mode}. Use full/watchlist/codes[/red]")
        raise typer.Exit(1)

    result = _run(fetch_cn_market(dt, codes=code_list))
    console.print(f"[green]Fetched CN market ({mode}): {result}[/green]")


@app.command()
def screen(
    trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today"),
    codes: str | None = typer.Option(None, help="Comma-separated stock codes to display (default: all)"),
):
    """Run sector pool filtering and stock scoring."""
    from aisp.screening.sector_pools import SectorPoolManager
    from aisp.screening.stock_scorer import StockScorer

    dt = date.fromisoformat(trade_date) if trade_date else date.today()
    code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else None

    async def _screen():
        scorer = StockScorer()
        if code_list:
            # Targeted: code → find sector → score, no top-N
            results = await scorer.score_by_codes(code_list, dt)
            return None, results
        else:
            # Batch: update pools → top N per sector
            pool_mgr = SectorPoolManager()
            pools = await pool_mgr.update_pools(dt)
            results = await scorer.score_all_pools(pools, dt)
            return pools, results

    pools, results = _run(_screen())

    if code_list:
        for s in results:
            console.print(f"\n[bold]{s.name}({s.code})[/bold] 板块:{s.sector} 池:{s.pool_type.value}")
            console.print(f"  总分: {s.total_score:.4f}  否决: {s.veto or '无'}")
            console.print(f"  因子: {s.factor_scores}")
            console.print(f"  弹性权重: {s.dynamic_weights}")
        console.print(f"\n[green]{len(results)} stocks scored[/green]")
    else:
        console.print(f"[green]Screening complete: {len(results)} candidates from {len(pools)} pools[/green]")


@app.command()
def analyze(
    trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today"),
    codes: str | None = typer.Option(None, help="Comma-separated stock codes to analyze (default: all)"),
):
    """Run LLM analysis and generate trading signals."""
    from aisp.engine.analyzer import run_analysis

    dt = date.fromisoformat(trade_date) if trade_date else date.today()
    code_list = [c.strip() for c in codes.split(",") if c.strip()] if codes else None
    count = _run(run_analysis(dt, codes=code_list))
    console.print(f"[green]Generated {count} signals.[/green]")


@app.command()
def briefing(trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today")):
    """Generate daily briefing report."""
    from aisp.report.briefing import generate_briefing

    dt = date.fromisoformat(trade_date) if trade_date else date.today()
    path = _run(generate_briefing(dt))
    console.print(f"[green]Briefing saved to {path}[/green]")


@app.command()
def show(
    trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default latest"),
):
    """Open TUI briefing viewer (interactive dashboard)."""
    from aisp.tui import run_tui

    dt = date.fromisoformat(trade_date) if trade_date else None
    run_tui(dt)


@app.command()
def run_analysis_pipeline(
    trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today"),
    codes: str | None = typer.Option(None, help="Comma-separated stock codes (default: watchlist)"),
):
    """screen → analyze → briefing (3-in-1 pipeline).

    Without --codes, automatically loads watchlist from config/symbols.toml
    to ensure all watchlist stocks are analyzed (not just pool top-N).

    If CN stock data is missing for the target date, auto-fetches watchlist
    stocks before proceeding.
    """
    from aisp.data.symbols import load_cn_watchlist
    from aisp.engine.analyzer import run_analysis
    from aisp.report.briefing import generate_briefing
    from aisp.screening.sector_pools import SectorPoolManager
    from aisp.screening.stock_scorer import StockScorer

    dt = date.fromisoformat(trade_date) if trade_date else date.today()
    if codes:
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
    else:
        watchlist = load_cn_watchlist()
        code_list = [item["code"] for item in watchlist] if watchlist else None
        if code_list:
            console.print(f"[dim]Watchlist: {len(code_list)} stocks[/dim]")

    async def _pipeline():
        # Auto-fetch missing data before analysis
        await _ensure_cn_data(dt, code_list)
        await _ensure_global_data(dt)

        console.print("[bold]Step 1/3: Running sector screening...[/bold]")
        pool_mgr = SectorPoolManager()
        pools = await pool_mgr.update_pools(dt)
        scorer = StockScorer()
        await scorer.score_all_pools(pools, dt)

        console.print("[bold]Step 2/3: Running LLM analysis...[/bold]")
        count = await run_analysis(dt, codes=code_list)
        console.print(f"[dim]  Generated {count} signals[/dim]")

        console.print("[bold]Step 3/4: Evaluating signal performance...[/bold]")
        from aisp.review.tracker import PerformanceTracker

        tracker = PerformanceTracker()
        eval_count = await tracker.evaluate_signals(dt)
        console.print(f"[dim]  Evaluated {eval_count} signals[/dim]")

        console.print("[bold]Step 4/4: Generating briefing...[/bold]")
        path = await generate_briefing(dt)
        return path

    path = _run(_pipeline())
    console.print(f"[bold green]Pipeline complete. Briefing: {path}[/bold green]")


@app.command()
def dump_prompts(
    trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today"),
    codes: str | None = typer.Option(None, help="Comma-separated stock codes (default: watchlist)"),
    output_dir: str = typer.Option("prompts", "--output", "-o", help="Output directory"),
):
    """Dump Agent analysis prompts to files (no LLM call).

    Runs the full screening pipeline, then writes each stock's complete prompt
    (system + tools + user) to a markdown file for testing in other agent systems.
    """
    from aisp.data.symbols import load_cn_watchlist
    from aisp.engine.analyzer import run_analysis

    dt = date.fromisoformat(trade_date) if trade_date else date.today()
    if codes:
        code_list = [c.strip() for c in codes.split(",") if c.strip()]
    else:
        watchlist = load_cn_watchlist()
        code_list = [item["code"] for item in watchlist] if watchlist else None
        if code_list:
            console.print(f"[dim]Watchlist: {len(code_list)} stocks[/dim]")

    prompt_path = Path(output_dir) / str(dt)

    async def _pipeline():
        await _ensure_cn_data(dt, code_list)
        await _ensure_global_data(dt)

        console.print("[bold]Step 1/2: Running sector screening...[/bold]")
        from aisp.screening.sector_pools import SectorPoolManager
        from aisp.screening.stock_scorer import StockScorer

        pool_mgr = SectorPoolManager()
        pools = await pool_mgr.update_pools(dt)
        scorer = StockScorer()
        await scorer.score_all_pools(pools, dt)

        console.print("[bold]Step 2/2: Dumping prompts...[/bold]")
        await run_analysis(dt, codes=code_list, prompt_dir=prompt_path)

    _run(_pipeline())
    console.print(f"[bold green]Prompts saved to {prompt_path}/[/bold green]")


@app.command()
def run_morning(
    trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today"),
):
    """Morning pipeline: fetch-us → fetch-commodities → fetch-btc → screen → analyze → briefing."""
    from aisp.data.btc_risk import fetch_btc_risk_metrics
    from aisp.data.commodities import fetch_commodities as _fetch_commodities
    from aisp.data.symbols import load_cn_watchlist
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

        # Morning pipeline uses latest available CN trade_date for screening/analysis
        # (today's CN data won't exist yet — A-share hasn't opened)
        cn_date = dt if dt_parsed else await _latest_cn_trade_date()
        if not cn_date:
            console.print("[yellow]No CN data in DB, skipping screening/analysis.[/yellow]")
            return None
        if cn_date != dt:
            console.print(f"[dim]  Using latest CN data: {cn_date} (today's A-share not yet available)[/dim]")

        console.print("[bold]Step 4/6: Running sector screening...[/bold]")
        pool_mgr = SectorPoolManager()
        pools = await pool_mgr.update_pools(cn_date)
        scorer = StockScorer()
        await scorer.score_all_pools(pools, cn_date)

        # Ensure all watchlist stocks are analyzed (not just those in active pools)
        watchlist = load_cn_watchlist()
        wl_codes = [item["code"] for item in watchlist] if watchlist else None

        console.print("[bold]Step 5/6: Running LLM analysis...[/bold]")
        await run_analysis(cn_date, btc_metrics=btc_metrics, codes=wl_codes)

        console.print("[bold]Step 6/6: Generating briefing...[/bold]")
        path = await generate_briefing(cn_date, btc_metrics=btc_metrics)
        return path

    path = _run(_pipeline())
    console.print(f"[bold green]Morning pipeline complete. Briefing: {path}[/bold green]")


@app.command()
def run_close(
    trade_date: str | None = typer.Option(None, help="Date YYYY-MM-DD, default today"),
):
    """Close pipeline: fetch-cn (watchlist) → update pools → track performance."""
    from aisp.data.cn_market import fetch_cn_market
    from aisp.data.symbols import load_cn_watchlist
    from aisp.review.tracker import PerformanceTracker
    from aisp.screening.sector_pools import SectorPoolManager

    dt_parsed = date.fromisoformat(trade_date) if trade_date else None
    dt = dt_parsed or date.today()

    async def _pipeline():
        # Fetch watchlist stocks + all sector daily data (THS)
        watchlist = load_cn_watchlist()
        code_list = [item["code"] for item in watchlist] if watchlist else None
        mode_label = f"watchlist ({len(code_list)} stocks)" if code_list else "full market"
        console.print(f"[bold]Step 1/3: Fetching A-share data ({mode_label})...[/bold]")
        await fetch_cn_market(dt_parsed, codes=code_list)

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


# ── Portfolio: import & view ──────────────────────────────


@app.command()
def import_positions(
    screenshots: Annotated[list[Path], typer.Argument(help="One or more screenshot files (png/jpg/webp)")],
    trade_date: str | None = typer.Option(None, "--date", "-d", help="Snapshot date YYYY-MM-DD (override OCR)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Import positions from broker app screenshots via LLM OCR."""
    from rich.table import Table

    from aisp.portfolio.ocr import extract_positions as _extract

    async def _do():
        data = await _extract(screenshots)
        return data

    data = _run(_do())
    # Override snapshot_date if user specified --date
    if trade_date:
        data["snapshot_date"] = trade_date
    positions = data.get("positions") or []
    if not positions:
        console.print("[yellow]No positions extracted from screenshots.[/yellow]")
        return

    # Display confidence & warnings
    confidence = data.get("confidence", 0)
    console.print(f"\n[bold]Extracted {len(positions)} positions[/bold]  "
                  f"(date: {data['snapshot_date']}, confidence: {confidence:.0%})")
    for w in data.get("warnings") or []:
        console.print(f"  [yellow]⚠ {w}[/yellow]")

    # Rich table preview
    table = Table(title="Positions Preview")
    table.add_column("Code", style="cyan")
    table.add_column("Name")
    table.add_column("Qty", justify="right")
    table.add_column("Avail", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("P/L", justify="right")
    table.add_column("P/L%", justify="right")

    for p in positions:
        pl = p.get("profit_loss")
        pl_pct = p.get("profit_loss_pct")
        pl_style = "red" if (pl and pl > 0) else "green" if (pl and pl < 0) else ""
        table.add_row(
            p.get("code", ""),
            p.get("name", ""),
            str(p.get("quantity", "")),
            str(p.get("available_quantity", "")),
            f"{p['avg_cost']:.2f}" if p.get("avg_cost") else "",
            f"{p['current_price']:.2f}" if p.get("current_price") else "",
            f"[{pl_style}]{pl:+.2f}[/{pl_style}]" if pl is not None else "",
            f"[{pl_style}]{pl_pct:+.2f}%[/{pl_style}]" if pl_pct is not None else "",
        )
    console.print(table)

    if not yes:
        typer.confirm("Write to database?", abort=True)

    from aisp.portfolio.importer import import_positions as _import

    count = _run(_import(data))
    console.print(f"[green]Imported {count} position snapshots.[/green]")


@app.command()
def import_trades(
    screenshots: Annotated[list[Path], typer.Argument(help="One or more screenshot files (png/jpg/webp)")],
    trade_date: str | None = typer.Option(None, "--date", "-d", help="Trade date YYYY-MM-DD (override OCR)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Import trade records from broker app screenshots via LLM OCR."""
    from rich.table import Table

    from aisp.portfolio.ocr import extract_trades as _extract

    async def _do():
        data = await _extract(screenshots)
        return data

    data = _run(_do())
    # Override trade_date for all trades if user specified --date
    if trade_date:
        for t in data.get("trades") or []:
            t["trade_date"] = trade_date
    trades = data.get("trades") or []
    if not trades:
        console.print("[yellow]No trades extracted from screenshots.[/yellow]")
        return

    confidence = data.get("confidence", 0)
    console.print(f"\n[bold]Extracted {len(trades)} trades[/bold]  "
                  f"(confidence: {confidence:.0%})")
    for w in data.get("warnings") or []:
        console.print(f"  [yellow]⚠ {w}[/yellow]")

    table = Table(title="Trades Preview")
    table.add_column("Date", style="dim")
    table.add_column("Code", style="cyan")
    table.add_column("Name")
    table.add_column("Dir")
    table.add_column("Price", justify="right")
    table.add_column("Qty", justify="right")
    table.add_column("Amount", justify="right")
    table.add_column("Fees", justify="right")

    for t in trades:
        direction = t.get("trade_direction", "")
        dir_style = "red" if direction == "buy" else "green"
        dir_label = "买入" if direction == "buy" else "卖出"
        total_cost = t.get("total_cost")
        table.add_row(
            t.get("trade_date", ""),
            t.get("code", ""),
            t.get("name", ""),
            f"[{dir_style}]{dir_label}[/{dir_style}]",
            f"{t['price']:.2f}" if t.get("price") else "",
            str(t.get("quantity", "")),
            f"{t['amount']:,.2f}" if t.get("amount") else "",
            f"{total_cost:.2f}" if total_cost is not None else "",
        )
    console.print(table)

    if not yes:
        typer.confirm("Write to database?", abort=True)

    from aisp.portfolio.importer import import_trades as _import

    count = _run(_import(data))
    console.print(f"[green]Imported {count} trade records.[/green]")


@app.command()
def positions(
    trade_date: str | None = typer.Option(None, "--date", "-d", help="Date YYYY-MM-DD, default latest"),
):
    """View position snapshots."""
    from sqlalchemy import func, select

    from aisp.db.engine import get_engine, get_session_factory
    from aisp.db.models import PositionSnapshot

    async def _query():
        engine = get_engine()
        sf = get_session_factory(engine)
        async with sf() as session:
            if trade_date:
                dt = date.fromisoformat(trade_date)
            else:
                dt = await session.scalar(
                    select(func.max(PositionSnapshot.snapshot_date))
                )
            if not dt:
                return None, []
            result = await session.execute(
                select(PositionSnapshot)
                .where(PositionSnapshot.snapshot_date == dt)
                .order_by(PositionSnapshot.code)
            )
            rows = result.scalars().all()
        await engine.dispose()
        return dt, rows

    dt, rows = _run(_query())
    if not rows:
        console.print("[dim]No position snapshots found.[/dim]")
        return

    from rich.table import Table

    table = Table(title=f"Positions — {dt}")
    table.add_column("Code", style="cyan")
    table.add_column("Name")
    table.add_column("Qty", justify="right")
    table.add_column("Avail", justify="right")
    table.add_column("Cost", justify="right")
    table.add_column("Price", justify="right")
    table.add_column("Mkt Value", justify="right")
    table.add_column("P/L", justify="right")
    table.add_column("P/L%", justify="right")
    table.add_column("Today P/L", justify="right")

    total_mv = 0.0
    total_pl = 0.0
    for r in rows:
        pl_style = "red" if (r.profit_loss and r.profit_loss > 0) else (
            "green" if (r.profit_loss and r.profit_loss < 0) else ""
        )
        today_style = "red" if (r.today_profit_loss and r.today_profit_loss > 0) else (
            "green" if (r.today_profit_loss and r.today_profit_loss < 0) else ""
        )
        if r.market_value:
            total_mv += r.market_value
        if r.profit_loss:
            total_pl += r.profit_loss
        table.add_row(
            r.code,
            r.name,
            str(r.quantity),
            str(r.available_quantity) if r.available_quantity is not None else "",
            f"{r.avg_cost:.2f}",
            f"{r.current_price:.2f}" if r.current_price else "",
            f"{r.market_value:,.2f}" if r.market_value else "",
            f"[{pl_style}]{r.profit_loss:+,.2f}[/{pl_style}]" if r.profit_loss is not None else "",
            f"[{pl_style}]{r.profit_loss_pct:+.2f}%[/{pl_style}]" if r.profit_loss_pct is not None else "",
            f"[{today_style}]{r.today_profit_loss:+,.2f}[/{today_style}]" if r.today_profit_loss is not None else "",
        )

    console.print(table)
    pl_total_style = "red" if total_pl > 0 else "green" if total_pl < 0 else ""
    console.print(
        f"  Total market value: {total_mv:,.2f}  |  "
        f"Total P/L: [{pl_total_style}]{total_pl:+,.2f}[/{pl_total_style}]"
    )


@app.command()
def trades(
    trade_date: str | None = typer.Option(None, "--date", "-d", help="Date YYYY-MM-DD"),
    days: int = typer.Option(7, "--days", "-n", help="Show trades from last N days"),
):
    """View trade records."""
    from datetime import timedelta

    from sqlalchemy import select

    from aisp.db.engine import get_engine, get_session_factory
    from aisp.db.models import TradeRecord

    async def _query():
        engine = get_engine()
        sf = get_session_factory(engine)
        async with sf() as session:
            q = select(TradeRecord)
            if trade_date:
                dt = date.fromisoformat(trade_date)
                q = q.where(TradeRecord.trade_date == dt)
            else:
                cutoff = date.today() - timedelta(days=days)
                q = q.where(TradeRecord.trade_date >= cutoff)
            q = q.order_by(TradeRecord.trade_date.desc(), TradeRecord.code)
            result = await session.execute(q)
            rows = result.scalars().all()
        await engine.dispose()
        return rows

    rows = _run(_query())
    if not rows:
        console.print("[dim]No trade records found.[/dim]")
        return

    from rich.table import Table

    title = f"Trades — {trade_date}" if trade_date else f"Trades — last {days} days"
    table = Table(title=title)
    table.add_column("Date", style="dim")
    table.add_column("Code", style="cyan")
    table.add_column("Name")
    table.add_column("Dir")
    table.add_column("Price", justify="right")
    table.add_column("Qty", justify="right")
    table.add_column("Amount", justify="right")
    table.add_column("Fees", justify="right")
    table.add_column("Net", justify="right")

    for r in rows:
        dir_style = "red" if r.trade_direction.value == "buy" else "green"
        dir_label = "买入" if r.trade_direction.value == "buy" else "卖出"
        table.add_row(
            r.trade_date.isoformat(),
            r.code,
            r.name,
            f"[{dir_style}]{dir_label}[/{dir_style}]",
            f"{r.price:.2f}",
            str(r.quantity),
            f"{r.amount:,.2f}",
            f"{r.total_cost:.2f}" if r.total_cost is not None else "",
            f"{r.net_amount:,.2f}" if r.net_amount is not None else "",
        )

    console.print(table)
    console.print(f"  {len(rows)} records total")


# ── Natural language watchlist ─────────────────────────────


@app.command()
def telegram():
    """Start Telegram bot (long polling)."""
    from aisp.telegram.bot import run_bot

    run_bot()


@app.command()
def watch(
    query: str = typer.Argument(help="Natural language command, e.g. '添加天赐材料' / 'remove TSLA'"),
):
    """Manage watchlist with natural language (LLM-powered).

    Examples:
      aisp watch "添加天赐材料"
      aisp watch "关注苹果和英伟达"
      aisp watch "删除工商银行"
      aisp watch "观察列表里有什么"
      aisp watch "add gold futures"
    """
    from aisp.watch_nlp import handle_watch_query

    messages = _run(handle_watch_query(query))
    for msg in messages:
        console.print(msg)


# ── Symbol management (structured) ───────────────────────

_SECTION_HELP = (
    "Section: cn (A股自选), us (美股), yf (国际大宗), ak (国内大宗)"
)

# Maps CLI shorthand → (toml_section, key_field)
_SECTION_MAP = {
    "cn": ("cn_watchlist", "code"),
    "us": ("us_market", "symbol"),
    "yf": ("yf_commodities", "symbol"),
    "ak": ("ak_commodities", "symbol"),
}


@app.command()
def watch_add(
    identifier: str = typer.Argument(help="Stock code or symbol, e.g. 002709 / AAPL / GC=F"),
    name: str = typer.Argument(help="Display name, e.g. 天赐材料 / Apple / Gold Futures"),
    section: str = typer.Option("cn", "--type", "-t", help=_SECTION_HELP),
    asset_type: str | None = typer.Option(
        None, "--asset-type", "-a", help="Asset type for us_market: index/stock/commodity"
    ),
):
    """Add a symbol to the watchlist or market config."""
    from aisp.data.symbols import add_symbol

    if section not in _SECTION_MAP:
        console.print(f"[red]Unknown section: {section}. Use: {list(_SECTION_MAP)}[/red]")
        raise typer.Exit(1)

    toml_section, key_field = _SECTION_MAP[section]
    fields: dict[str, str] = {"name": name}

    if section == "us":
        fields["asset_type"] = asset_type or "stock"

    if add_symbol(toml_section, key_field, identifier, fields):
        console.print(f"[green]Added {name}({identifier}) to [{toml_section}][/green]")
    else:
        console.print(f"[yellow]{identifier} already in [{toml_section}][/yellow]")


@app.command()
def watch_rm(
    identifier: str = typer.Argument(help="Stock code or symbol to remove"),
    section: str = typer.Option("cn", "--type", "-t", help=_SECTION_HELP),
):
    """Remove a symbol from the watchlist or market config."""
    from aisp.data.symbols import remove_symbol

    if section not in _SECTION_MAP:
        console.print(f"[red]Unknown section: {section}. Use: {list(_SECTION_MAP)}[/red]")
        raise typer.Exit(1)

    toml_section, key_field = _SECTION_MAP[section]

    if remove_symbol(toml_section, key_field, identifier):
        console.print(f"[green]Removed {identifier} from [{toml_section}][/green]")
    else:
        console.print(f"[yellow]{identifier} not found in [{toml_section}][/yellow]")


@app.command()
def watch_ls(
    section: str = typer.Option("cn", "--type", "-t", help=_SECTION_HELP),
):
    """List all symbols in a config section."""
    from rich.table import Table

    from aisp.data.symbols import list_section

    if section not in _SECTION_MAP:
        console.print(f"[red]Unknown section: {section}. Use: {list(_SECTION_MAP)}[/red]")
        raise typer.Exit(1)

    toml_section, key_field = _SECTION_MAP[section]
    items = list_section(toml_section)

    if not items:
        console.print(f"[dim][{toml_section}] is empty[/dim]")
        return

    table = Table(title=f"{toml_section} ({len(items)} entries)")
    table.add_column(key_field.capitalize(), style="cyan")
    table.add_column("Name", style="green")
    if section == "us":
        table.add_column("Type", style="dim")

    for item in items:
        row = [item.get(key_field, ""), item.get("name", "")]
        if section == "us":
            row.append(item.get("asset_type", ""))
        table.add_row(*row)

    console.print(table)
