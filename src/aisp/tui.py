"""TUI briefing viewer — interactive terminal dashboard."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Footer, Header, Label, ListItem, ListView, Markdown

from aisp.config import get_settings


def _list_briefing_dates(briefing_dir: Path) -> list[date]:
    """List available briefing dates (newest first)."""
    dates: list[date] = []
    for f in briefing_dir.glob("*.md"):
        m = re.match(r"(\d{4}-\d{2}-\d{2})\.md$", f.name)
        if m:
            dates.append(date.fromisoformat(m.group(1)))
    dates.sort(reverse=True)
    return dates


class BriefingApp(App):
    """A-ISP Briefing TUI Viewer."""

    TITLE = "A-ISP 每日简报"

    CSS = """
    #sidebar {
        width: 18;
        border-right: solid $primary-background;
    }
    #sidebar ListView {
        height: 1fr;
    }
    #sidebar Label {
        text-style: bold;
        padding: 0 1;
        color: $text-muted;
    }
    #scroll-area {
        width: 1fr;
    }
    #scroll-area Markdown {
        padding: 0 1;
    }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("q", "quit", "退出"),
        Binding("j,down", "scroll_down", "下滚", priority=True),
        Binding("k,up", "scroll_up", "上滚", priority=True),
        Binding("space", "page_down", "翻页↓", priority=True),
        Binding("b", "page_up", "翻页↑", priority=True),
        Binding("left,h", "prev_date", "上一日"),
        Binding("right,l", "next_date", "下一日"),
        Binding("g", "scroll_home", "顶部", show=False, priority=True),
        Binding("shift+g", "scroll_end", "底部", show=False, priority=True),
        Binding("r", "reload", "刷新"),
    ]

    def __init__(self, initial_date: date | None = None):
        super().__init__()
        self.briefing_dir = get_settings().briefing_dir
        self.dates = _list_briefing_dates(self.briefing_dir)
        self.current_idx = 0
        if initial_date and initial_date in self.dates:
            self.current_idx = self.dates.index(initial_date)

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="sidebar"):
                yield Label("日期列表")
                yield ListView(
                    *[ListItem(Label(d.isoformat())) for d in self.dates],
                    id="date-list",
                )
            with VerticalScroll(id="scroll-area"):
                yield Markdown("", id="briefing-md")
        yield Footer()

    def _scroll_area(self) -> VerticalScroll:
        return self.query_one("#scroll-area", VerticalScroll)

    def on_mount(self) -> None:
        # Focus the scroll area so scroll keys work immediately
        self._scroll_area().focus()
        if self.dates:
            lv = self.query_one("#date-list", ListView)
            lv.index = self.current_idx
            self._load_briefing(self.current_idx)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.current_idx = event.list_view.index or 0
        self._load_briefing(self.current_idx)
        self._scroll_area().focus()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        idx = event.list_view.index
        if idx is not None:
            self.current_idx = idx
            self._load_briefing(idx)

    def _load_briefing(self, idx: int) -> None:
        if not self.dates or idx < 0 or idx >= len(self.dates):
            return
        d = self.dates[idx]
        filepath = self.briefing_dir / f"{d}.md"
        content = filepath.read_text(encoding="utf-8") if filepath.exists() else f"# {d}\n\n*文件不存在*"
        md = self.query_one("#briefing-md", Markdown)
        md.update(content)
        self._scroll_area().scroll_home(animate=False)
        self.sub_title = d.isoformat()

    def action_prev_date(self) -> None:
        if self.dates and self.current_idx < len(self.dates) - 1:
            self.current_idx += 1
            lv = self.query_one("#date-list", ListView)
            lv.index = self.current_idx
            self._load_briefing(self.current_idx)

    def action_next_date(self) -> None:
        if self.dates and self.current_idx > 0:
            self.current_idx -= 1
            lv = self.query_one("#date-list", ListView)
            lv.index = self.current_idx
            self._load_briefing(self.current_idx)

    def action_scroll_down(self) -> None:
        self._scroll_area().scroll_relative(y=3, animate=False)

    def action_scroll_up(self) -> None:
        self._scroll_area().scroll_relative(y=-3, animate=False)

    def action_page_down(self) -> None:
        sa = self._scroll_area()
        sa.scroll_relative(y=sa.size.height - 2, animate=False)

    def action_page_up(self) -> None:
        sa = self._scroll_area()
        sa.scroll_relative(y=-(sa.size.height - 2), animate=False)

    def action_scroll_home(self) -> None:
        self._scroll_area().scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        self._scroll_area().scroll_end(animate=False)

    def action_reload(self) -> None:
        self.dates = _list_briefing_dates(self.briefing_dir)
        lv = self.query_one("#date-list", ListView)
        lv.clear()
        for d in self.dates:
            lv.append(ListItem(Label(d.isoformat())))
        if self.dates:
            self.current_idx = min(self.current_idx, len(self.dates) - 1)
            lv.index = self.current_idx
            self._load_briefing(self.current_idx)


def run_tui(trade_date: date | None = None) -> None:
    """Launch the TUI briefing viewer."""
    app = BriefingApp(initial_date=trade_date)
    app.run()
