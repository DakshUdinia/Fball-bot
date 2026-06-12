"""Market discovery — finds World Cup markets on Polymarket via Gamma API."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .live_data import LiveMatchService, FixtureSummary

logger = logging.getLogger(__name__)

TEAM_ALIASES: dict[str, str] = {
    "usa": "United States", "united states": "USA",
    "england": "England", "uk": "England",
    "korea republic": "South Korea", "south korea": "South Korea",
    "ivory coast": "Côte d'Ivoire",
}

SEARCH_QUERIES = ["World Cup 2026", "World Cup", "FIFA World Cup", "FIFA 2026", "football match"]

# Fallback market condition IDs for known World Cup 2026 matches on Polymarket.
# Used when Gamma API search returns empty (API issue, rate limit, etc).
# These are the canonical "will [Team A] beat [Team B]" binary markets.
# Format: (condition_id, team_a, team_b)
FALLBACK_MARKET_IDS: list[tuple[str, str, str]] = [
    # Group stage matches — update these as the tournament progresses
    # ("0x...", "Brazil", "Serbia"),
    # ("0x...", "Portugal", "Ghana"),
    # ("0x...", "Argentina", "Mexico"),
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

    async def discover_all(self, league_id: int = 1) -> list[FootballMarket]:
        """Search Gamma API and match to fixtures. Falls back to hardcoded IDs."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        fixtures = await self._live.get_fixtures_by_date(today, league_id)
        live = await self._live.get_live_fixtures(league_id)
        all_fx = {f.fixture_id: f for f in fixtures + live}
        results: list[FootballMarket] = []

        seen: set[str] = set()
        for query in SEARCH_QUERIES:
            try:
                gamma = await self._gamma.search_markets(query, limit=50)
            except Exception as e:
                logger.debug("Gamma query '%s' failed: %s", query, e)
                continue
            for gm in gamma if isinstance(gamma, list) else []:
                cid = gm.get("condition_id", "")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                fm = self._parse_market(gm, all_fx)
                if fm:
                    results.append(fm)
                    self._markets[fm.fixture_id] = fm

        # Fallback: if Gamma returned nothing useful, try hardcoded IDs
        if not results and FALLBACK_MARKET_IDS:
            logger.info("Gamma search empty — trying %d fallback market IDs", len(FALLBACK_MARKET_IDS))
            for cid, team_a, team_b in FALLBACK_MARKET_IDS:
                if cid in seen:
                    continue
                seen.add(cid)
                try:
                    gm = await self._gamma.get_market(cid)
                except Exception:
                    continue
                if not gm:
                    continue
                fm = self._parse_market(gm, all_fx)
                if fm:
                    results.append(fm)
                    self._markets[fm.fixture_id] = fm

        logger.info("Discovered %d football markets", len(results))
        self._last_refresh = __import__("time").time()
        return results

    def _parse_market(self, gm: dict, fixtures: dict[int, FixtureSummary]) -> FootballMarket | None:
        q = gm.get("question", "") or gm.get("title", "")
        teams = extract_teams(q)
        if not teams:
            return None

        fid = 0
        for fxid, fx in fixtures.items():
            hn = normalize(fx.home_team)
            an = normalize(fx.away_team)
            t1, t2 = normalize(teams[0]), normalize(teams[1])
            if (t1 == hn and t2 == an) or (t1 == an and t2 == hn):
                fid = fxid
                break

        tokens = gm.get("tokens", []) or gm.get("outcomes", [])
        yes_tok = no_tok = ""
        yes_pr = no_pr = 0.5
        for t in tokens:
            out = (t.get("outcome", "") or "").lower()
            p = float(t.get("price", t.get("current_price", "0.5")) or 0.5)
            if out == "yes":
                yes_tok = t.get("token_id", "")
                yes_pr = p
            elif out == "no":
                no_tok = t.get("token_id", "")
                no_pr = p

        match_date = ""
        if fid and fid in fixtures:
            match_date = fixtures[fid].date

        return FootballMarket(
            condition_id=gm.get("condition_id", ""),
            yes_token_id=yes_tok,
            no_token_id=no_tok,
            slug=gm.get("slug", ""),
            question=q,
            home_team=teams[0],
            away_team=teams[1],
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
        for m in self._markets.values():
            try:
                d = await market_svc.get_market(m.condition_id)
                if isinstance(d, dict):
                    tokens = d.get("tokens", []) or d.get("outcomes", [])
                    for t in tokens:
                        o = (t.get("outcome", "") or "").lower()
                        p = float(t.get("price", t.get("current_price", "0.5")) or 0.5)
                        if o == "yes":
                            m.current_yes_price = p
                        elif o == "no":
                            m.current_no_price = p
            except Exception as e:
                logger.debug("Price refresh %s: %s", m.condition_id, e)
