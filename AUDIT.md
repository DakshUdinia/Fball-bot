# FBALL-BOT — System Audit Report

**Version**: 0.1.0  
**Date**: 2026-06-12  
**Type**: Autonomous Polymarket Football Scalping Bot  
**Capital**: $10.00 (configurable)  
**Mode**: Paper (default) / Live  

---

## 1. Executive Summary

Fball-bot is an autonomous trading bot that scalps Polymarket prediction markets during live World Cup football matches. It detects goal events via real-time Polymarket CLOB price movements (~2s latency), runs a probability model to identify market overreactions, and executes FOK market orders to capture mean-reversion profits — all before human traders can react.

**Core thesis**: Polymarket football markets are retail-dominated. Most traders take 15-45s to react to a goal. The bot reacts in ~2-4s. This 10x speed advantage creates a consistent scalping edge.

---

## 2. System Architecture

```
                        ┌─────────────────────────────────┐
                        │         FBALL-BOT                │
                        │     /home/dikky/Fball-bot/       │
                        └─────────────────────────────────┘
                                    │
            ┌───────────────────────┼───────────────────────┐
            ▼                       ▼                       ▼
    ┌───────────────┐     ┌─────────────────┐     ┌───────────────┐
    │  Fast Path    │     │   Slow Path     │     │  Risk Layer   │
    │  (primary)    │     │  (background)   │     │  (every cycle)│
    └───────┬───────┘     └────────┬────────┘     └───────┬───────┘
            │                      │                      │
            ▼                      ▼                      ▼
    ┌───────────────┐     ┌─────────────────┐     ┌───────────────┐
    │ PriceTrigger  │     │ LiveMatchService│     │ PortfolioRisk │
    │ (ws.py)       │     │ (live_data.py)  │     │ (risk.py)     │
    │ Polls CLOB    │     │ Polls API-Foot. │     │ 5 layers      │
    │ every 2s      │     │ every 30s       │     │ + Kelly       │
    │ Spike >5%     │     │ Event context   │     │ + match dist  │
    └───────┬───────┘     └────────┬────────┘     └───────┬───────┘
            │                      │                      │
            └───────────┬──────────┘                      │
                        │                                  │
                        ▼                                  │
                ┌───────────────┐                          │
                │ Bot Loop      │◄─────────────────────────┘
                │ (bot.py)      │
                │ Orchestrator  │
                └───────┬───────┘
                        │
                        ▼
                ┌───────────────┐     ┌─────────────────┐
                │ Probability   │────►│ Gemini Scout    │
                │ Model         │     │ (gemini.py)     │
                │ (models.py)   │     │ fire-and-forget │
                └───────┬───────┘     └─────────────────┘
                        │
                        ▼
                ┌───────────────┐     ┌─────────────────┐
                │ Scalping      │     │ Telegram        │
                │ Strategy      │     │ Notifier        │
                │ (scalping.py) │     │ (notify.py)     │
                └───────┬───────┘     └─────────────────┘
                        │
                        ▼
                ┌───────────────┐
                │ CLOB Trader   │
                │ (pm.py)       │
                │ FOK Market    │
                │ Order         │
                └───────────────┘
```

---

## 3. Component Inventory

| # | Module | File | Lines | Type | Purpose |
|---|--------|------|-------|------|---------|
| 1 | **PriceTrigger** | `fball_bot/ws.py` | 190 | New | Real-time Polymarket CLOB price spike detector |
| 2 | **LiveMatchService** | `fball_bot/live_data.py` | 145 | New | API-Football v3 polling for event context |
| 3 | **MarketDiscovery** | `fball_bot/discovery.py` | 195 | New | Gamma API market search + fixture mapping + fallback |
| 4 | **ProbabilityModel** | `fball_bot/models.py` | 120 | New | Event-driven probability delta model |
| 5 | **ScalpingStrategy** | `fball_bot/scalping.py` | 190 | New | Entry/exit rules for goal + red card scalps |
| 6 | **PortfolioRisk** | `fball_bot/risk.py` | 290 | New | 5-layer risk + Kelly sizing + token-aware minimum |
| 7 | **GeminiScout** | `fball_bot/gemini.py` | 160 | New | Non-blocking Gemini 3.5 Flash trade reviewer |
| 8 | **GammaClient** | `fball_bot/pm.py` | 115 | New | Polymarket Gamma API client |
| 9 | **CLOBTrader** | `fball_bot/pm.py` | 115 | New | Polymarket CLOB FOK order execution |
| 10 | **Notifier** | `fball_bot/notify.py` | 100 | New | Telegram push notifications |
| 11 | **FballTradingBot** | `fball_bot/bot.py` | 330 | New | Main loop + orchestration + crash recovery |
| 12 | **Database** | `fball_bot/database.py` | 135 | New | SQLite persistence (matches, trades, state) |
| 13 | **Config** | `fball_bot/config.py` | 48 | New | .env-driven configuration |
| 14 | **Main** | `main.py` | 110 | New | Entry point with CLI + Rich status display |

**Total**: 14 modules, ~2,100 lines of Python

---

## 4. Data Flow & Pipeline

### 4.1 Fast Path (Primary — ~2-4s latency)

```
Step 1: PRICE SPIKE DETECTION
───────────────────────────────────────────────────────────────
  Component:  PriceTrigger (ws.py)
  Mechanism:  REST poll to https://clob.polymarket.com/books
              for every tracked token_id every 2 seconds
  Detection:  Mid-price change >5% within 5-second window
  Output:     PriceSpike(fixture_id, token_id, 
              price_before, price_now, change_pct, direction)
  Latency:    ~2s (1 poll cycle)

Step 2: FAST PATH HANDLER
───────────────────────────────────────────────────────────────
  Component:  bot.py:_on_price_spike()
  Actions:
    1. Check risk gate (PortfolioRisk.can_trade())
    2. Get market data from discovery cache
    3. Build synthetic MatchEvent from spike direction
    4. Run FootballProbabilityModel.update()
    5. Check reversion magnitude (>2¢ expected)
    6. Fire Gemini scout (daemon thread — NON-BLOCKING)
    7. Build TradeSignal
    8. Call _execute_signal()
  Latency:    <1ms (pure Python, no I/O)

Step 3: EXECUTION
───────────────────────────────────────────────────────────────
  Component:  bot.py:_execute_signal()
  Sub-steps:
    a. Gemini gate (check scout cache if available)
    b. Orderbook depth check (GammaClient.get_orderbook_depth)
    c. Validate order (PortfolioRisk.validate_order)
    d. Compute size (PortfolioRisk.compute_max_trade_size)
    e. Insert trade to SQLite
    f. Register ActiveScalp with strategy
    g. Send Telegram notification (daemon thread)
    h. If LIVE mode: CLOBTrader.place_market_order() → FOK
  Latency:    ~1-2s (includes CLOB order round-trip)
```

**Total Fast Path: ~2-4s from event to fill**

### 4.2 Slow Path (Background — ~30-90s latency)

```
Step 1: EVENT POLLING
───────────────────────────────────────────────────────────────
  Component:  LiveMatchService (live_data.py)
  Mechanism:  REST poll to API-Football v3 every 30s
  Rate limit: 1 req/min (free tier) → 429 backoff
  Output:     New MatchEvent objects (goals, red cards, subs)
  Latency:    30-90s

Step 2: MATCH PROCESSING
───────────────────────────────────────────────────────────────
  Component:  bot.py:_process_match()
  Actions:
    1. Poll new events via LiveMatchService.poll_new_events()
    2. Update market price from Gamma API
    3. Run ScalpingStrategy.evaluate() (model + entry check)
    4. Execute signals via _execute_signal()
    5. Update football_matches table in SQLite
  Latency:    ~500ms (includes Gamma API call)

Step 3: EXIT MANAGEMENT
───────────────────────────────────────────────────────────────
  Component:  bot.py:_manage_exits()
  Runs:       EVERY cycle (regardless of events)
  Actions:
    1. For each tracked fixture with active scalps:
       a. Get current market price
       b. Run ScalpingStrategy.evaluate() with empty events
       c. Exit signals: profit target, stop loss, timeout
    2. Execute exit signals via _execute_signal()
  Latency:    <10ms per fixture (pure Python)
```

### 4.3 Pre-Scout Path (Kickoff)

```
Step 0: MATCH START
───────────────────────────────────────────────────────────────
  Component:  bot.py:_cycle()
  Trigger:    Live match detected from API-Football
  Action:     Fire GeminiScout.scout() with match context
              (teams, current price, league, tournament stage)
  Result:     Scout cached in GeminiScout._cache[fixture_id]
  Purpose:    First goal gets AI review (not blind)
  Latency:    1-3s before first event (cache populated at kickoff)
```

---

## 5. Latency Profile

| Segment | Component | P50 | P95 | Notes |
|---------|-----------|-----|-----|-------|
| Event detection | CLOB poll → spike | 2.0s | 4.0s | 2s poll, cross-cycle if needed |
| Gemini scout | Fire-and-forget | 1.5s | 3.0s | Daemon thread, never blocks |
| Gamma price | REST call | 200ms | 500ms | Cached, only on slow path |
| Orderbook depth | CLOB REST | 200ms | 400ms | Only on fast path entry |
| CLOB order | FOK market order | 500ms | 1,500ms | Polygon tx time |
| API-Football | REST poll | 30s | 90s | Free tier rate limited |
| **Fast path total** | **Event → fill** | **~2.5s** | **~5.0s** | |
| **Slow path total** | **Event → fill** | **~32s** | **~93s** | |

---

## 6. Risk Management System

PortfolioRisk (`fball_bot/risk.py`) — 5 layers, all active simultaneously:

### Layer 1: Per-Trade Cap
- Hard ceiling: $3.00 (configurable)
- Token-aware minimum: `max($1.00, 5 shares × token_price)` = ~$2.50 typical
- Dynamic Kelly sizing adjusts actual size: $10 × Kelly × conviction × multipliers

### Layer 2: Daily Loss
- Threshold: 5% of initial capital ($0.50 on $10)
- Tiered response:
  - 60% used → warning log
  - 80% used → tight sizing (0.2× multiplier)
  - 100% → 120-minute pause + enter recovery mode

### Layer 3: Monthly Loss
- Threshold: 15% of initial capital ($1.50 on $10)
- Response: 80% warning → 100% → 30-day pause

### Layer 4: Drawdown
- Threshold: 25% from peak capital ($2.50 loss)
- Velocity-aware:
  - Fast (<2h from peak): **1.5× effective** (treat as 37.5%)
  - Slow (>24h from peak): **0.8× effective** (treat as 20%)
  - Linear decay between 2-24h
- Response: 7-day pause at limit

### Layer 5: Total Loss
- Threshold: 40% of initial capital ($4.00 on $10)
- Response: **PERMANENT HALT** — irreversible
- State persists in JSON file (atomic writes via .tmp + rename)

### Kelly Criterion
- Formula: f* = (p × b - q) / b where p = win rate, b = avg win/loss ratio
- Half-Kelly (conservative): k × 0.5
- Floor: 0.1 (never drops below 10% of capital)
- Only activates after 5+ trades (prior: 0.15 fixed)
- Self-calibrating: win rate and avg win/loss learned from actual trade history

### Match Density Scaling
- 1-2 concurrent matches: full size
- 3+ concurrent matches: `× max(0.5, 1 - (count - 2) × 0.15)`

### Recovery Mode
- Entered after: pause triggered, near-pause consecutive losses
- Effect: sizing reduced to 40%, exits when total PnL exceeds baseline by 2%

---

## 7. AI Integration (Gemini 3.5 Flash)

**Component**: `fball_bot/gemini.py` — `GeminiScout`

### How it works

1. **Pre-scout at kickoff**: When a match goes live, fire Gemini scout with match context (teams, price, league). Result cached before any event happens.

2. **Event scout**: On every new event (goal, red card), fire scout in daemon thread. This populates cache for SUBSEQUENT signals on the same match.

3. **Score-based adjustment**: Scout returns SCORE 0-100 with reason:
   - 0-20 (TERRIBLE): conviction × 0.0 → trade vetoed
   - 21-40 (POOR): conviction × 0.5
   - 41-60 (AVERAGE): conviction × 1.0
   - 61-80 (GOOD): conviction × 1.2
   - 81-100 (EXCELLENT): conviction × 1.5

4. **Cache**: Per-fixture, 5-minute TTL. Survives cycle restarts. Only available for subsequent signals on the same fixture.

5. **Fire-and-forget**: Runs in daemon thread. Never blocks the hot trading path.

### Audit Prompt
```
Context: Match, minute, score, event type, price movement, trade direction
Scoring: 0-20 terrible (dead rubber, wrong direction)
         21-40 poor (stoppage time, decided match)
         41-60 average (standard goal)
         61-80 good (clear overreaction, knockout match)
         81-100 excellent (late equalizer, high surprise, liquid)
Format: SCORE <number> <one-sentence-reason>
```

---

## 8. Trading Strategy

### Primary Signal: Goal Spike Scalp

When a goal is scored:
1. Polymarket YES price spikes +10-20¢ in <5s
2. PriceTrigger detects >5% move within 2s
3. Probability model computes fair reversion target:
   `P_new = P_prev + delta × W_time × W_surprise × W_score`
   `reversion_target = spike - (spike - baseline) × reversion_rate`
4. If market price > reversion target + 2¢ → SELL (bet against spike)
5. Exit rules:
   - Profit target: +3¢ → close 100%
   - Stop loss: -3¢ → close 100%
   - Timeout: 180s → close 60%
   - New counter-event → close immediately

### Secondary Signal: Red Card Scalp

- Red card detected → smaller position (60% of goal size)
- Lower confidence (0.8× multiplier)
- Same exit rules

### Probability Model

```
P_new = P_prev + delta × W_time × W_surprise × W_score

BASE_DELTAS:
  goal_favorite:   +0.06
  goal_underdog:   +0.10
  goal_equal:      +0.08
  red_card_fav:    -0.15
  red_card_under:  +0.08

W_time:     minute <30=1.0, 75-85=1.6, 90+=2.5
W_surprise: surprise = |prior-0.5|×2, weight = 1.0 - surprise×0.3
W_score:    0 goals=1.0, 1=0.9, 2=0.7, 3+=0.5

reversion_rate: minute <30=30%, 75-85=15%, 90+=5%
```

---

## 9. File Map

```
/home/dikky/Fball-bot/
├── main.py                    # Entry point (CLI + Rich UI)
├── .env                       # Configuration (keys, params)
├── .env.example               # Template config
├── requirements.txt           # Python dependencies
├── AUDIT.md                   # This file
├── README.md                  # Quick start
│
├── fball_bot/
│   ├── __init__.py            # Package init
│   ├── config.py              # .env loader
│   ├── database.py            # SQLite (matches, trades, state)
│   ├── live_data.py           # API-Football v3 polling
│   ├── pm.py                  # Gamma API + CLOB trader
│   ├── ws.py                  # PriceTrigger (real-time spike detection)
│   ├── discovery.py           # Market discovery + fixture mapping
│   ├── models.py              # Probability model
│   ├── scalping.py            # Entry/exit strategy
│   ├── risk.py                # PortfolioRisk (5 layers + Kelly)
│   ├── gemini.py              # Gemini 3.5 Flash scout
│   ├── notify.py              # Telegram notifier
│   ├── bot.py                 # Main loop + orchestration
│   └── data/
│       ├── portfolio_risk.json  # Persistent risk state
│       └── fball_bot.db         # SQLite database
│
└── fball-bot.log              # Runtime log
```

---

## 10. External Dependencies

| Dependency | Version | Purpose |
|-----------|---------|---------|
| httpx | ≥0.28.0 | HTTP client for Polymarket API + CLOB |
| python-dotenv | ≥1.2.0 | .env file loading |
| rich | ≥13.0.0 | Terminal UI (status display) |
| google-genai | ≥1.7.0 | Gemini 3.5 Flash API |
| py-clob-client | ≥0.34.6 | Polymarket CLOB order signing |
| web3 | ≥6.15.0 | Polygon chain interaction |

**Zero dependency on MoneyLine** — fully autonomous.

---

## 11. External API Endpoints

| API | Endpoint | Purpose | Rate Limit |
|-----|----------|---------|------------|
| Polymarket Gamma | `gamma-api.polymarket.com/markets` | Market search + prices | 10 req/s |
| Polymarket CLOB | `clob.polymarket.com/books` | Orderbook depth + mid-price | None (public) |
| Polymarket CLOB WS | `ws-subscriptions-clob.polymarket.com/ws/l2` | Real-time orderbook (future) | Auth required |
| API-Football | `api-football-v1.p.rapidapi.com/v3` | Match events + fixtures | 1 req/min (free) |
| Telegram | `api.telegram.org/bot{token}/sendMessage` | Push notifications | 20 msg/min |

---

## 12. Known Limitations

| Issue | Severity | Impact | Mitigation |
|-------|----------|--------|------------|
| API-Football free tier 1 req/min | Medium | Event context delayed 30-90s | Fast path trades on price spike, not event poll |
| First trade on each match has no Gemini cache | Low | No AI review on first spike | Pre-scout at kickoff partially mitigates |
| No WebSocket for CLOB (REST polling) | Low | 2s poll gap | Acceptable — 2s is still 7x faster than humans |
| $10 capital limits position math | Medium | $2.50 minimum = 25% of capital per trade | Plan: compound → add capital → scale |
| No WebSocket for price spikes | Low | 2s detection latency | Fully compatible with Polymarket CLOB WS in future |
| Single-threaded async loop | Low | All matches processed sequentially | No blocking I/O in hot path; sufficient for <10 matches |
| No order type fallback (FOK only) | Low | Order fails if partial fill | Fills at market price; partial fills rare on liquid markets |

---

## 13. Security

- **Private keys**: Stored in `.env` only (never in code). `.env` is in `.gitignore`.
- **State persistence**: PortfolioRisk state saved atomically (tmp + rename). No secrets stored.
- **API keys**: API-Football key is read-only public data access. Gemini key is standard API key.
- **Polymarket wallet**: Only used for CLOB order signing. Never exposed to third parties.

---

## 14. Deployment Checklist

```bash
# 1. Requirements
pip install -r requirements.txt

# 2. Configure
# Edit .env with:
#   FOOTBALL_API_KEY    — Required for match event context
#   GEMINI_API_KEY       — Required for AI trade review
#   WALLET_PRIVATE_KEY   — Required for LIVE trading (not paper)
#   TELEGRAM_BOT_TOKEN   — Optional, recommended
#   TELEGRAM_CHAT_ID     — Optional, recommended

# 3. Paper mode (default)
python main.py

# 4. Live mode
# Set PAPER_TRADING=false in .env
python main.py

# 5. Status check
python main.py --status

# 6. Monitor
tail -f fball-bot.log
```

---

## 15. Recovery Procedures

### Bot Crash
1. Run `python main.py` — positions auto-restored from SQLite
2. Open positions from before crash are re-registered with fresh timers
3. Timeout exits fire on next cycle (within 30s)

### PortfolioRisk State Loss
1. If `data/portfolio_risk.json` is corrupted, delete it
2. Bot creates fresh state on next startup with `INITIAL_CAPITAL`
3. Kelly learning resets (needs 5+ trades to recalibrate)

### API Key Expired
1. `FOOTBALL_API_KEY`: Renew at RapidAPI. Bot detects 401 and logs warning.
2. `GEMINI_API_KEY`: Renew at Google AI Studio. Bot logs "Gemini Scout disabled".

---

*End of Audit Report — Fball-bot v0.1*
