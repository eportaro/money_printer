/* ═══════════════════════════════════════════════════════════
   Polymarket BTC Round Tracker — Logic
   ═══════════════════════════════════════════════════════════ */

let activeCutoff = null;

document.addEventListener('DOMContentLoaded', () => {
    updateAll();
    setInterval(updateAll, 10000); // Poll every 10s
    setInterval(() => {
        if (activeCutoff) updateTimer(activeCutoff);
    }, 1000);

    document.getElementById('sync-baseline-btn').addEventListener('click', async () => {
        const input = document.getElementById('manual-baseline-input');
        const price = parseFloat(input.value);
        if (isNaN(price)) return;

        // Get the current target cutoff
        try {
            const predRes = await fetch('/api/predict');
            const predData = await predRes.json();
            const cutoff = predData.next_cutoff;

            const res = await fetch('/api/sync_history_baseline', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ price: price, cutoff: cutoff })
            });
            if (res.ok) {
                input.value = '';
                updateAll();
            }
        } catch (e) {}
    });
});

async function updateAll() {
    await fetchPrice();
    await fetchPrediction();
    await fetchPerformance();
    await fetchSignals();
    await fetchRounds();
    await fetchStats();
}

async function fetchPrice() {
    try {
        const res = await fetch('/api/price?minutes=1');
        const data = await res.json();
        document.getElementById('current-price').textContent = `$${data.current_price.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
    } catch (e) {}
}

async function fetchPrediction() {
    try {
        const res = await fetch('/api/predict');
        const data = await res.json();
        
        activeCutoff = data.next_cutoff;
        updateTimer(activeCutoff);
        
        // Active Round Card
        document.getElementById('round-open-price').textContent = `$${data.window_open.toLocaleString('en-US', { minimumFractionDigits: 2 })}`;
        const baselineSource = data.baseline_source || 'unknown';
        const sourceLabel = {
            manual_sync: 'BASELINE MANUAL',
            polymarket_gamma: 'BASELINE POLYMARKET API',
            binance_prev_close: 'BASELINE BINANCE / NO CHAINLINK',
            binance_window_open: 'BASELINE BINANCE FALLBACK'
        }[baselineSource] || baselineSource;
        const sourceEl = document.getElementById('baseline-source');
        sourceEl.textContent = sourceLabel;
        sourceEl.className = `baseline-source ${baselineSource.includes('binance') ? 'warn' : ''}`;
        
        const predEl = document.getElementById('round-prediction');
        predEl.textContent = data.prediction;
        predEl.className = `value ${data.prediction.toLowerCase()}`;
        document.getElementById('active-model-label').textContent = data.model_version || data.active_model || '---';
        
        const dist = data.current_price - data.window_open;
        const distEl = document.getElementById('round-dist-open');
        distEl.textContent = `${dist >= 0 ? '+' : ''}$${dist.toFixed(2)}`;
        distEl.className = `value ${dist >= 0 ? 'up' : 'down'}`;

        const action = data.recommended_action || 'WAIT';
        const actionEl = document.getElementById('round-action');
        actionEl.textContent = action.replace('_', ' ');
        actionEl.className = `value ${action.toLowerCase().replace('_', '-')}`;

        const edgeUp = data.edge_up === null || data.edge_up === undefined ? '--' : `${(data.edge_up * 100).toFixed(1)}%`;
        const edgeDown = data.edge_down === null || data.edge_down === undefined ? '--' : `${(data.edge_down * 100).toFixed(1)}%`;
        const upAsk = data.up_ask === null || data.up_ask === undefined ? '--' : data.up_ask.toFixed(2);
        const downAsk = data.down_ask === null || data.down_ask === undefined ? '--' : data.down_ask.toFixed(2);
        document.getElementById('round-edge').textContent = `UP ${edgeUp} @${upAsk} | DOWN ${edgeDown} @${downAsk}`;
        
        // Meter
        const conf = data.confidence * 100;
        document.getElementById('confidence-val').textContent = `${conf.toFixed(1)}% CONFIDENCIA`;
        
        // Meter logic: 50% is neutral. UP moves it right, DOWN moves it left.
        const fill = document.getElementById('meter-fill');
        if (data.prediction === 'UP') {
            fill.style.width = `${50 + (conf / 2)}%`;
        } else {
            fill.style.width = `${50 - (conf / 2)}%`;
        }
        
    } catch (e) {}
}

async function fetchPerformance() {
    try {
        const res = await fetch('/api/model-performance');
        const data = await res.json();
        const report = data.report || {};
        const training = data.training || {};
        const marketMetrics = training.market_metrics || {};
        const strategy = data.strategy || {};

        document.getElementById('model-mode-pill').textContent = data.active_model || '---';
        document.getElementById('perf-modeling-rows').textContent = report.modeling_rows ?? '---';
        document.getElementById('perf-rounds').textContent = report.unique_resolved_rounds ?? '---';
        document.getElementById('perf-features').textContent = marketMetrics.feature_count ?? '---';
        document.getElementById('perf-threshold').textContent = data.edge_threshold !== undefined ? `${(data.edge_threshold * 100).toFixed(1)}%` : '---';

        document.getElementById('perf-row-acc').textContent = pct(report.row_accuracy_pct);
        document.getElementById('perf-first-acc').textContent = pct(report.first_prediction_round_accuracy_pct);
        document.getElementById('perf-last-acc').textContent = pct(report.last_prediction_round_accuracy_pct);
        document.getElementById('perf-roc').textContent = marketMetrics.roc_auc !== undefined ? Number(marketMetrics.roc_auc).toFixed(3) : '---';

        document.getElementById('perf-bets').textContent = strategy.closed_bets ?? report.closed_bets ?? '---';
        document.getElementById('perf-bet-win').textContent = pct(strategy.win_rate ?? report.bet_win_rate_pct);
        const pnl = strategy.pnl ?? report.bet_total_pnl;
        const pnlEl = document.getElementById('perf-pnl');
        pnlEl.textContent = pnl === undefined || pnl === null ? '---' : `${pnl >= 0 ? '+' : ''}$${Number(pnl).toFixed(2)}`;
        pnlEl.className = pnl >= 0 ? 'up' : pnl < 0 ? 'down' : '';
        document.getElementById('perf-market-ready').textContent = training.market_model_available ? 'SI' : 'NO';

        document.getElementById('model-note').textContent = training.market_model_available
            ? 'La prediccion live usa market-aware-v1: features tecnicas, quotes de Polymarket y el baseline anterior como senal auxiliar.'
            : 'La web esta usando el modelo Binance porque no encontro model_supabase.pkl.';
    } catch (e) {}
}

function pct(value) {
    if (value === undefined || value === null || Number.isNaN(Number(value))) return '---';
    return `${Number(value).toFixed(1)}%`;
}

async function fetchSignals() {
    try {
        const res = await fetch('/api/signals?limit=20');
        const data = await res.json();
        const body = document.getElementById('signals-body');
        body.innerHTML = '';

        data.forEach(s => {
            const tr = document.createElement('tr');
            const observed = new Date(s.observed_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            const prob = s.model_prob === null || s.model_prob === undefined ? '--' : `${(s.model_prob * 100).toFixed(1)}%`;
            const edge = s.edge === null || s.edge === undefined ? '--' : `${(s.edge * 100).toFixed(1)}%`;
            const pnl = s.pnl === null || s.pnl === undefined ? '---' : `${s.pnl >= 0 ? '+' : ''}$${Number(s.pnl).toFixed(2)}`;
            const result = s.result || 'OPEN';
            const resultClass = result.toLowerCase();
            const sideClass = s.side ? s.side.toLowerCase() : '';

            tr.innerHTML = `
                <td>${observed}</td>
                <td>${s.seconds_to_cutoff ?? '--'}s</td>
                <td class="pred-${sideClass}">${s.side}</td>
                <td>$${Number(s.entry_price).toFixed(2)}</td>
                <td>${prob}</td>
                <td>${edge}</td>
                <td class="res-${resultClass}">${result}</td>
                <td class="pnl ${s.pnl >= 0 ? 'up' : s.pnl < 0 ? 'down' : ''}">${pnl}</td>
            `;
            body.appendChild(tr);
        });
    } catch (e) {}
}

async function fetchRounds() {
    try {
        const res = await fetch('/api/rounds');
        const data = await res.json();
        
        const body = document.getElementById('rounds-body');
        body.innerHTML = '';
        
        data.forEach(r => {
            const tr = document.createElement('tr');
            const timeStr = new Date(r.next_cutoff * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
            const actualOutcome = r.actual_outcome || (r.actual_close && r.window_open
                ? r.actual_close > r.window_open ? 'UP' : r.actual_close < r.window_open ? 'DOWN' : 'TIE'
                : '---');
            const baselineCell = r.window_open === null || r.window_open === undefined ? '---' : `$${Number(r.window_open).toFixed(2)}`;
            const closeCell = r.actual_close === null || r.actual_close === undefined ? '---' : '$' + Number(r.actual_close).toFixed(2);
            
            tr.innerHTML = `
                <td>${timeStr}</td>
                <td class="editable-baseline" onclick="manualSyncHistory(${r.next_cutoff}, ${r.window_open || 0})">
                    ${baselineCell} <span class="edit-icon">✎</span>
                </td>
                <td class="pred-${r.prediction.toLowerCase()}">${r.prediction}</td>
                <td>${closeCell}</td>
                <td class="pred-${actualOutcome.toLowerCase()}">${actualOutcome}</td>
                <td class="res-${(r.outcome || '').toLowerCase()}">${r.outcome || 'ESPERANDO...'}</td>
            `;
            body.appendChild(tr);
        });
    } catch (e) {}
}

async function manualSyncHistory(cutoff, currentVal) {
    const newVal = prompt(`Sincronizar Baseline para el corte de las ${new Date(cutoff*1000).toLocaleTimeString()}:`, currentVal);
    if (newVal === null || newVal === "" || isNaN(parseFloat(newVal))) return;

    try {
        const res = await fetch('/api/sync_history_baseline', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cutoff: cutoff, price: parseFloat(newVal) })
        });
        if (res.ok) {
            updateAll();
        }
    } catch (e) { alert("Error al sincronizar"); }
}

async function fetchStats() {
    try {
        const res = await fetch('/api/stats');
        const data = await res.json();
        const pnl = data.pnl === undefined ? '' : ` | PnL ${data.pnl >= 0 ? '+' : ''}$${Number(data.pnl).toFixed(2)}`;
        document.getElementById('win-rate-display').textContent = `Win Rate: ${data.win_rate}% (${data.total})${pnl}`;
    } catch (e) {}
}

function updateTimer(cutoff) {
    const now = Math.floor(Date.now() / 1000);
    const diff = cutoff - now;
    if (diff <= 0) {
        document.getElementById('cutoff-time').textContent = "00:00:00";
        return;
    }
    const m = Math.floor(diff / 60);
    const s = diff % 60;
    document.getElementById('cutoff-time').textContent = `00:${m.toString().padStart(2, '0')}:${s.toString().padStart(2, '0')}`;
}
