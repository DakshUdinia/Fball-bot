"""Score-state probability model for football matches.

Inspired by Dixon-Coles, this model moves beyond simple flat heuristics.
It calculates probability changes based on:
1. Current score state (0-0 vs 3-0)
2. Time remaining (minute 10 vs minute 80)
3. Event type (goal, red card, VAR reversal)
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)


class FootballReversionModel:
    """Predicts market price movements based on football events."""

    def __init__(self) -> None:
        # Base probabilities for a typical match (pre-game)
        self.base_home_win_prob = 0.45
        self.base_away_win_prob = 0.30
        self.base_draw_prob = 0.25
        self._baselines: dict[int, float] = {}

    def get_baseline(self, fixture_id: int) -> float | None:
        return self._baselines.get(fixture_id)

    def set_baseline(self, fixture_id: int, price: float) -> None:
        self._baselines[fixture_id] = price

    def update(self, current: float, event: Any, pre_match: float) -> Any:
        # Reconstruct the expected return object for price spikes
        from dataclasses import dataclass
        @dataclass
        class ModelUpdate:
            scalp_direction: str
            reversion_target: float
            confidence: float
            
        # Fast path computation for spike reversions
        # Spikes generally revert unless backed by a major event
        # If fake_event team is "home" (meaning YES price spiked UP)
        if event.team == "home":
            target = current - 0.05  # Expect 5c reversion
            return ModelUpdate("SELL", max(0.01, target), 1.0)
        else:
            target = current + 0.05  # Expect 5c reversion
            return ModelUpdate("BUY", min(0.99, target), 1.0)

    def compute_event_impact(
        self,
        event_type: str,
        team_side: str,      # "home" or "away"
        market_home: str,    # Which team the Polymarket contract is about
        market_away: str,
        minute: int,
        home_score: int,
        away_score: int,
    ) -> float:
        """Returns the expected % change in win probability for the market's target team."""
        
        # 1. Figure out if the event helps or hurts the market's target team
        # E.g., if market is "Will France beat Argentina" and France is 'home':
        # - France goal (home goal) -> Positive impact
        # - Argentina goal (away goal) -> Negative impact
        is_positive_event = False
        if event_type in ("goal", "penalty_awarded"):
            is_positive_event = (team_side == "home" and market_home == "home") or \
                                (team_side == "away" and market_home == "away")
        elif event_type in ("red_card", "var_reversal"):
            # Red card or goal cancelled HURTS the team it happens to
            is_positive_event = (team_side == "away" and market_home == "home") or \
                                (team_side == "home" and market_home == "away")
        elif event_type == "own_goal":
             # Own goal is already handled by live_data.py flipping the team side
             is_positive_event = (team_side == "home" and market_home == "home") or \
                                 (team_side == "away" and market_home == "away")
        else:
            return 0.0

        # 2. Time factor (decay)
        # Events matter MORE late in the game (less time to recover)
        time_factor = self._compute_time_factor(minute)

        # 3. Score state factor
        # A goal making it 1-0 is huge. A goal making it 4-0 is negligible.
        state_factor = self._compute_state_factor(home_score, away_score, is_positive_event)

        # 4. Base impact by event type
        base_impact = 0.0
        if event_type in ("goal", "own_goal"):
            base_impact = 0.20  # 20% base probability shift for a goal
        elif event_type == "red_card":
            base_impact = 0.12  # 12% shift for a red card
        elif event_type == "penalty_awarded":
            base_impact = 0.15  # 75% chance of goal * 0.20 impact = 0.15
        elif event_type == "var_reversal":
            base_impact = -0.20 # Exact opposite of a goal

        # Calculate total impact
        impact = base_impact * time_factor * state_factor
        
        # Direction
        return impact if is_positive_event else -impact

    def _compute_time_factor(self, minute: int) -> float:
        """Events later in the game have a stronger impact on final outcome."""
        if minute <= 0: return 0.5
        if minute >= 90: return 1.5
        
        # Logistic curve: impact grows as time runs out
        # 45' = 1.0, 90' = 1.5
        return 0.5 + (minute / 90.0)

    def _compute_state_factor(self, home_score: int, away_score: int, is_positive: bool) -> float:
        """A goal changing a draw to a lead is worth more than a blowout goal."""
        goal_diff = abs(home_score - away_score)
        
        if goal_diff == 0:
            # Breaking a tie -> Huge impact
            return 1.2
        elif goal_diff == 1:
            # Extending a lead -> Moderate impact
            # Or equalizing -> Huge impact (handled by goal_diff becoming 0 on NEXT event)
            # Actually, if we are trailing by 1 and score, goal_diff was 1.
            if is_positive:
                return 0.8  # Extending lead
            else:
                return 1.2  # Equalizing
        elif goal_diff == 2:
            return 0.4
        else:
            # 3+ goal difference -> Game is already decided
            return 0.1

    def compute_reversion_target(self, current_price: float, expected_change: float) -> float:
        """Calculate the target price we expect the market to settle at."""
        target = current_price + expected_change
        return max(0.01, min(0.99, target))
