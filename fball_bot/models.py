"""Probability model — event-driven deltas on Polymarket pre-match prices.

Core formula: P_new = P_prev + delta * W_time * W_surprise * W_score
Reversion: spike closes by R_rate(minute) over time
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .live_data import MatchEvent, MatchState

logger = logging.getLogger(__name__)

BASE_DELTAS = {
    ("goal", "favorite"): 0.06,
    ("goal", "underdog"): 0.10,
    ("goal", "equal"): 0.08,
    ("red_card", "favorite_sent"): -0.15,
    ("red_card", "underdog_sent"): 0.08,
    ("red_card", "equal"): -0.10,
}

TIME_WEIGHTS: list[tuple[int, float]] = [
    (0, 1.0), (30, 1.0), (45, 1.2), (46, 1.0),
    (60, 1.0), (75, 1.3), (85, 1.6), (90, 2.0),
    (91, 2.5),
]

REVERSION_RATES: list[tuple[int, float]] = [
    (0, 0.30), (30, 0.25), (45, 0.10), (46, 0.25),
    (60, 0.20), (75, 0.15), (85, 0.10), (90, 0.05),
]


def _tw(minute: int) -> float:
    for i in range(len(TIME_WEIGHTS) - 1):
        if TIME_WEIGHTS[i][0] <= minute < TIME_WEIGHTS[i + 1][0]:
            return TIME_WEIGHTS[i][1]
    return TIME_WEIGHTS[-1][1]


def _rr(minute: int) -> float:
    for m, r in REVERSION_RATES:
        if minute < m:
            return r
    return REVERSION_RATES[-1][1]


@dataclass
class ProbabilityUpdate:
    new_prob: float
    delta: float
    reversion_target: float
    scalp_direction: str   # BUY, SELL, or NONE
    confidence: float


class FootballProbabilityModel:
    """Market-calibrated heuristic model for football match probabilities."""

    def __init__(self) -> None:
        self._baselines: dict[str, float] = {}

    def set_baseline(self, fixture_id: int, price: float) -> None:
        self._baselines[str(fixture_id)] = price

    def get_baseline(self, fixture_id: int) -> float | None:
        return self._baselines.get(str(fixture_id))

    def update(
        self,
        current: float,
        event: MatchEvent,
        pre_match: float | None = None,
    ) -> ProbabilityUpdate:
        prior = pre_match or current

        delta = 0.0
        if event.type == "goal":
            is_fav = prior > 0.55
            is_underdog = prior < 0.45
            scoring_is_fav = is_fav if event.team == "home" else not is_fav if is_fav else False

            if scoring_is_fav:
                delta = BASE_DELTAS[("goal", "favorite")]
                if event.team == "away":
                    delta = -delta
            elif is_underdog:
                delta = BASE_DELTAS[("goal", "underdog")]
                if event.team == "home":
                    delta = -delta
                else:
                    delta = abs(BASE_DELTAS[("goal", "underdog")]) * (-1 if event.team == "home" else 1)
            else:
                delta = BASE_DELTAS[("goal", "equal")]
                if event.team == "away":
                    delta = -delta

        elif event.type == "red_card":
            is_fav = prior > 0.55
            if is_fav and event.team == "home":
                delta = BASE_DELTAS[("red_card", "favorite_sent")]
            elif not is_fav and event.team == "away":
                delta = BASE_DELTAS[("red_card", "favorite_sent")]
            elif is_fav and event.team == "away":
                delta = BASE_DELTAS[("red_card", "underdog_sent")]
            else:
                delta = BASE_DELTAS[("red_card", "equal")]

        # Apply weights
        w_time = _tw(event.minute)
        surprise = abs(prior - 0.5) * 2
        w_surprise = 1.0 - surprise * 0.3
        total_g = event.home_score + event.away_score
        w_score = 1.0 if total_g == 0 else 0.9 if total_g == 1 else 0.7 if total_g == 2 else 0.5

        adj = delta * w_time * w_surprise * w_score
        new_prob = max(0.01, min(0.99, current + adj))

        # Reversion target
        reversion = _rr(event.minute)
        spike = new_prob - prior
        reversion_target = max(0.01, min(0.99, new_prob - spike * reversion))

        # Scalp signal
        gap = current - reversion_target
        if gap > 0.02:
            direction = "SELL"
            confidence = min(1.0, gap / 0.05)
        elif gap < -0.02:
            direction = "BUY"
            confidence = min(1.0, abs(gap) / 0.05)
        else:
            direction = "NONE"
            confidence = 0.0

        return ProbabilityUpdate(
            new_prob=round(new_prob, 4),
            delta=round(adj, 4),
            reversion_target=round(reversion_target, 4),
            scalp_direction=direction,
            confidence=round(confidence, 2),
        )
