const elements = {
    pnl: document.getElementById('stat-pnl'),
    winrate: document.getElementById('stat-winrate'),
    trades: document.getElementById('stat-trades'),
    alltime: document.getElementById('stat-alltime'),
    matchesCount: document.getElementById('matches-count'),
    matchesBody: document.getElementById('matches-body'),
    openCount: document.getElementById('open-trades-count'),
    openBody: document.getElementById('open-trades-body'),
    historyBody: document.getElementById('history-trades-body'),
    dot: document.getElementById('connection-dot'),
    status: document.getElementById('connection-status')
};

const formatUSD = (val) => {
    const sign = val >= 0 ? '+' : '-';
    return `${sign}$${Math.abs(val).toFixed(2)}`;
};

async function fetchStats() {
    try {
        const res = await fetch('/api/stats');
        const data = await res.json();
        
        elements.pnl.textContent = formatUSD(data.today_pnl);
        elements.pnl.className = `stat-value ${data.today_pnl >= 0 ? 'text-green' : 'text-red'}`;
        
        elements.winrate.textContent = `${data.today_win_rate.toFixed(1)}%`;
        elements.trades.textContent = data.today_trades;
        
        elements.alltime.textContent = formatUSD(data.all_time_pnl);
        
        elements.dot.className = 'dot live';
        elements.status.textContent = 'Connected (Live)';
    } catch (e) {
        elements.dot.className = 'dot error';
        elements.status.textContent = 'Offline (Reconnecting...)';
    }
}

async function fetchMatches() {
    try {
        const res = await fetch('/api/matches');
        const data = await res.json();
        
        const live = data.live || [];
        elements.matchesCount.textContent = live.length;
        
        elements.matchesBody.innerHTML = live.map(m => `
            <tr>
                <td><strong>${m.minute}'</strong></td>
                <td>
                    <div style="font-weight: 500">${m.home_team}</div>
                    <div style="font-weight: 500">${m.away_team}</div>
                </td>
                <td>
                    <div class="text-blue">${m.home_score}</div>
                    <div class="text-red">${m.away_score}</div>
                </td>
                <td><strong style="font-size: 1.1em">${m.current_yes_price.toFixed(3)}</strong></td>
            </tr>
        `).join('');
        
        if (live.length === 0) {
            elements.matchesBody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-secondary)">No live matches tracked</td></tr>`;
        }
    } catch (e) {
        console.error(e);
    }
}

async function fetchTrades() {
    try {
        const res = await fetch('/api/trades');
        const data = await res.json();
        
        const open = data.open || [];
        elements.openCount.textContent = open.length;
        
        elements.openBody.innerHTML = open.map(t => `
            <tr>
                <td><span class="pill pill-${t.side.toLowerCase()}">${t.side}</span></td>
                <td>$${t.size_usd.toFixed(2)}</td>
                <td>${t.entry_price.toFixed(3)}</td>
                <td style="color: var(--text-secondary); font-size: 0.8em">${t.entry_reason}</td>
            </tr>
        `).join('');
        
        if (open.length === 0) {
            elements.openBody.innerHTML = `<tr><td colspan="4" style="text-align: center; color: var(--text-secondary)">No open positions</td></tr>`;
        }

        const history = data.history || [];
        elements.historyBody.innerHTML = history.slice(0, 10).map(t => {
            const pnlClass = t.pnl >= 0 ? 'text-green' : 'text-red';
            const pnlSign = t.pnl >= 0 ? '+' : '';
            return `
            <tr>
                <td><span class="pill pill-${t.side.toLowerCase()}">${t.side}</span></td>
                <td><span style="font-size: 0.8em; color: var(--text-secondary)">${t.exit_reason || 'closed'}</span></td>
                <td class="${pnlClass}"><strong>${pnlSign}$${Math.abs(t.pnl).toFixed(2)}</strong></td>
                <td style="color: var(--text-secondary); font-size: 0.8em">${new Date(t.closed_at).toLocaleTimeString()}</td>
            </tr>
            `;
        }).join('');

    } catch (e) {
        console.error(e);
    }
}

function poll() {
    fetchStats();
    fetchMatches();
    fetchTrades();
}

// Initial fetch and set interval
poll();
setInterval(poll, 1000);
