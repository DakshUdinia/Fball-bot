#!/usr/bin/env python3
"""Fball-bot — autonomous World Cup Polymarket scalping bot.

Usage:
    python main.py              # Run continuous trading loop
    python main.py --status     # Quick status check
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime

from rich.console import Console
from rich.table import Table

from fball_bot.config import (
    FOOTBALL_API_KEY, FOOTBALL_CHECK_INTERVAL, PAPER_TRADING,
    INITIAL_CAPITAL, RISK_MAX_PER_TRADE_USD, GEMINI_API_KEY,
)
from fball_bot.bot import FballTradingBot
from fball_bot.database import init_database, get_tracked_matches

console = Console()


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(), logging.FileHandler("fball-bot.log")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def show_splash() -> None:
    console.print()
    console.print("[bold bright_green]")
    console.print("  ╔══════════════════════════════════════════╗")
    console.print("  ║       FBALL-BOT v0.1                     ║")
    console.print("  ║  World Cup Polymarket Scalping Bot       ║")
    console.print("  ║  Gemini 3.5 Flash · PortfolioRisk        ║")
    console.print("  ╚══════════════════════════════════════════╝")
    console.print("[/]")


async def cmd_run() -> None:
    show_splash()
    if not FOOTBALL_API_KEY:
        console.print("[red]ERROR: FOOTBALL_API_KEY not set[/]")
        console.print("Set in .env — get one at https://rapidapi.com/api-sports/api/api-football/")
        sys.exit(1)

    console.print(f"  [dim]Mode: {'PAPER' if PAPER_TRADING else 'LIVE'} | "
                   f"Capital: ${INITIAL_CAPITAL:.2f} | "
                   f"Poll: {FOOTBALL_CHECK_INTERVAL}s[/]")
    console.print(f"  [dim]Gemini: {'READY' if GEMINI_API_KEY else 'OFF'} | "
                   f"Start: {datetime.now().isoformat()}[/]\n")

    bot = FballTradingBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutdown...[/]")
        await bot.stop()


async def cmd_status() -> None:
    show_splash()
    init_database()
    matches = get_tracked_matches()
    live = [m for m in matches if m["status"] == "live"]

    table = Table(show_header=False, border_style="bright_green")
    table.add_column("Setting", style="cyan")
    table.add_column("Value")
    table.add_row("Mode", "PAPER" if PAPER_TRADING else "LIVE")
    table.add_row("Capital", f"${INITIAL_CAPITAL:.2f}")
    table.add_row("Max/Trade", f"${RISK_MAX_PER_TRADE_USD:.2f}")
    table.add_row("API Key", "YES" if FOOTBALL_API_KEY else "NO")
    table.add_row("Gemini", "YES" if GEMINI_API_KEY else "NO (set GEMINI_API_KEY in .env)")
    table.add_row("Tracked", str(len(matches)))
    table.add_row("Live Now", str(len(live)))
    console.print(table)

    if live:
        t = Table(title="Live Matches", border_style="cyan")
        t.add_column("Match", style="yellow")
        t.add_column("Score", style="bright_white")
        t.add_column("Min", style="cyan")
        for m in live:
            t.add_row(f"{m['home_team']} vs {m['away_team']}",
                       f"{m['home_score']}-{m['away_score']}", str(m["minute"]))
        console.print(t)
    else:
        console.print("\n[yellow]No live matches[/]")

    console.print(f"\n[dim]DB: fball_bot/data/fball_bot.db[/]")


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description="Fball-bot — autonomous World Cup scalper")
    parser.add_argument("--status", action="store_true", help="Status check")
    args = parser.parse_args()
    if args.status:
        asyncio.run(cmd_status())
    else:
        asyncio.run(cmd_run())


if __name__ == "__main__":
    main()
