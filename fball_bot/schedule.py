"""World Cup schedule — fetches the fixture calendar from football-data.org.

This is the LOW-FREQUENCY, low-value data path. The World Cup calendar barely
changes, so it is fetched at most a couple of times a day and cached to disk.
Keeping the schedule here frees the entire API-Football free budget for the
latency-sensitive live-event path (see live_data.py).

No simulated data: if the schedule cannot be fetched and no cache exists, an
empty list is returned and the caller falls back to API-Football's live feed.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CACHE_FILE = "data/schedule_cache.json"


@dataclass
class ScheduledMatch:
    """A World Cup fixture from the calendar (not live state)."""
    home_team: str
    away_team: str
    utc_kickoff: str   # ISO 8601, e.g. 2026-06-13T19:00:00Z
    status: str        # SCHEDULED, LIVE, IN_PLAY, PAUSED, FINISHED
    matchday: int = 0
    source_id: int = 0  # football-data.org match id (NOT the API-Football id)


class ScheduleService:
    """Fetches and caches the World Cup schedule from football-data.org."""

    BASE = "https://api.football-data.org/v4"

    def __init__(self) -> None:
        from .config import (
            FOOTBALL_DATA_API_KEY,
            FOOTBALL_DATA_COMPETITION,
            SCHEDULE_REFRESH_INTERVAL,
        )
        self._api_key = FOOTBALL_DATA_API_KEY
        self._competition = FOOTBALL_DATA_COMPETITION
        self._refresh_interval = SCHEDULE_REFRESH_INTERVAL
        self._client = httpx.AsyncClient(timeout=15)
        self._matches: list[ScheduledMatch] = []
        self._last_refresh: float = 0.0
        self._load_cache()

    async def close(self) -> None:
        await self._client.aclose()

    def _load_cache(self) -> None:
        if not os.path.exists(CACHE_FILE):
            return
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
            self._matches = [ScheduledMatch(**m) for m in data.get("matches", [])]
            self._last_refresh = float(data.get("fetched_at", 0.0))
            logger.info("Loaded %d World Cup fixtures from schedule cache", len(self._matches))
        except Exception as e:
            logger.debug("Failed to load schedule cache: %s", e)

    def _save_cache(self) -> None:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        try:
            with open(CACHE_FILE, "w") as f:
                json.dump(
                    {
                        "fetched_at": self._last_refresh,
                        "matches": [m.__dict__ for m in self._matches],
                    },
                    f,
                )
        except Exception as e:
            logger.debug("Failed to save schedule cache: %s", e)

    async def get_schedule(self, force: bool = False) -> list[ScheduledMatch]:
        """Return the WC schedule, refreshing from the API only when stale."""
        now = time.time()
        fresh = (now - self._last_refresh) < self._refresh_interval
        if self._matches and fresh and not force:
            return self._matches
        if not self._api_key:
            logger.warning("FOOTBALL_DATA_API_KEY not set — using cached schedule only")
            return self._matches
        await self._refresh()
        return self._matches

    async def _refresh(self) -> None:
        url = f"{self.BASE}/competitions/{self._competition}/matches"
        headers = {"X-Auth-Token": self._api_key}
        try:
            r = await self._client.get(url, headers=headers)
            r.raise_for_status()
            payload: dict[str, Any] = r.json()
        except Exception as e:
            # Keep whatever cache we already have rather than going blind.
            logger.warning("Schedule refresh failed (%s) — keeping cached schedule", e)
            return

        matches: list[ScheduledMatch] = []
        for item in payload.get("matches", []):
            home = (item.get("homeTeam", {}) or {}).get("name", "") or ""
            away = (item.get("awayTeam", {}) or {}).get("name", "") or ""
            if not home or not away:
                continue  # TBD fixtures (e.g. knockout slots not yet decided)
            matches.append(ScheduledMatch(
                home_team=home,
                away_team=away,
                utc_kickoff=item.get("utcDate", "") or "",
                status=item.get("status", "") or "",
                matchday=int(item.get("matchday", 0) or 0),
                source_id=int(item.get("id", 0) or 0),
            ))

        if matches:
            self._matches = matches
            self._last_refresh = time.time()
            self._save_cache()
            logger.info("Refreshed World Cup schedule: %d fixtures", len(matches))

    def matches_on(self, date_yyyy_mm_dd: str) -> list[ScheduledMatch]:
        """Scheduled matches whose UTC kickoff falls on the given date."""
        return [m for m in self._matches if m.utc_kickoff[:10] == date_yyyy_mm_dd]
