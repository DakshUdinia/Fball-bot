"""Scalping strategy — entry/exit rules for goals, cards, and VAR."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from .live_data import MatchEvent, MatchState
from .models import FootballReversionModel
from .discovery import FootballMarket

logger = logging.getLogger(__name__)

MIN_MOVE = 0.03         # Minimum price move required to enter
MIN_REV = 0.015         # Minimum expected reversion required
BASE_SIZE = 3.0         # Base trade size in USD
MAX_SIZE = 5.0          # Max trade size in USD

# Asymmetric risk/reward: 2:1 reward/risk
PROFIT_TARGET = 0.04    # 4 cents profit target (2x risk)
STOP_LOSS = 0.02        # 2 cents stop loss
PARTIAL_EXIT_LEVEL = 0.025  # Take 50% profit at 2.5 cents, let rest run

TIMEOUT_DEFAULT = 300   # 5 minutes default timeout
TIMEOUT_LATE = 60       # 60 seconds in injury time (85+ min)

COOLDOWN_WIN = 45       # 45 seconds cooldown after a win (re-enter quickly)
COOLDOWN_LOSS = 120     # 120 seconds cooldown after a loss (more caution)
COOLDOWN_DEFAULT = 60   # Default cooldown


@dataclass
class ActiveScalp:
    trade_id: int
    fixture_id: int
    token_id: str
    side: str
    entry_price: float
    size_usd: float
    entry_time: float
    reason: str
    partial_exited: bool = False


@dataclass
class TradeSignal:
    type: str          # entry or exit
    fixture_id: int
    token_id: str
    side: str          # BUY or SELL
    price: float
    size: float
    reason: str
    confidence: float
    trade_id: int | None = None


class ScalpingStrategy:
    """Evaluates matches for scalping opportunities across all event types."""

    def __init__(self, model: FootballReversionModel) -> None:
        self._model = model
        self._active: dict[int, ActiveScalp] = {}
        self._last_trade_time: dict[int, float] = {}
        self._last_trade_result: dict[int, str] = {}

    def register_entry(self, s: ActiveScalp) -> None:
        self._active[s.trade_id] = s
        self._last_trade_time[s.fixture_id] = time.time()

    def register_exit(self, trade_id: int) -> ActiveScalp | None:
        return self._active.pop(trade_id, None)

    def get_active(self, fixture_id: int | None = None) -> list[ActiveScalp]:
        if fixture_id:
            return [s for s in self._active.values() if s.fixture_id == fixture_id]
        return list(self._active.values())

    def active_count(self) -> int:
        return len(self._active)

    def _get_cooldown(self, fixture_id: int) -> float:
        result = self._last_trade_result.get(fixture_id)
        if result == "win": return COOLDOWN_WIN
        if result == "loss": return COOLDOWN_LOSS
        return COOLDOWN_DEFAULT

    async def evaluate(
        self,
        fixture_id: int,
        state: MatchState | None,
        market: FootballMarket | None,
        new_events: list[MatchEvent],
        orderbook_imbalance: float = 1.0,  # 1.0 = balanced. >1 = buy pressure
    ) -> list[TradeSignal]:
        signals: list[TradeSignal] = []
        if not state or not market or not market.yes_token_id:
            return signals

        # Hard blocks
        if abs(state.home_score - state.away_score) >= 3:
            return signals
        if state.minute >= 90 and state.home_score + state.away_score == 0:
            return signals
        if state.status in ("finished", "cancelled"):
            return signals

        timeout = TIMEOUT_LATE if state.minute >= 85 else TIMEOUT_DEFAULT

        # Check exits
        for s in self.get_active(fixture_id):
            price = market.current_yes_price
            pnl_per_share = (s.entry_price - price) if s.side == "SELL" else (price - s.entry_price)
            elapsed = time.time() - s.entry_time
            exit_side = "BUY" if s.side == "SELL" else "SELL"

            if not s.partial_exited and pnl_per_share >= PARTIAL_EXIT_LEVEL:
                signals.append(TradeSignal(
                    "exit", fixture_id, s.token_id, exit_side,
                    price, s.size_usd * 0.5, f"partial_profit_{pnl_per_share:.3f}", 1.0, s.trade_id,
                ))
                s.partial_exited = True
                continue

            if pnl_per_share >= PROFIT_TARGET:
                self._last_trade_result[fixture_id] = "win"
                signals.append(TradeSignal(
                    "exit", fixture_id, s.token_id, exit_side,
                    price, s.size_usd, f"profit_{pnl_per_share:.3f}", 1.0, s.trade_id,
                ))
            elif pnl_per_share <= -STOP_LOSS:
                self._last_trade_result[fixture_id] = "loss"
                signals.append(TradeSignal(
                    "exit", fixture_id, s.token_id, exit_side,
                    price, s.size_usd, f"stop_{pnl_per_share:.3f}", 1.0, s.trade_id,
                ))
            elif elapsed >= timeout:
                self._last_trade_result[fixture_id] = "loss" if pnl_per_share < 0 else "win"
                remaining_size = s.size_usd * (0.5 if s.partial_exited else 1.0)
                signals.append(TradeSignal(
                    "exit", fixture_id, s.token_id, exit_side,
                    price, remaining_size, f"timeout_{elapsed:.0f}s", 0.5, s.trade_id,
                ))

        if self.get_active(fixture_id):
            return signals

        if time.time() - self._last_trade_time.get(fixture_id, 0) < self._get_cooldown(fixture_id):
            return signals

        # Check entries for ALL event types
        for ev in new_events:
            sig = self._evaluate_event(fixture_id, ev, market, state.minute, orderbook_imbalance)
            if sig:
                signals.append(sig)
                break

        return signals

    def _evaluate_event(
        self, fid: int, ev: MatchEvent, m: FootballMarket, match_minute: int, imbalance: float
    ) -> TradeSignal | None:
        
        # Get market direction (Polymarket condition is about home or away team?)
        market_team = "home" if m.home_team.lower() in m.question.lower() else "away"
        
        # Pre-match or baseline price
        pre = m.pre_match_price or 0.5
        cur = m.current_yes_price
        
        # Calculate expected % change in probability
        impact = self._model.compute_event_impact(
            ev.type, ev.team, market_team, "away" if market_team=="home" else "home",
            match_minute, ev.home_score, ev.away_score
        )
        
        if impact == 0:
            return None
            
        target = self._model.compute_reversion_target(cur, impact)
        move = abs(cur - pre)
        rev = abs(cur - target)
        
        # Adjust size based on event type and orderbook imbalance
        size = BASE_SIZE
        confidence = 1.0
        
        if ev.type in ("goal", "own_goal"):
            if move < MIN_MOVE or rev < MIN_REV: return None
        elif ev.type == "var_reversal":
            # VAR is explosive — fast entry, bigger size
            if move < 0.02: return None
            size *= 1.5
            confidence = 1.2
        elif ev.type == "penalty_awarded":
            # Anticipation trade
            if move < 0.02: return None
            size *= 0.8
        elif ev.type == "red_card":
            if move < 0.025: return None
            size *= 0.65
            confidence = 0.8
            
        side = "SELL" if cur > target else "BUY"
        
        # Orderbook intelligence boost
        if side == "BUY" and imbalance > 2.0:
            confidence *= 1.2  # Buy pressure supports our BUY
        elif side == "SELL" and imbalance < 0.5:
            confidence *= 1.2  # Sell pressure supports our SELL
            
        # Late game scaling
        if 75 <= match_minute < 85: size *= 1.25
        elif match_minute >= 85: size *= 0.80

        logger.info(
            "%s SCALP %s: $%.2f @ %.3f → revert %.3f (rev=%.3f conf=%.2f) %d'",
            ev.type.upper(), side, size, cur, target, rev, confidence, ev.minute,
        )

        return TradeSignal("entry", fid, m.yes_token_id, side, cur, min(size, MAX_SIZE),
                           f"{ev.type}_{ev.minute}m", confidence)
