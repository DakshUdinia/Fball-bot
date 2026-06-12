"""PortfolioRisk — adaptive, self-calibrating risk management.

Fixes applied:
  - Atomic save: write to .tmp then rename (race condition fix)
  - Drawdown velocity: decays over 24h (de-amplify)
  - Token-aware minimum order: 5-share floor
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent / "data"
STATE_FILE = DATA_DIR / "portfolio_risk.json"

MIN_SHARES = 5  # Polymarket CLOB minimum


@dataclass
class PortfolioState:
    initial_capital: float = 10.0
    current_capital: float = 10.0
    peak_capital: float = 10.0
    total_pnl: float = 0.0
    daily_pnl: float = 0.0
    monthly_pnl: float = 0.0

    wins: int = 0
    losses: int = 0
    total_trades: int = 0
    sum_win_pct: float = 0.0
    sum_loss_pct: float = 0.0

    peak_timestamp: float = field(default_factory=time.time)
    last_drawdown_check: float = field(default_factory=time.time)
    drawdown_at_last_check: float = 0.0
    drawdown_velocity_mult: float = 1.0  # current multiplier (decays over 24h)

    consecutive_losses: int = 0
    consecutive_wins: int = 0
    max_consecutive_losses_seen: int = 0

    is_paused: bool = False
    pause_until: float = 0.0
    pause_reason: str = ""
    permanently_halted: bool = False
    in_recovery: bool = False
    recovery_target: float = 0.0
    recovery_entry_capital: float = 0.0

    last_daily_reset: float = field(default_factory=time.time)
    month_start_time: float = field(default_factory=time.time)
    active_matches_last_cycle: int = 1


class PortfolioRisk:
    """Self-calibrating risk manager — 5 layers + Kelly sizing."""

    def __init__(self, **kwargs: Any) -> None:
        self._cfg = {
            "max_per_trade_usd": float(kwargs.get("max_per_trade_usd", 3.0)),
            "daily_max_loss_pct": float(kwargs.get("daily_max_loss_pct", 0.05)),
            "monthly_max_loss_pct": float(kwargs.get("monthly_max_loss_pct", 0.15)),
            "max_drawdown_pct": float(kwargs.get("max_drawdown_pct", 0.25)),
            "total_max_loss_pct": float(kwargs.get("total_max_loss_pct", 0.40)),
            "max_consecutive_losses": int(kwargs.get("max_consecutive_losses", 3)),
            "pause_minutes": int(kwargs.get("pause_minutes", 120)),
            "initial_capital": float(kwargs.get("initial_capital", 10.0)),
        }
        self.s = self._load_state()

    # ══════════════════════════════════════════════════════════════════

    def can_trade(self, active_match_count: int = 1) -> tuple[bool, str]:
        self._check_resets()
        s = self.s
        s.active_matches_last_cycle = max(active_match_count, 1)

        if s.permanently_halted:
            return False, "PERMANENT HALT — total loss limit reached"

        total_limit = self._cfg["initial_capital"] * self._cfg["total_max_loss_pct"]
        if s.total_pnl <= -total_limit:
            s.permanently_halted = True
            self._save_state()
            logger.error("🔴 PERMANENT HALT — total loss $%.2f", s.total_pnl)
            return False, f"Total loss ${s.total_pnl:.2f} — halted"

        if s.is_paused and time.time() < s.pause_until:
            rem = int(s.pause_until - time.time()) // 60
            return False, f"{s.pause_reason} — {rem}m remaining"
        s.is_paused = False

        if self._effective_drawdown() >= self._cfg["max_drawdown_pct"]:
            self._pause(f"Drawdown {self._effective_drawdown():.1f}%", 7 * 24 * 60)
            return False

        monthly = self._cfg["initial_capital"] * self._cfg["monthly_max_loss_pct"]
        if s.monthly_pnl <= -monthly:
            self._pause("Monthly loss limit", 30 * 24 * 60)
            return False

        daily = self._cfg["initial_capital"] * self._cfg["daily_max_loss_pct"]
        if s.daily_pnl <= -daily:
            self._pause(f"Daily loss ${s.daily_pnl:.2f}", self._cfg["pause_minutes"])
            return False

        if s.consecutive_losses >= self._cfg["max_consecutive_losses"]:
            self._pause(f"{s.consecutive_losses} consecutive losses", self._cfg["pause_minutes"])
            return False

        if s.in_recovery and s.total_pnl >= s.recovery_target:
            s.in_recovery = False
            self._save_state()
            logger.info("🟢 Recovery complete")

        return True, ""

    def compute_max_trade_size(
        self,
        base_size: float = 2.0,
        conviction: float = 0.5,
        token_price: float = 0.50,
    ) -> float:
        """Compute max size for a trade. Returns dollar amount.

        Args:
            base_size: Strategy's base size (e.g. $2)
            conviction: Model confidence 0.0-1.0
            token_price: Current token price (for minimum calculation)

        Returns:
            USD size, floored at exchange minimum ($2.50 typical)
        """
        s = self.s
        hard_cap = self._cfg["max_per_trade_usd"]

        kelly_pct = self._kelly_fraction()
        kelly_dollars = s.current_capital * kelly_pct
        size = kelly_dollars * (0.5 + conviction * 0.5)

        daily = self._cfg["initial_capital"] * self._cfg["daily_max_loss_pct"]
        daily_rem = max(0.1, (daily + s.daily_pnl) / daily) if daily > 0 else 1.0
        size *= daily_rem

        dd_mult = max(0.2, 1.0 - (self._effective_drawdown() / self._cfg["max_drawdown_pct"]))
        size *= dd_mult

        if s.in_recovery:
            size *= 0.4
        # Match density: with speed edge, only reduce for 3+ concurrent matches
        # 1-2 matches = full size, 3+ = gentle reduction
        match_count = max(s.active_matches_last_cycle, 1)
        if match_count <= 2:
            size *= 1.0  # Full size
        else:
            size *= max(0.5, 1.0 - (match_count - 2) * 0.15)  # 3→0.85, 4→0.70, 5→0.55
        if s.consecutive_losses > 0:
            size *= max(0.3, 1.0 - s.consecutive_losses * 0.25)

        # Token-aware minimum: $1 OR 5 shares × token_price, whichever is greater
        min_dollar = max(1.0, MIN_SHARES * token_price)
        size = max(min_dollar, min(size, hard_cap))

        logger.debug(
            "Size: kelly=$%.2f conv=%.2f dd=%.2f → $%.2f (min=$%.2f cap=$%.2f)",
            kelly_dollars, conviction, dd_mult, size, min_dollar, hard_cap,
        )
        return round(size, 2)

    def record_trade(self, pnl: float, entry_price: float) -> dict[str, Any]:
        s = self.s
        s.total_trades += 1
        s.daily_pnl += pnl
        s.monthly_pnl += pnl
        s.total_pnl += pnl
        s.current_capital = s.initial_capital + s.total_pnl

        if s.current_capital > s.peak_capital:
            s.peak_capital = s.current_capital
            s.peak_timestamp = time.time()
            s.drawdown_velocity_mult = 1.0  # Reset velocity on new peak

        if pnl > 0:
            s.wins += 1
            s.consecutive_wins += 1
            s.consecutive_losses = 0
            s.sum_win_pct += abs(pnl / (entry_price * 100)) if entry_price > 0 else 0
        else:
            s.losses += 1
            s.consecutive_losses += 1
            s.consecutive_wins = 0
            s.max_consecutive_losses_seen = max(s.max_consecutive_losses_seen, s.consecutive_losses)
            s.sum_loss_pct += abs(pnl) / (entry_price * 100) if entry_price > 0 else 0

        if s.in_recovery:
            if s.total_pnl >= s.recovery_target:
                s.in_recovery = False
                logger.info("🟢 Recovery target met")
        elif s.consecutive_losses >= self._cfg["max_consecutive_losses"] - 1:
            s.in_recovery = True
            s.recovery_entry_capital = s.current_capital
            s.recovery_target = s.total_pnl + self._cfg["initial_capital"] * 0.02
            logger.info("🔄 Recovery mode — target $%.2f", s.recovery_target)

        self._save_state()
        return {
            "capital": round(s.current_capital, 2),
            "daily_pnl": round(s.daily_pnl, 4),
            "monthly_pnl": round(s.monthly_pnl, 4),
            "total_pnl": round(s.total_pnl, 4),
            "consecutive_losses": s.consecutive_losses,
            "win_rate": round(self._win_rate() * 100, 1),
        }

    def validate_order(self, price: float, size: float, token_price: float = 0.50) -> tuple[bool, str]:
        """Validate order against exchange minimums."""
        min_val = max(1.0, MIN_SHARES * token_price)
        if size < min_val:
            return False, f"${size:.2f} < minimum ${min_val:.2f} ({MIN_SHARES} shares × ${token_price:.3f})"
        if size > self._cfg["max_per_trade_usd"]:
            return False, f"${size:.2f} > max ${self._cfg['max_per_trade_usd']:.2f}"
        return True, ""

    def get_status_summary(self) -> dict[str, Any]:
        s = self.s
        dd = self._drawdown_pct()
        return {
            "capital": {
                "initial": s.initial_capital, "current": round(s.current_capital, 2),
                "peak": round(s.peak_capital, 2), "drawdown_pct": round(dd * 100, 2),
            },
            "pnl": {
                "daily": round(s.daily_pnl, 4), "monthly": round(s.monthly_pnl, 4),
                "total": round(s.total_pnl, 4),
            },
            "kelly_fraction": round(self._kelly_fraction(), 3),
            "win_rate": round(self._win_rate() * 100, 1),
            "consecutive": {"losses": s.consecutive_losses, "wins": s.consecutive_wins},
            "recovery_mode": s.in_recovery,
            "status": "halted" if s.permanently_halted else ("paused" if s.is_paused else "active"),
            "max_trade_size": self.compute_max_trade_size(),
            "total_trades": s.total_trades,
        }

    # ── Kelly ────────────────────────────────────────────────────────

    def _win_rate(self) -> float:
        tot = self.s.wins + self.s.losses
        return self.s.wins / tot if tot > 0 else 0.5

    def _avg_win_loss_ratio(self) -> float:
        s = self.s
        aw = s.sum_win_pct / s.wins if s.wins > 0 else 0.03
        al = s.sum_loss_pct / s.losses if s.losses > 0 else 0.03
        return aw / al if al > 0 else 1.0

    def _kelly_fraction(self) -> float:
        if self.s.total_trades < 5:
            return 0.15
        p = self._win_rate()
        b = self._avg_win_loss_ratio()
        if b <= 0:
            return 0.1
        k = (p * b - (1 - p)) / b
        return max(0.1, min(k * 0.5, 0.5))

    # ── Drawdown ─────────────────────────────────────────────────────

    def _drawdown_pct(self) -> float:
        s = self.s
        if s.peak_capital <= 0:
            return 0.0
        return (s.peak_capital - s.current_capital) / s.peak_capital

    def _effective_drawdown(self) -> float:
        """Velocity-adjusted drawdown with 24h decay.

        - Fast drop (<2h from peak): amplify ×1.5
        - Over 24h: multiplier decays linearly back to 1.0
        - Slow drift (>24h from peak): de-amplify ×0.8
        """
        s = self.s
        dd = self._drawdown_pct()
        if dd <= 0:
            return 0.0

        hours = (time.time() - s.peak_timestamp) / 3600
        if hours < 2:
            mult = 1.5
        elif hours > 24:
            mult = 0.8
        else:
            # Linear decay from 1.5 → 1.0 between 2-24h
            progress = (hours - 2) / 22
            mult = 1.5 - progress * 0.5

        return min(dd * max(mult, 0.8), 0.99)

    # ── Pause ────────────────────────────────────────────────────────

    def _pause(self, reason: str, minutes: int) -> None:
        self.s.is_paused = True
        self.s.pause_until = time.time() + minutes * 60
        self.s.pause_reason = reason
        self.s.in_recovery = True
        self.s.recovery_entry_capital = self.s.current_capital
        self.s.recovery_target = self.s.total_pnl + self._cfg["initial_capital"] * 0.02
        self._save_state()
        logger.warning("⏸ PAUSED: %s — %d min", reason, minutes)

    # ── Resets ───────────────────────────────────────────────────────

    def _check_resets(self) -> None:
        s = self.s
        now = time.time()
        if now - s.last_daily_reset >= 86400:
            logger.info("Daily reset (was: $%.4f)", s.daily_pnl)
            s.daily_pnl = 0.0
            s.last_daily_reset = now
            if s.in_recovery and s.total_pnl >= 0:
                s.in_recovery = False
        if now - s.month_start_time >= 30 * 86400:
            s.monthly_pnl = 0.0
            s.month_start_time = now
        if s.in_recovery and s.total_trades == 0:
            s.in_recovery = False

    # ── Persistence (atomic save) ────────────────────────────────────

    def _load_state(self) -> PortfolioState:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(STATE_FILE) as f:
                return PortfolioState(**json.load(f))
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            ic = self._cfg["initial_capital"]
            logger.info("New risk state: $%.2f", ic)
            return PortfolioState(initial_capital=ic, current_capital=ic, peak_capital=ic)

    def _save_state(self) -> None:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            with open(tmp, "w") as f:
                json.dump(self.s.__dict__, f, indent=2, default=str)
            tmp.rename(STATE_FILE)  # Atomic on most OS
        except Exception as e:
            logger.debug("Save state: %s", e)
