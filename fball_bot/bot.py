"""FballTradingBot — fully autonomous World Cup Polymarket scalper.

Fixes:
  - Dedicated exit loop every cycle (#1)
  - Crash recovery: restores open positions on startup (#3)
  - Token-aware minimum order (#4)
  - Live orderbook liquidity check (#6)
  - Pre-scout at kickoff (#5)
  - Adaptive polling: 429 backoff (#2)
  - Telegram notifications (#7)
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

from .config import (
    FOOTBALL_API_KEY, FOOTBALL_CHECK_INTERVAL, FOOTBALL_LEAGUE_ID,
    MARKET_REFRESH_INTERVAL, PAPER_TRADING, MAX_CONCURRENT_MATCHES,
    POLL_INTERVAL_SECONDS, INITIAL_CAPITAL, RISK_MAX_PER_TRADE_USD,
    WALLET_PRIVATE_KEY, SCHEDULE_REFRESH_INTERVAL,
)
from .database import (
    init_database, upsert_match, insert_trade, close_trade,
    get_open_trades, get_tracked_matches,
)
from .live_data import LiveMatchService, MatchState
from .schedule import ScheduleService
from .discovery import FootballMarketDiscovery, FootballMarket
from .models import FootballReversionModel
from .scalping import ScalpingStrategy, TradeSignal, ActiveScalp
from .risk import PortfolioRisk
from .gemini import GeminiScout
from .pm import GammaClient, CLOBTrader
from .ws import PriceTrigger, PriceSpike
from .notify import Notifier


class FballTradingBot:
    """Autonomous loop: discover → poll → model → scalp → execute."""

    # Min orderbook depth ($) required to attempt a FOK scalp
    MIN_LIQUIDITY_USD = 20  # Lowered from 50: $20 depth is sufficient for a $3 trade

    def __init__(self) -> None:
        self._live = LiveMatchService()
        self._schedule = ScheduleService()
        self._model = FootballReversionModel()
        self._scalping = ScalpingStrategy(self._model)
        self._risk = PortfolioRisk(
            max_per_trade_usd=RISK_MAX_PER_TRADE_USD,
            initial_capital=INITIAL_CAPITAL,
        )
        self._gemini = GeminiScout()
        self._gamma: GammaClient | None = None
        self._trader: CLOBTrader | None = None
        self._notifier = Notifier()
        self._price_trigger = PriceTrigger()
        self._price_trigger.on_spike(self._on_price_spike)

        self._running = False
        self._cycle_count = 0
        self._last_market_refresh = 0.0
        self._discovery: FootballMarketDiscovery | None = None
        self._tracked_fixtures: set[int] = set()
        self._rate_limit_backoff = 0  # seconds to skip due to 429
        self._last_schedule_refresh = 0.0

    async def _init_polymarket(self) -> None:
        self._gamma = GammaClient()
        if not PAPER_TRADING and WALLET_PRIVATE_KEY:
            try:
                self._trader = CLOBTrader(WALLET_PRIVATE_KEY)
                await self._trader.initialize()
            except Exception as e:
                logger.warning("CLOB init fail: %s", e)
        self._discovery = FootballMarketDiscovery(self._gamma, self._live)

    async def _restore_positions(self) -> None:
        """Fix #3: Recover open positions from DB after crash."""
        open_trades = get_open_trades()
        if not open_trades:
            return
        logger.info("Restoring %d open positions from DB...", len(open_trades))
        for t in open_trades:
            self._scalping.register_entry(ActiveScalp(
                trade_id=t["id"],
                fixture_id=t["fixture_id"],
                token_id=t["token_id"] or "",
                side=t["side"],
                entry_price=t["entry_price"],
                size_usd=t["size_usd"],
                entry_time=time.time(),  # Fresh timestamp = immediate timeout check
                reason=t.get("entry_reason", "restored"),
            ))
            self._tracked_fixtures.add(t["fixture_id"])
            logger.info("  Restored trade %d: fixture=%d %s $%.2f",
                         t["id"], t["fixture_id"], t["side"], t["entry_price"])

    # ═══════════════════════════════════════════════════════════════════

    async def run(self) -> None:
        logger.info("🏁 Fball-bot — $%.2f cap, %ds poll, PriceTrigger active",
                     INITIAL_CAPITAL, FOOTBALL_CHECK_INTERVAL)
        init_database()
        await self._init_polymarket()
        await self._restore_positions()
        await self._refresh_schedule()
        self._notifier.send("🤖 Fball-bot started")

        # Start price trigger in background task
        asyncio.create_task(self._price_trigger.start())

        self._running = True
        while self._running:
            try:
                await self._cycle()
            except Exception as e:
                logger.exception("Cycle: %s", e)
                self._notifier.error(f"Cycle crashed: {e}")
            self._cycle_count += 1
            await asyncio.sleep(FOOTBALL_CHECK_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        await self._live.close()
        await self._schedule.close()
        if self._gamma:
            await self._gamma.close()

    async def _refresh_schedule(self) -> None:
        """Refresh the World Cup calendar from football-data.org (low frequency).

        Keeps the schedule off the API-Football budget, which is reserved for
        the latency-sensitive live event path.
        """
        now = time.time()
        if self._last_schedule_refresh and (now - self._last_schedule_refresh) < SCHEDULE_REFRESH_INTERVAL:
            return
        try:
            matches = await self._schedule.get_schedule()
            self._last_schedule_refresh = now
            today = time.strftime("%Y-%m-%d", time.gmtime())
            todays = self._schedule.matches_on(today)
            logger.info(
                "🗓️ World Cup schedule: %d fixtures (%d today) — API-Football budget %d/%d left",
                len(matches), len(todays),
                self._live.requests_remaining_today, self._live._daily_budget,
            )
        except Exception as e:
            logger.warning("Schedule refresh error: %s", e)

    async def _cycle(self) -> None:
        if not FOOTBALL_API_KEY:
            return

        # Refresh the low-frequency World Cup schedule (football-data.org).
        await self._refresh_schedule()

        # Handle 429 backoff (#2)
        if self._rate_limit_backoff > 0:
            self._rate_limit_backoff -= max(1, FOOTBALL_CHECK_INTERVAL)
            return

        # Refresh markets
        now = time.time()
        if now - self._last_market_refresh > MARKET_REFRESH_INTERVAL and self._discovery:
            await self._discovery.discover_all(FOOTBALL_LEAGUE_ID)
            self._last_market_refresh = now

        # Get live matches
        try:
            live = await self._live.get_live_fixtures(FOOTBALL_LEAGUE_ID)
        except Exception as e:
            logger.debug("Fixtures: %s", e)
            return

        if not live:
            # No live matches — still manage positions
            await self._manage_exits()
            return

        for s in live:
            self._tracked_fixtures.add(s.fixture_id)

        # ── Fix #5: Pre-scout match context at kickoff for all live matches ──
        if self._gemini.available:
            for summary in live:
                # Check if we already cached a scout for this match
                if not self._gemini.get_reason(summary.fixture_id):
                    market = self._discovery.get_market(summary.fixture_id) if self._discovery else None
                    if market:
                        self._gemini.scout({
                            "fixture_id": summary.fixture_id,
                            "home_team": summary.home_team,
                            "away_team": summary.away_team,
                            "minute": summary.minute,
                            "home_score": summary.home_score,
                            "away_score": summary.away_score,
                            "event_type": "kickoff",
                            "event_minute": 0,
                            "event_team": "none",
                            "price_before": market.current_yes_price,
                            "price_now": market.current_yes_price,
                            "side": "HOLD",
                            "size": 3.0,
                            "baseline": market.current_yes_price,
                        })
                        logger.info("🧠 Pre-scout fired for %s vs %s",
                                     summary.home_team, summary.away_team)

        # Risk gate
        ok, reason = self._risk.can_trade(active_match_count=len(live))
        if not ok:
            if self._cycle_count % 10 == 0:
                logger.info("⛔ Risk: %s", reason)
            await self._manage_exits()
            return

        # Process ALL live matches — speed edge applies everywhere
        for summary in live:
            await self._process_match(summary)

        # Dedicated exit management — runs EVERY cycle
        await self._manage_exits()

    # ── PriceTrigger callback: low-latency event path ────────────

    async def _on_price_spike(self, spike: PriceSpike) -> None:
        """Called by PriceTrigger when a rapid price movement is detected.

        This is the LOW-LATENCY PATH. We detect and trade on the price
        movement itself, without waiting for API-Football to confirm.
        API-Football confirmation arrives later in _process_match().
        """
        fid = spike.fixture_id
        logger.info(
            "⚡ PRICE SPIKE: fixture=%d %s %.1f%% (%.3f→%.3f) — fast path",
            fid, spike.direction, spike.change_pct,
            spike.price_before, spike.price_now,
        )

        # Check risk gate
        ok, reason = self._risk.can_trade()
        if not ok:
            logger.debug("Spike skipped (risk): %s", reason)
            return

        # Get market info
        market = self._discovery.get_market(fid) if self._discovery else None
        if not market:
            return

        # Build a synthetic "goal" event from the spike direction
        fake_event = None
        from .live_data import MatchEvent

        # Direction-based: if YES price spiked UP, it's a goal for home team
        if spike.direction == "up":
            fake_event = MatchEvent(
                type="goal",
                minute=0,  # Unknown from price alone
                team="home",
                player=None,
                home_score=0,
                away_score=0,
                event_id=f"spike-{spike.token_id}-{int(spike.timestamp)}",
            )
            trade_side = "SELL"  # Sell the spike, expect reversion
        else:
            fake_event = MatchEvent(
                type="goal",
                minute=0,
                team="away",
                player=None,
                home_score=0,
                away_score=0,
                event_id=f"spike-{spike.token_id}-{int(spike.timestamp)}",
            )
            trade_side = "BUY"  # Buy the dip

        # Run probability model with the spike price as the event
        pre_match = self._model.get_baseline(fid) or market.pre_match_price or spike.price_before
        update = self._model.update(
            current=spike.price_now,
            event=fake_event,
            pre_match=pre_match,
        )

        # Generate entry signal
        if update.scalp_direction == "NONE":
            logger.debug("Spike: no scalp signal from model")
            return

        # Check if reversion is big enough
        expected_rev = abs(spike.price_now - update.reversion_target)
        if expected_rev < 0.02:
            logger.debug("Spike: reversion %.3f too small", expected_rev)
            return

        # Fire Gemini scout in background
        if self._gemini.available:
            self._gemini.scout({
                "fixture_id": fid,
                "home_team": market.home_team,
                "away_team": market.away_team,
                "minute": 0,
                "home_score": 0,
                "away_score": 0,
                "event_type": "price_spike",
                "event_minute": 0,
                "event_team": fake_event.team,
                "price_before": spike.price_before,
                "price_now": spike.price_now,
                "side": trade_side,
                "size": 3.0,
                "baseline": pre_match,
            })

        # Create trade signal
        from .scalping import TradeSignal
        signal = TradeSignal(
            type="entry",
            fixture_id=fid,
            token_id=market.yes_token_id,
            side=trade_side,
            price=spike.price_now,
            size=3.0,
            reason=f"price_spike_{spike.direction}_{spike.change_pct:.0f}pct",
            confidence=update.confidence,
        )

        # Execute immediately — fast path
        await self._execute_signal(fid, signal, spike.price_now)

    # ── Dedicated exit loop ─────────────────────────────────

    async def _manage_exits(self) -> None:
        """Check ALL tracked fixtures for exit conditions, regardless of events."""
        for fid in list(self._tracked_fixtures):
            active = self._scalping.get_active(fid)
            if not active:
                continue
            market = self._discovery.get_market(fid) if self._discovery else None
            current_price = market.current_yes_price if market else 0.0
            state = self._live.get_match_state(fid)

            match_state = state or MatchState(
                fixture_id=fid, home_team="", away_team="",
                status="live", minute=0, home_score=0, away_score=0,
            )

            signals = await self._scalping.evaluate(
                fid, match_state,
                market or FootballMarket("", "", "", "", "", "", "", 0),
                [],
            )
            for sig in signals:
                if sig.type == "exit":
                    await self._execute_signal(fid, sig, current_price)

    # ── Match processing ────────────────────────────────────────────

    async def _process_match(self, summary) -> None:
        fid = summary.fixture_id
        events = await self._live.poll_new_events(fid)

        # Handle 429 only on actual rate-limit response (not empty event lists)
        if self._live.last_status_code == 429:
            self._rate_limit_backoff = FOOTBALL_CHECK_INTERVAL * 3
            logger.warning("Rate limited by API-Football, backing off %ds", self._rate_limit_backoff)

        market = self._discovery.get_market(fid) if self._discovery else None
        if not market and self._discovery:
            await self._discovery.discover_all(FOOTBALL_LEAGUE_ID)
            market = self._discovery.get_market(fid)
        if not market or not market.yes_token_id:
            return

        if self._model.get_baseline(fid) is None:
            self._model.set_baseline(fid, market.current_yes_price)
            logger.info("📡 %s vs %s — YES=%.3f",
                         summary.home_team, summary.away_team, market.current_yes_price)

        # Subscribe to price trigger for real-time spike detection
        if market.yes_token_id not in self._price_trigger._subscriptions:
            await self._price_trigger.subscribe(
                token_id=market.yes_token_id,
                fixture_id=fid,
                baseline_price=market.current_yes_price,
            )

        for ev in events:
            logger.info("⚡ %s! %s %d-%d (%d')", ev.type.upper(), ev.team,
                         ev.home_score, ev.away_score, ev.minute)

        # Refresh price
        if self._gamma:
            try:
                detail = await self._gamma.get_market(market.condition_id)
                if detail:
                    market.current_yes_price = GammaClient.parse_price(detail, "YES")
            except Exception:
                pass

        state = MatchState(
            fixture_id=fid, home_team=summary.home_team, away_team=summary.away_team,
            status=summary.status, minute=summary.minute,
            home_score=summary.home_score, away_score=summary.away_score, events=[],
        )

        signals = await self._scalping.evaluate(fid, state, market, events)
        for signal in signals:
            await self._execute_signal(fid, signal, market.current_yes_price)

        upsert_match({
            "fixture_id": fid, "league": "World Cup",
            "home_team": summary.home_team, "away_team": summary.away_team,
            "match_date": summary.date, "status": summary.status,
            "home_score": summary.home_score, "away_score": summary.away_score,
            "minute": summary.minute, "condition_id": market.condition_id,
            "yes_token_id": market.yes_token_id, "no_token_id": market.no_token_id,
            "pre_match_price": self._model.get_baseline(fid) or 0.0,
            "current_yes_price": market.current_yes_price,
            "current_no_price": market.current_no_price, "is_tracked": 1,
        })

    # ── Signal execution ────────────────────────────────────────────

    async def _execute_signal(self, fid: int, sig: TradeSignal, cur_price: float) -> None:
        if sig.type == "entry":
            # Gemini is ADVISORY ONLY — adjusts size, NEVER fully blocks a trade.
            # Speed is our edge: stale veto or slow AI must not miss a spike.
            scout_mult = self._gemini.get_adjustment(fid)
            scout_reason = self._gemini.get_reason(fid)
            scout_mult = max(0.5, scout_mult)  # Floor at 0.5x, never full veto
            if scout_reason:
                logger.debug("Gemini advisory: mult=%.1f %s", scout_mult, scout_reason)

            # Fix #6: Liquidity check
            if self._gamma and sig.token_id:
                depth = await self._gamma.get_orderbook_depth(sig.token_id)
                if depth < self.MIN_LIQUIDITY_USD:
                    logger.warning("Low liquidity for %s: $%.0f depth < $%d — skipping",
                                    sig.token_id[:12], depth, self.MIN_LIQUIDITY_USD)
                    return

            # Risk validation + token-aware minimum
            ok, reason = self._risk.validate_order(sig.price, sig.size, token_price=sig.price)
            if not ok:
                logger.warning("Invalid: %s", reason)
                return

            max_size = self._risk.compute_max_trade_size(
                sig.size, sig.confidence * scout_mult, token_price=sig.price,
            )
            if max_size <= 0:
                return
            final_size = min(sig.size, max_size)
            qty = final_size / sig.price if sig.price > 0 else final_size

            rid_info = "kelly=%.2f dd=%.1f%% cap=$%.2f" % (
                self._risk._kelly_fraction(),
                self._risk._effective_drawdown() * 100,
                self._risk.s.current_capital,
            )

            match_str = ""
            if fid in self._tracked_fixtures:
                state = self._live.get_match_state(fid)
                if state:
                    match_str = f"{state.home_team} vs {state.away_team}"

            if PAPER_TRADING:
                tid = insert_trade({
                    "fixture_id": fid, "trade_type": sig.side, "side": sig.side,
                    "token_id": sig.token_id, "entry_price": sig.price,
                    "size_usd": final_size, "quantity": qty,
                    "order_id": f"paper-{int(time.time())}", "status": "open",
                    "entry_reason": sig.reason,
                })
                self._scalping.register_entry(ActiveScalp(
                    trade_id=tid, fixture_id=fid, token_id=sig.token_id,
                    side=sig.side, entry_price=sig.price, size_usd=final_size,
                    entry_time=time.time(), reason=sig.reason,
                ))
                logger.info("📗 ENTRY %s $%.2f @ %.3f (%s) — id=%d %s scout=%s",
                             sig.side, final_size, sig.price, sig.reason, tid,
                             rid_info, scout_reason or "none")
                self._notifier.trade_entry(sig.side, final_size, sig.price,
                                            sig.reason, match_str, scout_reason)
            elif self._trader:
                try:
                    result = await self._trader.place_market_order(
                        token_id=sig.token_id, amount=final_size,
                        side=sig.side, price=sig.price,
                    )
                    oid = (result.get("order_ids") or ["unknown"])[0]
                    tid = insert_trade({
                        "fixture_id": fid, "trade_type": sig.side, "side": sig.side,
                        "token_id": sig.token_id, "entry_price": sig.price,
                        "size_usd": final_size, "quantity": qty,
                        "order_id": oid, "status": "open", "entry_reason": sig.reason,
                    })
                    self._scalping.register_entry(ActiveScalp(
                        trade_id=tid, fixture_id=fid, token_id=sig.token_id,
                        side=sig.side, entry_price=sig.price, size_usd=final_size,
                        entry_time=time.time(), reason=sig.reason,
                    ))
                    logger.info("📗 LIVE %s $%.2f @ %.3f — id=%d", sig.side, final_size, sig.price, tid)
                    self._notifier.trade_entry(sig.side, final_size, sig.price,
                                                sig.reason, match_str)
                except Exception as e:
                    logger.error("Trade fail: %s", e)
                    self._notifier.error(f"Entry failed: {e}")

        elif sig.type == "exit" and sig.trade_id:
            # For partial exits, don't remove from active scalps
            is_partial = "partial" in sig.reason
            if is_partial:
                scalp = self._scalping._active.get(sig.trade_id)
            else:
                scalp = self._scalping.register_exit(sig.trade_id)
            if not scalp:
                return
            # Fix: PnL = qty_shares * price_move (not dollar_amount * price_move)
            qty = sig.size / scalp.entry_price if scalp.entry_price > 0 else sig.size
            if scalp.side == "SELL":
                pnl = qty * (scalp.entry_price - sig.price)
            else:
                pnl = qty * (sig.price - scalp.entry_price)
            close_trade(sig.trade_id, sig.price, pnl, sig.reason)
            info = self._risk.record_trade(pnl, scalp.entry_price)

            match_str = ""
            state = self._live.get_match_state(fid)
            if state:
                match_str = f"{state.home_team} vs {state.away_team}"

            emoji = "🟢" if pnl >= 0 else "🔴"
            logger.info("%s EXIT id=%d $%.4f (%.1f%% WR, cap=$%.2f) — %s",
                         emoji, sig.trade_id, pnl, info["win_rate"], info["capital"], sig.reason)
            self._notifier.trade_exit(
                scalp.side, pnl, sig.reason, match_str, info["capital"],
            )
