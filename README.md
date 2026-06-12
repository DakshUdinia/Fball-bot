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

## Requirements

- Python 3.12+
- MoneyLine repo at `~/MoneyLine` (for Polymarket trading services)
- API-Football key (free: 100 req/day, enough for 3 matches)
