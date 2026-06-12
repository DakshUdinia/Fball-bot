"""Live match data — polls API-Football for real-time football match events."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class MatchEvent:
    type: str          # goal, red_card, yellow_card, substitution
    minute: int
    team: str          # home or away
    player: str | None = None
    home_score: int = 0
    away_score: int = 0
    event_id: str = ""
    detail: str = ""


@dataclass
class MatchState:
    fixture_id: int
    home_team: str
    away_team: str
    status: str        # scheduled, live, halftime, finished
    minute: int
    home_score: int
    away_score: int
    events: list[MatchEvent] = field(default_factory=list)


@dataclass
class FixtureSummary:
    fixture_id: int
    home_team: str
    away_team: str
    status: str
    minute: int
    home_score: int
    away_score: int
    date: str


class LiveMatchService:
    """Polls API-Football v3 for live match data."""

    BASE = "https://api-football-v1.p.rapidapi.com/v3"

    def __init__(self) -> None:
        from .config import FOOTBALL_API_KEY
        self._api_key = FOOTBALL_API_KEY
        self._headers = {
            "X-RapidAPI-Key": self._api_key,
            "X-RapidAPI-Host": "api-football-v1.p.rapidapi.com",
        }
        self._known_events: dict[int, set[str]] = {}
        self._client = httpx.AsyncClient(timeout=15)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict | None:
        if not self._api_key:
            logger.warning("FOOTBALL_API_KEY not set")
            return None
        try:
            r = await self._client.get(f"{self.BASE}{path}", headers=self._headers, params=params)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            logger.debug("API-Football error %s: %s", path, e)
            return None

    async def get_live_fixtures(self, league_id: int | None = None) -> list[FixtureSummary]:
        """Get all currently live fixtures."""
        data = await self._get("/fixtures", {"live": "all"})
        fixtures = []
        for item in (data or {}).get("response", []):
            f = item.get("fixture", {})
            l = item.get("league", {})
            t = item.get("teams", {})
            g = item.get("goals", {})
            s = f.get("status", {})
            lid = l.get("id", 0)
            if league_id and lid != league_id:
                continue
            fixtures.append(FixtureSummary(
                fixture_id=f.get("id", 0),
                home_team=t.get("home", {}).get("name", ""),
                away_team=t.get("away", {}).get("name", ""),
                status=s.get("short", ""),
                minute=int(s.get("elapsed", 0) or 0),
                home_score=int(g.get("home", 0) or 0),
                away_score=int(g.get("away", 0) or 0),
                date=f.get("date", ""),
            ))
            fid = f.get("id", 0)
            if fid not in self._known_events:
                self._known_events[fid] = set()
        return fixtures

    async def get_fixtures_by_date(self, date: str, league_id: int | None = None) -> list[FixtureSummary]:
        params: dict[str, Any] = {"date": date}
        if league_id:
            params["league"] = league_id
            params["season"] = 2026
        data = await self._get("/fixtures", params)
        fixtures = []
        for item in (data or {}).get("response", []):
            f = item.get("fixture", {})
            t = item.get("teams", {})
            g = item.get("goals", {})
            s = f.get("status", {})
            fixtures.append(FixtureSummary(
                fixture_id=f.get("id", 0),
                home_team=t.get("home", {}).get("name", ""),
                away_team=t.get("away", {}).get("name", ""),
                status=s.get("short", ""),
                minute=int(s.get("elapsed", 0) or 0),
                home_score=int(g.get("home", 0) or 0),
                away_score=int(g.get("away", 0) or 0),
                date=f.get("date", ""),
            ))
        return fixtures

    async def get_match_events(self, fixture_id: int) -> list[MatchEvent]:
        """Fetch all events for a fixture."""
        data = await self._get("/fixtures/events", {"fixture": fixture_id})
        events = []
        for item in (data or {}).get("response", []):
            team = item.get("team", {})
            player = item.get("player", {}) or {}
            etype = item.get("type", "").lower()
            detail = item.get("detail", "")
            minute = int(item.get("time", {}).get("elapsed", 0) or 0)

            if etype == "card":
                if detail.lower() == "red card":
                    etype = "red_card"
                else:
                    continue
            if etype == "subst":
                etype = "substitution"

            events.append(MatchEvent(
                type=etype,
                minute=minute,
                team="home" if team.get("id") == "home" else "away",
                player=player.get("name"),
                home_score=int(item.get("home_score", 0) or 0),
                away_score=int(item.get("away_score", 0) or 0),
                event_id=f"{fixture_id}-{minute}-{etype}",
                detail=detail,
            ))
        return events

    async def poll_new_events(self, fixture_id: int) -> list[MatchEvent]:
        """Return only NEW events since the last poll."""
        events = await self.get_match_events(fixture_id)
        known = self._known_events.setdefault(fixture_id, set())
        new = [e for e in events if e.event_id and e.event_id not in known]
        for e in new:
            known.add(e.event_id)
        return new
