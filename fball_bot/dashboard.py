"""Live Rich dashboard for terminal display."""

import time
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.text import Text

from .database import get_open_trades, get_tracked_matches, get_daily_analytics
from .ws import PriceTrigger


class Dashboard:
    def __init__(self, price_trigger: PriceTrigger | None = None) -> None:
        self.console = Console()
        self.price_trigger = price_trigger
        self.start_time = time.time()

    def generate_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )
        layout["main"].split_row(
            Layout(name="matches", ratio=2),
            Layout(name="trades", ratio=3),
        )
        
        # Header
        uptime = int(time.time() - self.start_time)
        m, s = divmod(uptime, 60)
        h, m = divmod(m, 60)
        
        if not self.price_trigger:
            ws_status = "🔴 OFFLINE"
        elif self.price_trigger.is_connected:
            ws_status = "🟢 DATA LIVE"
        elif not self.price_trigger._subscriptions:
            ws_status = "🟡 WAITING"
        else:
            ws_status = "🔴 OFFLINE"
        ws_markets = self.price_trigger.connected_markets if self.price_trigger else 0
        
        header_text = Text(f" Fball-bot | Uptime: {h:02d}:{m:02d}:{s:02d} | {ws_status} ({ws_markets} markets) ", style="bold white on blue", justify="center")
        layout["header"].update(Panel(header_text))

        # Live Matches
        matches = get_tracked_matches()
        live_matches = [m for m in matches if m["status"] in ("1H", "2H", "HT", "ET", "P", "live", "LIVE")]
        match_table = Table(show_header=True, header_style="bold magenta", expand=True)
        match_table.add_column("Minute")
        match_table.add_column("Match")
        match_table.add_column("Score")
        match_table.add_column("Price (YES)")

        for m_row in live_matches:
            match_table.add_row(
                f"{m_row['minute']}'",
                f"{m_row['home_team']} vs {m_row['away_team']}",
                f"{m_row['home_score']} - {m_row['away_score']}",
                f"{m_row['current_yes_price']:.3f}",
            )
        layout["matches"].update(Panel(match_table, title="[bold green]Live Matches"))

        # Open Trades
        trades = get_open_trades()
        trade_table = Table(show_header=True, header_style="bold cyan", expand=True)
        trade_table.add_column("ID", width=4)
        trade_table.add_column("Type")
        trade_table.add_column("Size ($)")
        trade_table.add_column("Entry")
        trade_table.add_column("Reason")
        
        for t in trades:
            style = "green" if t["side"] == "BUY" else "red"
            trade_table.add_row(
                str(t["id"]),
                f"[{style}]{t['side']}[/]",
                f"${t['size_usd']:.2f}",
                f"{t['entry_price']:.3f}",
                t.get("entry_reason", ""),
            )
        layout["trades"].update(Panel(trade_table, title="[bold cyan]Open Positions"))

        # Footer (Analytics)
        stats = get_daily_analytics()
        wr = stats['win_rate']
        pnl = stats['pnl']
        color = "green" if pnl >= 0 else "red"
        
        footer_text = Text(
            f" Today: {stats['trades']} trades | Win Rate: {wr:.1f}% | PnL: ${pnl:.2f} ",
            style=f"bold {color}", justify="center"
        )
        layout["footer"].update(Panel(footer_text))

        return layout

    async def start(self) -> None:
        import asyncio
        with Live(self.generate_layout(), refresh_per_second=2) as live:
            while True:
                await asyncio.sleep(0.5)
                live.update(self.generate_layout())
