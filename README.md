# Fball-bot

World Cup Polymarket scalping bot. Polls live match events, detects market
overreactions, and executes mean-reversion scalps on Polymarket CLOB.

## Quick Start

```bash
pip install httpx python-dotenv rich
cp .env.example .env   # Add your FOOTBALL_API_KEY
python main.py
```

## How It Works

1. **Goal scored** → market spikes YES price +10-20¢
2. **Model** computes fair reversion target based on minute/score/surprise
3. **If market > target +2¢** → SELL the spike (bet on reversion)
4. **30-180s later** → price corrects → BUY back → capture 2-5¢ profit

## Commands

```
python main.py            # Continuous trading loop
python main.py --status   # Quick check: live matches, PnL, risks
```

## Risk Controls

| Limit | Value | Effect |
|-------|-------|--------|
| Per trade | $3 | Hard cap |
| Daily loss | $0.50 | Pause 2h |
| Consecutive losses | 3 | Pause 2h |
| Total loss | $4 | Permanent halt |

## Data Sources

The bot splits data across two providers to stay within free tiers:

| Data | Provider | Why |
|------|----------|-----|
| Live events (goals, cards, VAR) | API-Football | Latency-sensitive; this is the trading edge |
| World Cup schedule / fixtures | football-data.org | Low-frequency; keeps API-Football budget free |

A daily budget guard tracks API-Football usage (free tier = 100 req/day) and
pauses requests before the cap so the bot is never blind mid-match. The live
fixtures call is cached briefly so discovery and the main loop share one call.

### Environment variables

```
FOOTBALL_API_KEY            # API-Football (live events)
FOOTBALL_DATA_API_KEY       # football-data.org (World Cup schedule)
FOOTBALL_DATA_COMPETITION   # default: WC
FOOTBALL_DAILY_REQUEST_BUDGET  # default: 100
FOOTBALL_BUDGET_RESERVE        # default: 5
FOOTBALL_LIVE_CACHE_TTL        # default: 10 (seconds)
SCHEDULE_REFRESH_INTERVAL      # default: 43200 (12h)
```

## Requirements

- Python 3.12+
- MoneyLine repo at `~/MoneyLine` (for Polymarket trading services)
- API-Football key (free: 100 req/day) — reserved for live events
- football-data.org key (free) — for the World Cup schedule
