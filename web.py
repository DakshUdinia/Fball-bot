import os
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
import logging

from fball_bot.database import get_tracked_matches, get_open_trades, get_daily_analytics, get_connection

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Fball-bot Dashboard")

# Determine paths
ROOT_DIR = Path(__file__).parent
STATIC_DIR = ROOT_DIR / "static"
STATIC_DIR.mkdir(exist_ok=True)

# Mount static files
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))

@app.get("/api/stats")
async def get_stats():
    stats = get_daily_analytics()
    # Also fetch all-time open trades count and total realized PnL
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM football_trades WHERE status = 'open'")
    open_count = cur.fetchone()[0]
    
    cur.execute("SELECT SUM(pnl) FROM football_trades WHERE status = 'closed'")
    row = cur.fetchone()
    all_time_pnl = row[0] if row[0] is not None else 0.0
    
    conn.close()
    
    return {
        "today_trades": stats["trades"],
        "today_win_rate": stats["win_rate"],
        "today_pnl": stats["pnl"],
        "open_trades": open_count,
        "all_time_pnl": all_time_pnl
    }

@app.get("/api/matches")
async def get_matches():
    # We fetch all tracked matches
    matches = get_tracked_matches()
    live_matches = [m for m in matches if m["status"] in ("1H", "2H", "HT", "ET", "P", "live", "LIVE")]
    return {
        "live": live_matches,
        "all_tracked": matches
    }

@app.get("/api/trades")
async def get_trades():
    open_trades = get_open_trades()
    
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM football_trades WHERE status = 'closed' ORDER BY closed_at DESC LIMIT 50")
    closed_trades = [dict(r) for r in cur.fetchall()]
    conn.close()
    
    return {
        "open": open_trades,
        "history": closed_trades
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
