"""Scalping strategy — entry/exit rules for goal spike & red card scalping."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from .live_data import MatchEvent, MatchState
from .models import FootballProbabilityModel, ProbabilityUpdate
from .discovery import FootballMarket

logger = logging.getLogger(__name__)

MIN_MOVE = 0.04
MIN_REV = 0.02
BASE_SIZE = 2.0
MAX_SIZE = 3.0
PROFIT_TARGET = 0.03
STOP_LOSS = 0.03
TIMEOUT = 180
COOLDOWN = 60


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
    """Evaluates matches for scalping opportunities and manages exits."""

    def __init__(self, model: FootballProbabilityModel) -> None:
        self._model = model
        self._active: dict[int, ActiveScalp] = {}
        self._last_trade_time: dict[int, float] = {}

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

    async def evaluate(
        self,
        fixture_id: int,
        state: MatchState | None,
        market: FootballMarket | None,
        new_events: list[MatchEvent],
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

        # Check exits
        for s in self.get_active(fixture_id):
            price = market.current_yes_price
            if s.side == "SELL":
                pnl = s.entry_price - price
            else:
                pnl = price - s.entry_price

            elapsed = time.time() - s.entry_time

            if pnl >= PROFIT_TARGET:
                signals.append(TradeSignal("exit", fixture_id, s.token_id, "BUY" if s.side == "SELL" else "SELL",
                                           price, s.size, f"profit_{pnl:.3f}", 1.0, s.trade_id))
            elif pnl <= -STOP_LOSS:
                signals.append(TradeSignal("exit", fixture_id, s.token_id, "BUY" if s.side == "SELL" else "SELL",
                                           price, s.size, f"stop_{pnl:.3f}", 1.0, s.trade_id))
            elif elapsed >= TIMEOUT:
                signals.append(TradeSignal("exit", fixture_id, s.token_id, "BUY" if s.side == "SELL" else "SELL",
                                           price, s.size * 0.6, f"timeout_{elapsed:.0f}s", 0.5, s.trade_id))

        # Don't enter if already have a scalp on this fixture
        if self.get_active(fixture_id):
            return signals

        last_trade = self._last_trade_time.get(fixture_id, 0)
        if time.time() - last_trade < COOLDOWN:
            return signals

        # Check new events for entry
        for ev in new_events:
            if ev.type == "goal":
                sig = self._goal_scalp(fixture_id, ev, market)
                if sig:
                    signals.append(sig)
                    break
            elif ev.type == "red_card":
                sig = self._red_card_scalp(fixture_id, ev, market)
                if sig:
                    signals.append(sig)
                    break

        return signals

    def _goal_scalp(self, fid: int, ev: MatchEvent, m: FootballMarket) -> TradeSignal | None:
        pre = self._model.get_baseline(fid) or m.pre_match_price or m.current_yes_price
        cur = m.current_yes_price
        update = self._model.update(cur, ev, pre)

        move = abs(cur - pre)
        if move < MIN_MOVE or update.delta == 0:
            return None

        rev = abs(cur - update.reversion_target)
        if rev < MIN_REV:
            return None

        if cur > update.reversion_target:
            side = "SELL"
        elif cur < update.reversion_target:
            side = "BUY"
        else:
            return None

        size = BASE_SIZE
        if 75 <= ev.minute < 85:
            size *= 1.2
        if ev.minute >= 85:
            size *= 0.5

        logger.info("GOAL SCALP %s: $%.2f %s @ %.3f (model: %.3f revert: %.3f) — %d'",
                     side, size, m.yes_token_id[:10], cur, update.new_prob, update.reversion_target, ev.minute)

        return TradeSignal("entry", fid, m.yes_token_id, side, cur, min(size, MAX_SIZE),
                           f"goal_{ev.minute}m", update.confidence)

    def _red_card_scalp(self, fid: int, ev: MatchEvent, m: FootballMarket) -> TradeSignal | None:
        pre = self._model.get_baseline(fid) or m.pre_match_price or m.current_yes_price
        cur = m.current_yes_price
        update = self._model.update(cur, ev, pre)

        move = abs(cur - pre)
        if move < 0.03:
            return None
        rev = abs(cur - update.reversion_target)
        if rev < MIN_REV:
            return None

        side = "SELL" if cur > update.reversion_target else "BUY"
        size = BASE_SIZE * 0.6

        return TradeSignal("entry", fid, m.yes_token_id, side, cur, min(size, MAX_SIZE),
                           f"red_{ev.minute}m", update.confidence * 0.8)
