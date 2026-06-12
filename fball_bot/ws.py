"""PriceTrigger — REAL-TIME Polymarket CLOB WebSocket price spike detector.

Uses genuine websockets library for push-based L2 orderbook updates.
Computes orderbook imbalance and rolling volatility for adaptive thresholds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from collections import deque
from typing import Any, Callable

logger = logging.getLogger(__name__)


class PriceSpike:
    """A detected price spike with context."""
    def __init__(
        self,
        token_id: str,
        fixture_id: int,
        price_before: float,
        price_now: float,
        change_pct: float,
        direction: str,
        timestamp: float,
        imbalance: float = 1.0,
    ) -> None:
        self.token_id = token_id
        self.fixture_id = fixture_id
        self.price_before = price_before
        self.price_now = price_now
        self.change_pct = change_pct
        self.direction = direction
        self.timestamp = timestamp
        self.imbalance = imbalance
        self.detected_at = time.time()


class PriceTrigger:
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/l2"
    REST_URL = "https://clob.polymarket.com/books"

    MIN_SPIKE_THRESHOLD_PCT = 2.0  # Absolute minimum 2% move
    SPIKE_WINDOW_SECONDS = 10.0
    ALERT_COOLDOWN = 30.0
    MID_PRICE_WINDOW = 30          # Slightly larger for volatility tracking

    def __init__(self) -> None:
        self._subscriptions: dict[str, dict[str, Any]] = {}
        self._price_history: dict[str, deque] = {}
        self._callbacks: list[Callable] = []
        self._running = False
        self._reconnect_delay = 1.0
        self._last_activity: float = time.time()
        self._use_websocket = True
        self._rest_client: Any = None

    def on_spike(self, callback: Callable) -> None:
        self._callbacks.append(callback)

    async def subscribe(self, token_id: str, fixture_id: int, baseline_price: float) -> None:
        self._subscriptions[token_id] = {
            "fixture_id": fixture_id,
            "baseline_price": baseline_price,
            "last_alert": 0.0,
            "best_bid": baseline_price - 0.005,
            "best_ask": baseline_price + 0.005,
            "bid_vol": 100.0,
            "ask_vol": 100.0,
        }
        self._price_history[token_id] = deque(maxlen=self.MID_PRICE_WINDOW)
        self._price_history[token_id].append((time.time(), baseline_price))
        logger.info("PriceTrigger subscribed to %s", token_id[:16])

    def unsubscribe(self, token_id: str) -> None:
        self._subscriptions.pop(token_id, None)
        self._price_history.pop(token_id, None)

    async def start(self) -> None:
        self._running = True
        import httpx
        self._rest_client = httpx.AsyncClient(timeout=5, http2=False)
        try:
            while self._running:
                try:
                    if self._use_websocket:
                        await self._run_websocket()
                    else:
                        await self._run_rest_polling()
                except Exception as e:
                    delay = self._reconnect_delay
                    logger.warning("PriceTrigger disconnected (%s), retry in %.0fs", type(e).__name__, delay)
                    await asyncio.sleep(delay)
                    self._reconnect_delay = min(self._reconnect_delay * 1.5, 10.0)
        finally:
            if self._rest_client:
                await self._rest_client.aclose()

    async def stop(self) -> None:
        self._running = False

    async def _run_websocket(self) -> None:
        try:
            import websockets
        except ImportError:
            self._use_websocket = False
            await self._run_rest_polling()
            return

        async with websockets.connect(
            self.WS_URL, ping_interval=20, ping_timeout=10, max_size=2**20
        ) as ws:
            for token_id in list(self._subscriptions.keys()):
                await ws.send(json.dumps({"type": "subscribe", "channel": "l2", "id": token_id}))

            self._reconnect_delay = 1.0
            self._last_activity = time.time()
            logger.info("PriceTrigger WebSocket LIVE")

            async for raw_msg in ws:
                if not self._running: break
                self._last_activity = time.time()
                try:
                    self._handle_ws_message(raw_msg)
                except Exception:
                    pass

                # Subscribe new tokens lazily
                for token_id in list(self._subscriptions.keys()):
                    if token_id not in self._subscriptions: continue
                    try:
                        await ws.send(json.dumps({"type": "subscribe", "channel": "l2", "id": token_id}))
                    except Exception:
                        pass

    def _handle_ws_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes): raw = raw.decode()
        data = json.loads(raw)
        if not isinstance(data, list): data = [data]

        for msg in data:
            market = msg.get("market", "") or msg.get("id", "")
            if market not in self._subscriptions: continue

            bids = msg.get("bids", [])
            asks = msg.get("asks", [])
            info = self._subscriptions[market]

            if bids:
                info["best_bid"] = float(bids[0].get("price", bids[0][0] if isinstance(bids[0], list) else 0))
                info["bid_vol"] = sum(float(b.get("size", b[1] if isinstance(b, list) else 0)) for b in bids[:3])
            
            if asks:
                info["best_ask"] = float(asks[0].get("price", asks[0][0] if isinstance(asks[0], list) else 0))
                info["ask_vol"] = sum(float(a.get("size", a[1] if isinstance(a, list) else 0)) for a in asks[:3])

            if info["best_bid"] <= 0 or info["best_ask"] <= 0 or info["best_bid"] >= info["best_ask"]:
                return

            mid = (info["best_bid"] + info["best_ask"]) / 2
            self._price_history[market].append((time.time(), mid))
            self._check_spike(market, mid, info)

    async def _run_rest_polling(self) -> None:
        self._reconnect_delay = 1.0
        while self._running:
            if self._subscriptions:
                tasks = [self._poll_one_token(tid, info) for tid, info in list(self._subscriptions.items())]
                await asyncio.gather(*tasks, return_exceptions=True)
                self._last_activity = time.time()
            await asyncio.sleep(1.0)

    async def _poll_one_token(self, token_id: str, info: dict) -> None:
        try:
            r = await self._rest_client.get(self.REST_URL, params={"token_id": token_id})
            if r.status_code != 200: return
            data = r.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if not bids or not asks: return
            
            info["best_bid"] = float(bids[0].get("price", 0) if isinstance(bids[0], dict) else bids[0][0])
            info["best_ask"] = float(asks[0].get("price", 0) if isinstance(asks[0], dict) else asks[0][0])
            info["bid_vol"] = sum(float(b.get("size", 0) if isinstance(b, dict) else b[1]) for b in bids[:3])
            info["ask_vol"] = sum(float(a.get("size", 0) if isinstance(a, dict) else a[1]) for a in asks[:3])

            mid = (info["best_bid"] + info["best_ask"]) / 2
            self._price_history[token_id].append((time.time(), mid))
            self._check_spike(token_id, mid, info)
        except Exception:
            pass

    def _compute_rolling_volatility(self, history: deque) -> float:
        """Calculate standard deviation of price changes to determine market 'noise'."""
        if len(history) < 5:
            return self.MIN_SPIKE_THRESHOLD_PCT

        changes = []
        for i in range(1, len(history)):
            prev = history[i-1][1]
            curr = history[i][1]
            if prev > 0:
                changes.append(abs(curr - prev) / prev * 100)

        if not changes: return self.MIN_SPIKE_THRESHOLD_PCT
        
        mean = sum(changes) / len(changes)
        variance = sum((c - mean) ** 2 for c in changes) / len(changes)
        std_dev = math.sqrt(variance)
        
        # Adaptive threshold: 1.5x the standard deviation, floored at 2.0%
        return max(self.MIN_SPIKE_THRESHOLD_PCT, std_dev * 1.5)

    def _check_spike(self, token_id: str, current_mid: float, info: dict) -> None:
        history = self._price_history.get(token_id)
        if not history or len(history) < 2: return

        now = time.time()
        first_price = None
        for ts, price in history:
            if now - ts <= self.SPIKE_WINDOW_SECONDS:
                if first_price is None: first_price = price
                break

        if first_price is None or first_price <= 0: return

        change_pct = ((current_mid - first_price) / first_price) * 100
        
        # --- ADAPTIVE THRESHOLD ---
        adaptive_threshold = self._compute_rolling_volatility(history)
        if abs(change_pct) < adaptive_threshold:
            return

        if now - info.get("last_alert", 0) < self.ALERT_COOLDOWN:
            return

        direction = "up" if change_pct > 0 else "down"
        info["last_alert"] = now
        
        # --- ORDERBOOK IMBALANCE ---
        bid_v = info.get("bid_vol", 1.0)
        ask_v = info.get("ask_vol", 1.0)
        imbalance = bid_v / max(ask_v, 1.0)

        spike = PriceSpike(
            token_id=token_id,
            fixture_id=info["fixture_id"],
            price_before=round(first_price, 4),
            price_now=round(current_mid, 4),
            change_pct=round(change_pct, 1),
            direction=direction,
            timestamp=now,
            imbalance=imbalance,
        )

        logger.info(
            "SPIKE: fix=%d %s %.1f%% (th=%.1f%%) imb=%.1f (%.3f->%.3f)",
            info["fixture_id"], direction, change_pct, adaptive_threshold, imbalance,
            first_price, current_mid
        )

        for cb in self._callbacks:
            try:
                if asyncio.iscoroutinefunction(cb): asyncio.create_task(cb(spike))
                else: cb(spike)
            except Exception as e:
                logger.error("Spike cb err: %s", e)

    @property
    def is_connected(self) -> bool:
        return (time.time() - self._last_activity) < 30

    @property
    def connected_markets(self) -> int:
        return len(self._subscriptions)
