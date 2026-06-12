"""Gemini Scout — non-blocking AI trade reviewer for football scalping.

Uses Gemini 3.5 Flash to score trade signals 0-100.
NEVER blocks — fires in daemon thread, caches results.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from .config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

_AUDIT_PROMPT = """You are a sharp football + prediction markets analyst.
Review this scalp trade signal and rate it 0-100.

Context:
- Match: {home_team} vs {away_team}
- Minute: {minute}'
- Score: {home_score}-{away_score}
- Event: {event_type} at {event_minute}' by {event_team}
- Market YES price BEFORE event: {price_before:.3f}
- Market YES price NOW: {price_now:.3f}
- Trade direction: {side}
- Proposed size: ${size:.2f}
- Pre-match baseline: {baseline:.3f}

Scoring guide:
  0-20  = TERRIBLE (dead rubber match, wrong direction, already 3-0 up)
  21-40 = POOR (late stoppage time, already decided, low liquidity)
  41-60 = AVERAGE (standard goal scalp, normal conditions)
  61-80 = GOOD (clear overreaction, knockout match, sweet spot minute)
  81-100 = EXCELLENT (perfect setup: late equalizer, high surprise, liquid market)

Know that:
- A goal in minute 75-85 by underdog in 0-0 draw = excellent scalp opportunity
- A 4th goal at 3-0 = worthless, skip
- Stoppage time goals = less reversion, lower score
- Dead rubber (both teams already eliminated) = lower score
- Knockout match = higher stakes, better liquidity, higher score
- Red card in 1st half = bigger opportunity than 2nd half

Respond with EXACTLY one line: SCORE <number> <one-sentence-reason>
Example: SCORE 78 Late equalizer in 0-0 draw, market will overreact
Example: SCORE 12 Dead rubber match, 3-0 already, no scalp
Example: SCORE 55 Standard goal, moderate reversion expected"""


class GeminiScout:
    """Non-blocking Gemini trade scout. Fires in daemon thread — never blocks."""

    def __init__(self) -> None:
        self._key = GEMINI_API_KEY
        self._client = None
        self._ready = False
        self._cache: dict[int, dict[str, Any]] = {}
        self._init()

    def _init(self) -> None:
        if not self._key:
            logger.info("Gemini Scout disabled — set GEMINI_API_KEY in .env")
            return
        try:
            import google.genai as genai
            self._client = genai.Client(api_key=self._key)
            self._ready = True
            logger.info("Gemini Scout ready (gemini-3.5-flash)")
        except ImportError:
            try:
                import google.generativeai as genai
                genai.configure(api_key=self._key)
                self._client = genai.GenerativeModel("gemini-3.5-flash")
                self._ready = True
                logger.info("Gemini Scout ready (via google-generativeai)")
            except ImportError:
                logger.warning("google-genai not installed. Run: pip install google-genai")

    @property
    def available(self) -> bool:
        return self._ready

    def scout(self, data: dict) -> None:
        """Fire-and-forget trade scout. Runs in daemon thread — NEVER BLOCKS."""
        if not self._ready:
            return
        import threading
        threading.Thread(
            target=lambda: asyncio.run(self._scout_async(data)),
            daemon=True,
        ).start()

    async def _scout_async(self, data: dict) -> None:
        try:
            fid = data.get("fixture_id", 0)
            prompt = _AUDIT_PROMPT.format(
                home_team=data.get("home_team", "?"),
                away_team=data.get("away_team", "?"),
                minute=data.get("minute", 0),
                home_score=data.get("home_score", 0),
                away_score=data.get("away_score", 0),
                event_type=data.get("event_type", "goal"),
                event_minute=data.get("event_minute", 0),
                event_team=data.get("event_team", "?"),
                price_before=data.get("price_before", 0.5),
                price_now=data.get("price_now", 0.5),
                side=data.get("side", "BUY"),
                size=data.get("size", 2.0),
                baseline=data.get("baseline", 0.5),
            )
            result = await self._call(prompt)
            if not result:
                return

            score, reason = self._parse(result)
            self._cache[fid] = {"score": score, "reason": reason, "ts": time.time()}

            if score < 40:
                logger.warning("🧠 SCOUT (%s) score=%d %s", data.get("side", "?"), score, reason)
            elif score > 75:
                logger.info("🧠 SCOUT ✅ (%s) score=%d %s", data.get("side", "?"), score, reason)
            else:
                logger.info("🧠 SCOUT (%s) score=%d %s", data.get("side", "?"), score, reason)
        except Exception as e:
            logger.debug("Gemini scout: %s", e)

    def get_adjustment(self, fixture_id: int) -> float:
        """Get confidence multiplier from cached scout.
        0.0 = veto, 0.5 = poor, 1.0 = average, 1.2 = good, 1.5 = excellent.
        Cache expires after 5 min.
        """
        c = self._cache.get(fixture_id)
        if not c or time.time() - c["ts"] > 300:
            return 1.0
        if c["score"] <= 20:
            return 0.0
        if c["score"] <= 40:
            return 0.5
        if c["score"] <= 60:
            return 1.0
        if c["score"] <= 80:
            return 1.2
        return 1.5

    def get_reason(self, fixture_id: int) -> str:
        c = self._cache.get(fixture_id)
        return c.get("reason", "") if c else ""

    def flush(self, fixture_id: int | None = None) -> None:
        if fixture_id:
            self._cache.pop(fixture_id, None)
        else:
            self._cache.clear()

    def _parse(self, text: str) -> tuple[int, str]:
        text = text.strip()
        if text.upper().startswith("SCORE "):
            rest = text[6:].strip()
            parts = rest.split(" ", 1)
            try:
                return max(0, min(100, int(parts[0]))), (parts[1] if len(parts) > 1 else "")
            except (ValueError, IndexError):
                pass
        return 50, "parse_failed"

    async def _call(self, prompt: str) -> str | None:
        if not self._client:
            return None
        try:
            if hasattr(self._client, "models"):
                resp = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._client.models.generate_content(
                        model="gemini-3.5-flash", contents=prompt,
                    ),
                )
                return resp.text.strip() if (resp and hasattr(resp, "text") and resp.text) else None
            else:
                resp = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self._client.generate_content(prompt),
                )
                return resp.text.strip() if (resp and hasattr(resp, "text") and resp.text) else None
        except Exception as e:
            logger.debug("Gemini API: %s", e)
            return None
