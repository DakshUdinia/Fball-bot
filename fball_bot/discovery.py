"""Market discovery — finds World Cup markets on Polymarket via Gamma API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
import hashlib
import time

from .live_data import LiveMatchService, FixtureSummary
from .config import MOCK_MISSING_MARKETS

logger = logging.getLogger(__name__)

CACHE_FILE = "data/market_cache.json"

TEAM_ALIASES: dict[str, str] = {
    "usa": "United States", "united states": "USA",
    "england": "England", "uk": "England",
    "korea republic": "South Korea", "south korea": "South Korea",
    "ivory coast": "Côte d'Ivoire",
    "bosnia and herzegovina": "Bosnia & Herzegovina",
    "bosnia-herzegovina": "Bosnia & Herzegovina",
}

SEARCH_QUERIES = ["World Cup 2026", "World Cup", "FIFA World Cup", "FIFA 2026", "football match"]

# Pre-curated list of 2026 World Cup Group Stage condition IDs on Polymarket.
# This prevents the bot from missing matches due to Gamma search API flakiness.
# Optional fallback if search fails
FALLBACK_MARKET_IDS = [
    # Canada vs Bosnia (World Cup 2026)
    "0x802adc7238db42d431521f625b2a367954d04ebf8836109257dd4a3b961c6108", # Canada
    "0x3bb3f35e86949207b257f1f3288a8ceaf071638cbd10a3c9a8273b4f542d4ee0", # Bosnia
]


@dataclass
class FootballMarket:
    condition_id: str
    yes_token_id: str
    no_token_id: str
    slug: str
    question: str
    home_team: str
    away_team: str
    fixture_id: int = 0
    match_date: str = ""
    pre_match_price: float = 0.0
    current_yes_price: float = 0.0
    current_no_price: float = 0.0
    is_active: bool = True
    liquidity: float = 0.0


def normalize(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"[^a-z0-9\s]", "", n).strip()
    return TEAM_ALIASES.get(n, n)


def extract_teams(question: str) -> tuple[str, str] | None:
    q = question.replace("?", "").replace(".", "").lower()
    
    # Special case for "Will [Team] win on [Date]" (often used for World Cup group matches)
    m = re.search(r"will\s+(.+?)\s+win\s+on\s+", q)
    if m:
        # We only get one team here. _parse_market will handle the matching if one team matches.
        return m.group(1).strip().title(), ""
        
    m = re.search(r"(?:between\s+)?(.+?)\s+(?:vs\.?|beat|defeat|against|v\s)\s+(.+)", q)
    if m:
        return m.group(1).strip().title(), m.group(2).strip().title()
    m = re.search(r"will\s+(.+?)\s+(?:win|beat|defeat)\s+(.+)", q)
    if m:
        return m.group(1).strip().title(), m.group(2).strip().title()
    return None


class FootballMarketDiscovery:
    """Discovers football markets on Polymarket and maps to API-Football fixtures."""

    def __init__(self, gamma_api: Any, live_service: LiveMatchService) -> None:
        self._gamma = gamma_api
        self._live = live_service
        self._markets: dict[int, FootballMarket] = {}
        self._last_refresh: float = 0.0
        self._load_cache()

    def _load_cache(self) -> None:
        """Load previously discovered condition IDs from disk."""
        self._cached_cids: set[str] = set(FALLBACK_MARKET_IDS)
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r") as f:
                    data = json.load(f)
                    for cid in data.get("condition_ids", []):
                        self._cached_cids.add(cid)
            except Exception as e:
                logger.debug("Failed to load market cache: %s", e)

    def _save_cache(self) -> None:
        """Save successfully mapped condition IDs to disk."""
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump({"condition_ids": list(self._cached_cids)}, f)
        except Exception as e:
            logger.debug("Failed to save market cache: %s", e)

    async def discover_all(self, league_id: int = 1) -> list[FootballMarket]:
        """Search Gamma API, check cache, and match to fixtures."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fixtures = await self._live.get_fixtures_by_date(today, league_id)
        live = await self._live.get_live_fixtures(league_id)
        all_fx = {f.fixture_id: f for f in fixtures + live}
        results: list[FootballMarket] = []

        seen: set[str] = set()
        
        # 1. First try loading cached and hardcoded IDs (Fastest & most reliable)
        logger.debug("Checking %d cached market IDs", len(self._cached_cids))
        for cid in list(self._cached_cids):
            if cid in seen: continue
            seen.add(cid)
            try:
                gm = await self._gamma.get_market(cid)
                if gm:
                    fm = self._parse_market(gm, all_fx)
                    if fm:
                        results.append(fm)
                        self._markets[fm.fixture_id] = fm
                        continue
            except Exception:
                pass
            # If it failed to map or fetch, it might be stale, but we keep it in cache

        # 2. Then search Gamma for any new ones
        for query in SEARCH_QUERIES:
            try:
                gamma = await self._gamma.search_markets(query, limit=50)
            except Exception as e:
                logger.debug("Gamma query '%s' failed: %s", query, e)
                continue
                
            for gm in gamma if isinstance(gamma, list) else []:
                cid = gm.get("condition_id") or gm.get("conditionId", "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                fm = self._parse_market(gm, all_fx)
                if fm:
                    results.append(fm)
                    self._markets[fm.fixture_id] = fm
                    self._cached_cids.add(cid)

        # 3. MOCK_MISSING_MARKETS logic for testing
        if MOCK_MISSING_MARKETS:
            for fxid, fx in all_fx.items():
                if fxid not in self._markets:
                    mock_cid = "mock_" + hashlib.md5(str(fxid).encode()).hexdigest()[:10]
                    mock_fm = FootballMarket(
                        condition_id=mock_cid,
                        yes_token_id=f"{mock_cid}_YES",
                        no_token_id=f"{mock_cid}_NO",
                        slug=f"mock-match-{fxid}",
                        question=f"Will {fx.home_team} beat {fx.away_team}?",
                        home_team=fx.home_team,
                        away_team=fx.away_team,
                        fixture_id=fxid,
                        match_date=fx.date,
                        pre_match_price=0.50,
                        current_yes_price=0.50,
                        current_no_price=0.50,
                        is_active=True,
                        liquidity=1000.0,
                    )
                    results.append(mock_fm)
                    self._markets[fxid] = mock_fm
                    logger.debug("Created MOCK market for fixture %d", fxid)

        if results:
            self._save_cache()

        logger.info("Discovered %d football markets", len(results))
        self._last_refresh = time.time()
        return results

    def _parse_market(self, gm: dict, fixtures: dict[int, FixtureSummary]) -> FootballMarket | None:
        q = gm.get("question", "") or gm.get("title", "")
        teams = extract_teams(q)
        if not teams:
            return None

        fid = 0
        mapped_home = teams[0]
        mapped_away = teams[1]
        
        for fxid, fx in fixtures.items():
            hn = normalize(fx.home_team)
            an = normalize(fx.away_team)
            t1 = normalize(teams[0])
            t2 = normalize(teams[1]) if teams[1] else ""
            
            # If both teams are provided, must match both
            if t2:
                if (t1 == hn and t2 == an) or (t1 == an and t2 == hn):
                    fid = fxid
                    mapped_home = fx.home_team
                    mapped_away = fx.away_team
                    break
            # If only one team is provided (e.g. "Will Canada win..."), match if either is that team
            elif t1:
                if t1 == hn or t1 == an:
                    fid = fxid
                    mapped_home = fx.home_team
                    mapped_away = fx.away_team
                    break

        yes_tok = no_tok = ""
        yes_pr = no_pr = 0.5
        
        # Handle format with nested tokens array
        if "tokens" in gm:
            for t in gm["tokens"]:
                out = (t.get("outcome", "") or "").lower()
                p = float(t.get("price", t.get("current_price", "0.5")) or 0.5)
                if out == "yes":
                    yes_tok = t.get("token_id", "")
                    yes_pr = p
                elif out == "no":
                    no_tok = t.get("token_id", "")
                    no_pr = p
        # Handle format with parallel arrays (outcomes, outcomePrices, clobTokenIds)
        elif "outcomes" in gm:
            try:
                raw_outcomes = gm.get("outcomes", "[]")
                raw_prices = gm.get("outcomePrices", "[]")
                raw_token_ids = gm.get("clobTokenIds", "[]")
                if isinstance(raw_outcomes, str): raw_outcomes = json.loads(raw_outcomes)
                if isinstance(raw_prices, str): raw_prices = json.loads(raw_prices)
                if isinstance(raw_token_ids, str): raw_token_ids = json.loads(raw_token_ids)
                
                outcomes = [str(o).lower() for o in raw_outcomes]
                prices = raw_prices
                token_ids = raw_token_ids
                
                for i, out in enumerate(outcomes):
                    p = float(prices[i]) if i < len(prices) else 0.5
                    tid = token_ids[i] if i < len(token_ids) else ""
                    if out == "yes":
                        yes_tok = tid
                        yes_pr = p
                    elif out == "no":
                        no_tok = tid
                        no_pr = p
            except Exception:
                pass

        match_date = ""
        if fid and fid in fixtures:
            match_date = fixtures[fid].date

        return FootballMarket(
            condition_id=gm.get("condition_id") or gm.get("conditionId", ""),
            yes_token_id=yes_tok,
            no_token_id=no_tok,
            slug=gm.get("slug", ""),
            question=q,
            home_team=mapped_home,
            away_team=mapped_away,
            fixture_id=fid,
            match_date=match_date,
            pre_match_price=yes_pr,
            current_yes_price=yes_pr,
            current_no_price=no_pr,
            is_active=not gm.get("closed", False) and not gm.get("resolved", False),
            liquidity=float(gm.get("liquidity", 0) or 0),
        )

    def get_market(self, fixture_id: int) -> FootballMarket | None:
        return self._markets.get(fixture_id)

    async def refresh_prices(self, market_svc: Any) -> None:
        """Refresh all market prices in PARALLEL using asyncio.gather()."""
        markets = list(self._markets.values())
        if not markets:
            return

        async def _fetch_one(m: FootballMarket) -> None:
            if m.condition_id.startswith("mock_"):
                return
            try:
                d = await market_svc.get_market(m.condition_id)
                if not isinstance(d, dict):
                    return
                if "tokens" in d:
                    for t in d["tokens"]:
                        o = (t.get("outcome", "") or "").lower()
                        p = float(t.get("price", t.get("current_price", "0.5")) or 0.5)
                        if o == "yes":
                            m.current_yes_price = p
                        elif o == "no":
                            m.current_no_price = p
                elif "outcomes" in d:
                    try:
                        raw_outcomes = d.get("outcomes", "[]")
                        raw_prices = d.get("outcomePrices", "[]")
                        if isinstance(raw_outcomes, str): raw_outcomes = json.loads(raw_outcomes)
                        if isinstance(raw_prices, str): raw_prices = json.loads(raw_prices)
                        
                        outcomes = [str(o).lower() for o in raw_outcomes]
                        prices = raw_prices
                        for i, o in enumerate(outcomes):
                            p = float(prices[i]) if i < len(prices) else 0.5
                            if o == "yes":
                                m.current_yes_price = p
                            elif o == "no":
                                m.current_no_price = p
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("Price refresh %s: %s", m.condition_id[:12], e)

        await asyncio.gather(*[_fetch_one(m) for m in markets])
        logger.debug("Refreshed prices for %d markets (parallel)", len(markets))
