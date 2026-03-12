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
      watchlist - Only fetch stocks defined in config/watchlist.toml [[cn_watchlist]]
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

    Without --codes, automatically loads watchlist from config/watchlist.toml
    to ensure all watchlist stocks are analyzed (not just pool top-N).
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
        console.print("[bold]Step 1/3: Running sector screening...[/bold]")
        pool_mgr = SectorPoolManager()
        pools = await pool_mgr.update_pools(dt)
        scorer = StockScorer()
        await scorer.score_all_pools(pools, dt)

        console.print("[bold]Step 2/3: Running LLM analysis...[/bold]")
        count = await run_analysis(dt, codes=code_list)
        console.print(f"[dim]  Generated {count} signals[/dim]")

        console.print("[bold]Step 3/3: Generating briefing...[/bold]")
        path = await generate_briefing(dt)
        return path

    path = _run(_pipeline())
    console.print(f"[bold green]Pipeline complete. Briefing: {path}[/bold green]")


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


# ── Natural language watchlist ─────────────────────────────


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
