"""Notifier — Telegram alert system for trade events and bot health.

Fire-and-forget: never blocks the trading hot path.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import httpx

from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


class Notifier:
    """Telegram push notifications for trades and alerts.

    Methods are fire-and-forget — runs in daemon thread, never blocks.
    """

    def __init__(self) -> None:
        self._token = TELEGRAM_BOT_TOKEN
        self._chat_id = TELEGRAM_CHAT_ID
        self._enabled = bool(self._token and self._chat_id)
        if self._enabled:
            self._base = f"https://api.telegram.org/bot{self._token}"
            logger.info("Telegram notifier ready")

    @property
    def available(self) -> bool:
        return self._enabled

    def send(self, text: str, silent: bool = True) -> None:
        """Send a message. Fire-and-forget (daemon thread)."""
        if not self._enabled:
            return
        import threading
        threading.Thread(
            target=lambda: asyncio.run(self._async_send(text, silent)),
            daemon=True,
        ).start()

    def trade_entry(self, side: str, size: float, price: float, reason: str, match: str, scout: str = "") -> None:
        text = (
            f"📗 ENTRY\n"
            f"{side} ${size:.2f} @ {price:.3f}\n"
            f"{match}\n"
            f"Reason: {reason}"
        )
        if scout:
            text += f"\n🧠 Scout: {scout}"
        self.send(text)

    def trade_exit(self, side: str, pnl: float, reason: str, match: str, capital: float) -> None:
        emoji = "🟢" if pnl >= 0 else "🔴"
        text = (
            f"{emoji} EXIT\n"
            f"PnL: ${pnl:.4f}\n"
            f"Capital: ${capital:.2f}\n"
            f"{match}\n"
            f"Reason: {reason}"
        )
        self.send(text)

    def alert(self, msg: str) -> None:
        """Send an alert (e.g. crash, pause, halt). Not silent."""
        self.send(f"⚠️ {msg}", silent=False)

    def daily_summary(self, trades: int, wins: int, losses: int, pnl: float, capital: float) -> None:
        wr = wins / max(wins + losses, 1) * 100
        text = (
            f"📊 Daily Summary\n"
            f"Trades: {trades} ({wins}W/{losses}L)\n"
            f"Win Rate: {wr:.0f}%\n"
            f"PnL: ${pnl:.4f}\n"
            f"Capital: ${capital:.2f}"
        )
        self.send(text)

    def error(self, msg: str) -> None:
        self.send(f"❗ ERROR: {msg}", silent=False)

    # ── Internal ──

    async def _async_send(self, text: str, silent: bool) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                await c.post(
                    f"{self._base}/sendMessage",
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "disable_notification": silent,
                        "parse_mode": "HTML",
                    },
                )
        except Exception as e:
            logger.debug("Telegram send failed: %s", e)
