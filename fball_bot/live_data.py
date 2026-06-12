"""Live match data — polls API-Football for real-time football match events.

Bug fixes:
  - Team side detection now correctly compares numeric team IDs (was comparing
    int ID to string "home" — always returned "away")

New signals:
  - Yellow-red card (second yellow = effective red)
  - VAR decisions: goal cancelled, penalty confirmed/cancelled
  - Own goals (team flipped so model direction is correct)
  - Penalty goals flagged separately for better model accuracy
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class MatchEvent:
    type: str          # goal, own_goal, red_card, yellow_red, var_reversal,
                       # penalty_awarded, penalty_missed, substitution
    minute: int
    team: str          # home or away (from HOME TEAM'S perspective)
    player: str | None = None
    home_score: int = 0
    away_score: int = 0
    event_id: str = ""
    detail: str = ""
    is_penalty: bool = False   # True if goal was a penalty kick


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
    home_team_id: int = 0
    away_team_id: int = 0


class LiveMatchService:
    """Polls API-Football v3 for live match data."""

    BASE = "https://v3.football.api-sports.io"

    def __init__(self) -> None:
        from .config import FOOTBALL_API_KEY
        self._api_key = FOOTBALL_API_KEY
        self._headers = {
            "x-apisports-key": self._api_key,
        }
        self._known_events: dict[int, set[str]] = {}
        # BUG FIX: Store (home_team_id, away_team_id) per fixture for correct side detection
        self._fixture_teams: dict[int, tuple[int, int]] = {}
        self._match_states: dict[int, MatchState] = {}
        self._client = httpx.AsyncClient(timeout=15)
        self.last_status_code: int = 200

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> dict | None:
        if not self._api_key:
            logger.warning("FOOTBALL_API_KEY not set")
            return None
        try:
            r = await self._client.get(f"{self.BASE}{path}", headers=self._headers, params=params)
            self.last_status_code = r.status_code
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

            fid = f.get("id", 0)
            home_id = t.get("home", {}).get("id", 0)
            away_id = t.get("away", {}).get("id", 0)

            # BUG FIX: Store actual numeric team IDs for side detection
            self._fixture_teams[fid] = (home_id, away_id)

            summary = FixtureSummary(
                fixture_id=fid,
                home_team=t.get("home", {}).get("name", ""),
                away_team=t.get("away", {}).get("name", ""),
                status=s.get("short", ""),
                minute=int(s.get("elapsed", 0) or 0),
                home_score=int(g.get("home", 0) or 0),
                away_score=int(g.get("away", 0) or 0),
                date=f.get("date", ""),
                home_team_id=home_id,
                away_team_id=away_id,
            )
            fixtures.append(summary)
            if fid not in self._known_events:
                self._known_events[fid] = set()

            # Cache match state
            self._match_states[fid] = MatchState(
                fixture_id=fid,
                home_team=summary.home_team,
                away_team=summary.away_team,
                status=summary.status,
                minute=summary.minute,
                home_score=summary.home_score,
                away_score=summary.away_score,
            )
        return fixtures

    def get_match_state(self, fixture_id: int) -> MatchState | None:
        return self._match_states.get(fixture_id)

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
            fid = f.get("id", 0)
            home_id = t.get("home", {}).get("id", 0)
            away_id = t.get("away", {}).get("id", 0)
            self._fixture_teams[fid] = (home_id, away_id)
            fixtures.append(FixtureSummary(
                fixture_id=fid,
                home_team=t.get("home", {}).get("name", ""),
                away_team=t.get("away", {}).get("name", ""),
                status=s.get("short", ""),
                minute=int(s.get("elapsed", 0) or 0),
                home_score=int(g.get("home", 0) or 0),
                away_score=int(g.get("away", 0) or 0),
                date=f.get("date", ""),
                home_team_id=home_id,
                away_team_id=away_id,
            ))
        return fixtures

    def _get_team_side(self, fixture_id: int, team_id: int) -> str:
        """Correctly identify if a team is home or away using numeric IDs."""
        home_id, away_id = self._fixture_teams.get(fixture_id, (0, 0))
        if home_id and team_id == home_id:
            return "home"
        if away_id and team_id == away_id:
            return "away"
        # Fallback: unknown (shouldn't happen if fixture was fetched first)
        logger.debug("Unknown team side for fixture %d team %d (home=%d, away=%d)",
                     fixture_id, team_id, home_id, away_id)
        return "home"  # Default to home rather than always away (old bug)

    async def get_match_events(self, fixture_id: int) -> list[MatchEvent]:
        """Fetch and parse all events for a fixture with correct team sides."""
        data = await self._get("/fixtures/events", {"fixture": fixture_id})
        events = []
        for item in (data or {}).get("response", []):
            team = item.get("team", {}) or {}
            player = item.get("player", {}) or {}
            etype_raw = (item.get("type", "") or "").lower()
            detail = (item.get("detail", "") or "").lower()
            minute = int((item.get("time", {}) or {}).get("elapsed", 0) or 0)
            team_id = team.get("id", 0)

            # BUG FIX: Use numeric team IDs for side detection (not string comparison)
            team_side = self._get_team_side(fixture_id, team_id)

            etype = None
            is_penalty = False

            if etype_raw == "goal":
                if "own goal" in detail:
                    # Own goal: the listed team scored AGAINST themselves
                    # Flip the side so the model gets the correct direction
                    team_side = "away" if team_side == "home" else "home"
                    etype = "own_goal"
                elif "penalty" in detail:
                    etype = "goal"
                    is_penalty = True
                else:
                    etype = "goal"

            elif etype_raw == "card":
                if "red card" in detail:
                    etype = "red_card"
                elif "yellow red" in detail or "yellow_red" in detail:
                    # Second yellow = effective red card
                    etype = "red_card"
                    logger.debug("Second yellow (yellow-red) treated as red card")
                # Regular yellow cards: skip unless tracking accumulation
                else:
                    continue

            elif etype_raw == "var":
                if "goal cancelled" in detail or "goal disallowed" in detail:
                    etype = "var_reversal"   # Big reversal opportunity
                elif "penalty confirmed" in detail:
                    etype = "penalty_awarded"
                elif "penalty cancelled" in detail:
                    etype = "var_reversal"   # Penalty taken away
                else:
                    continue  # "Goal ok" / other VAR confirmations — no trade

            elif etype_raw == "subst":
                etype = "substitution"
            else:
                continue

            if etype:
                events.append(MatchEvent(
                    type=etype,
                    minute=minute,
                    team=team_side,
                    player=player.get("name"),
                    home_score=int(item.get("home_score", 0) or 0),
                    away_score=int(item.get("away_score", 0) or 0),
                    event_id=f"{fixture_id}-{minute}-{etype}-{team_id}",
                    detail=detail,
                    is_penalty=is_penalty,
                ))
        return events

    async def poll_new_events(self, fixture_id: int) -> list[MatchEvent]:
        """Return only NEW events since the last poll."""
        events = await self.get_match_events(fixture_id)
        known = self._known_events.setdefault(fixture_id, set())
        new = [e for e in events if e.event_id and e.event_id not in known]
        for e in new:
            known.add(e.event_id)
        if new:
            logger.debug("Fixture %d: %d new events", fixture_id, len(new))
        return new
