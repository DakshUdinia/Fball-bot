"""Polymarket — standalone Gamma API client + CLOB order execution.

Self-contained module. Zero dependency on MoneyLine.
Uses httpx for Gamma API and py-clob-client for CLOB orders.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"

# ═══════════════════════════════════════════════════════════════════
# Gamma API — market search, prices, metadata
# ═══════════════════════════════════════════════════════════════════


class GammaClient:
    """Lightweight Polymarket Gamma API client — market search & prices.

    Pure HTTP, no MoneyLine dependencies. 10 req/s rate limit (generous).
    """

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=15)
        self._base = GAMMA_BASE

    async def close(self) -> None:
        await self._http.aclose()

    async def search_markets(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Search markets by keyword in title."""
        try:
            r = await self._http.get(
                f"{self._base}/markets",
                params={"title": query, "limit": limit, "closed": "false"},
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.debug("Gamma search '%s' failed: %s", query, e)
            return []

    async def get_market(self, condition_id: str) -> dict[str, Any] | None:
        """Get a single market by condition ID."""
        try:
            r = await self._http.get(
                f"{self._base}/markets",
                params={"condition_ids": condition_id, "limit": "1"},
            )
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list) and data:
                return data[0]
            return None
        except Exception as e:
            logger.debug("Gamma market %s failed: %s", condition_id, e)
            return None

    async def get_markets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Get multiple markets by IDs."""
        results = []
        for cid in ids:
            m = await self.get_market(cid)
            if m:
                results.append(m)
        return results

    @staticmethod
    def parse_price(market: dict[str, Any], outcome: str = "YES") -> float:
        """Extract current price for YES or NO from market data."""
        if "tokens" in market:
            for t in market["tokens"]:
                o = (t.get("outcome", "") or "").lower()
                p = t.get("price", t.get("current_price", "0.5"))
                if isinstance(p, str):
                    try:
                        p = float(p)
                    except (ValueError, TypeError):
                        p = 0.5
                if o == outcome.lower():
                    return float(p)
        elif "outcomes" in market:
            try:
                raw_outcomes = market.get("outcomes", "[]")
                raw_prices = market.get("outcomePrices", "[]")
                if isinstance(raw_outcomes, str): raw_outcomes = json.loads(raw_outcomes)
                if isinstance(raw_prices, str): raw_prices = json.loads(raw_prices)
                
                outcomes = [str(o).lower() for o in raw_outcomes]
                prices = raw_prices
                for i, o in enumerate(outcomes):
                    if o == outcome.lower():
                        p = prices[i] if i < len(prices) else 0.5
                        try:
                            return float(p)
                        except (ValueError, TypeError):
                            return 0.5
            except Exception:
                pass
        return 0.5

    async def get_orderbook_depth(self, token_id: str) -> float:
        """Get total orderbook depth (bid + ask) for a token.

        Returns total USD depth, or 0 if unavailable.
        Used by ScalpingStrategy to skip illiquid markets.
        """
        try:
            r = await self._http.get(
                "https://clob.polymarket.com/books",
                params={"token_id": token_id},
            )
            r.raise_for_status()
            data = r.json()
            bids = sum(float(b.get("sz", 0)) * float(b.get("px", 0))
                       for b in data.get("bids", []))
            asks = sum(float(b.get("sz", 0)) * float(b.get("px", 0))
                       for b in data.get("asks", []))
            return bids + asks
        except Exception as e:
            logger.debug("Orderbook depth %s: %s", token_id, e)
            return 0.0

    @staticmethod
    def extract_tokens(market: dict[str, Any]) -> tuple[str, str]:
        """Extract (yes_token_id, no_token_id) from market data."""
        yes_tok = no_tok = ""
        if "tokens" in market:
            for t in market["tokens"]:
                o = (t.get("outcome", "") or "").lower()
                tid = t.get("token_id", "")
                if o == "yes":
                    yes_tok = tid
                elif o == "no":
                    no_tok = tid
        elif "outcomes" in market:
            try:
                raw_outcomes = market.get("outcomes", "[]")
                raw_token_ids = market.get("clobTokenIds", "[]")
                if isinstance(raw_outcomes, str): raw_outcomes = json.loads(raw_outcomes)
                if isinstance(raw_token_ids, str): raw_token_ids = json.loads(raw_token_ids)
                
                outcomes = [str(o).lower() for o in raw_outcomes]
                token_ids = raw_token_ids
                for i, o in enumerate(outcomes):
                    tid = token_ids[i] if i < len(token_ids) else ""
                    if o == "yes":
                        yes_tok = tid
                    elif o == "no":
                        no_tok = tid
            except Exception:
                pass
        return yes_tok, no_tok


# ═══════════════════════════════════════════════════════════════════
# CLOB Trading — FOK market orders via py-clob-client
# ═══════════════════════════════════════════════════════════════════


class CLOBTrader:
    """Polymarket CLOB trader — places FOK/FAK market orders.

    Thin wrapper around py-clob-client.
    Requires WALLET_PRIVATE_KEY to be set in environment.
    """

    def __init__(self, private_key: str) -> None:
        self._key = private_key
        self._clob = None
        self._ready = False

    async def initialize(self) -> None:
        """Create and configure the CLOB client with the wallet."""
        if self._ready:
            return
        try:
            from py_clob_client.client import PolyClob
            from py_clob_client.clob_types import MarketOrderArgs, OrderType

            self._clob = PolyClob(private_key=self._key, chain_id=137)
            # Derive address from key
            self._address = self._clob.get_address()
            self._order_type = OrderType

            # Set API credentials
            try:
                api_creds = self._clob.create_api_key()
                self._clob.set_api_credentials(api_creds)
            except Exception as e:
                logger.debug("API key creation (may already exist): %s", e)
                # If already exists, try a simpler init
                self._clob = PolyClob(private_key=self._key, chain_id=137)

            self._ready = True
            logger.info("CLOB ready for %s...%s", self._address[:6], self._address[-4:])
        except ImportError as e:
            logger.warning(
                "py-clob-client not installed. Run: pip install py-clob-client"
            )
            raise
        except Exception as e:
            logger.error("CLOB init failed: %s", e)
            raise

    async def place_market_order(
        self,
        token_id: str,
        amount: float,
        side: str,
        price: float = 0.50,
    ) -> dict[str, Any]:
        """Place a FOK market order on the CLOB.

        Args:
            token_id: The token ID to trade
            amount: USD amount to spend/receive
            side: "BUY" or "SELL"
            price: Limit/reference price

        Returns:
            Dict with order_ids, success status
        """
        if not self._ready:
            await self.initialize()
        if not self._clob:
            return {"success": False, "error": "CLOB not initialized"}

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType

            args = MarketOrderArgs(
                token_id=token_id,
                side=side.upper(),
                amount=str(amount),
                price=str(price),
                order_type=OrderType.FOK,
            )
            signed = self._clob.create_market_order(args)
            resp = self._clob.post_order(signed, OrderType.FOK)
            return {
                "success": True,
                "order_ids": resp.get("order_ids", [resp.get("id", "")]),
            }
        except Exception as e:
            logger.error("CLOB order failed: %s", e)
            return {"success": False, "error": str(e)}

    @property
    def address(self) -> str:
        return getattr(self, "_address", "unknown")

    @property
    def ready(self) -> bool:
        return self._ready
