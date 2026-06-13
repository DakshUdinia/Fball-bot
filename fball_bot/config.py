"""Bot configuration — reads .env. Fully autonomous, no external dependencies."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


# Live match data (API-Football via RapidAPI)
FOOTBALL_API_KEY = os.getenv("FOOTBALL_API_KEY", "").strip()
_league_str = os.getenv("FOOTBALL_LEAGUE_ID", "1")
FOOTBALL_LEAGUE_ID = int(_league_str) if _league_str else 0
FOOTBALL_SEASON = int(os.getenv("FOOTBALL_SEASON", "2026"))
FOOTBALL_CHECK_INTERVAL = int(os.getenv("FOOTBALL_CHECK_INTERVAL", "15"))  # 15s (was 30s)
MARKET_REFRESH_INTERVAL = int(os.getenv("MARKET_REFRESH_INTERVAL", "60"))  # 60s (was 300s)

# API-Football free tier hard cap. The budget guard stops issuing requests
# (and warns) before this is exhausted so the bot is never blind mid-match.
FOOTBALL_DAILY_REQUEST_BUDGET = int(os.getenv("FOOTBALL_DAILY_REQUEST_BUDGET", "100"))
# Reserve a slice of the budget so a long match doesn't fully drain it.
FOOTBALL_BUDGET_RESERVE = int(os.getenv("FOOTBALL_BUDGET_RESERVE", "5"))
# Reuse the live-fixtures result for this many seconds so discovery and the
# main cycle share one API-Football call instead of issuing duplicates.
FOOTBALL_LIVE_CACHE_TTL = int(os.getenv("FOOTBALL_LIVE_CACHE_TTL", "10"))

# World Cup schedule (football-data.org — free tier, low frequency).
# Used ONLY for the fixture calendar; live events still come from API-Football.
FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY", "").strip()
FOOTBALL_DATA_COMPETITION = os.getenv("FOOTBALL_DATA_COMPETITION", "WC").strip()
# How often to refresh the schedule from football-data.org (seconds). The WC
# calendar barely changes, so twice a day keeps us well under its free cap.
SCHEDULE_REFRESH_INTERVAL = int(os.getenv("SCHEDULE_REFRESH_INTERVAL", "43200"))  # 12h

# Polymarket wallet (Polygon mainnet)
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "").strip()

# Gemini 3.5 Flash (trade scout)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Telegram notifications (optional)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Risk ($10 capital defaults)
INITIAL_CAPITAL = float(os.getenv("INITIAL_CAPITAL", "10.0"))
RISK_MAX_PER_TRADE_USD = float(os.getenv("RISK_MAX_PER_TRADE_USD", "3.0"))
RISK_DAILY_MAX_LOSS_PCT = float(os.getenv("RISK_DAILY_MAX_LOSS_PCT", "0.05"))
RISK_MONTHLY_MAX_LOSS_PCT = float(os.getenv("RISK_MONTHLY_MAX_LOSS_PCT", "0.15"))
RISK_MAX_DRAWDOWN_PCT = float(os.getenv("RISK_MAX_DRAWDOWN_PCT", "0.25"))
RISK_TOTAL_MAX_LOSS_PCT = float(os.getenv("RISK_TOTAL_MAX_LOSS_PCT", "0.40"))
RISK_MAX_CONSECUTIVE_LOSSES = int(os.getenv("RISK_MAX_CONSECUTIVE_LOSSES", "3"))
RISK_PAUSE_MINUTES = int(os.getenv("RISK_PAUSE_MINUTES", "120"))
RISK_ENABLE_DYNAMIC_SIZING = os.getenv("RISK_ENABLE_DYNAMIC_SIZING", "true").lower() in ("1", "true", "yes", "on")

# Bot behavior
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))
MAX_CONCURRENT_MATCHES = int(os.getenv("MAX_CONCURRENT_MATCHES", "99"))  # All matches
PAPER_TRADING = os.getenv("PAPER_TRADING", "true").lower() in ("1", "true", "yes", "on")
MOCK_MISSING_MARKETS = False
