const BUCKETS = [895, 840, 720, 600, 480, 360, 240, 180, 120, 90, 60, 30, 15, 5];
const EDGE_BASE_KEY = 'edgebase';
const LAST_ALLOWED_SIGNAL_BUCKET = 30;
const EDGE_BASE_MIN_ENTRY = 0.05;
const PROVISIONAL_CLOSE_AFTER_SECONDS = 20;
const SOURCE_VERSION = 'dashboard-source-v23-live-strategy';
// Maps the collector's STRATEGY_VERSION (stored on each real signal) to its
// Strategy Lab card, so live paper trades land on the strategy that produced
// them instead of all piling into Edge Base.
const LIVE_STRATEGY_CARD_KEYS = {
    'edge-v1': EDGE_BASE_KEY,
    'value-aligned-v1': 'value_aligned'
};

function liveStrategyCardKey() {
    return LIVE_STRATEGY_CARD_KEYS[state.dashboard?.live_strategy?.strategy_version] || EDGE_BASE_KEY;
}

function initialSignalSource() {
    const savedVersion = localStorage.getItem('dashboardSourceVersion');
    const saved = localStorage.getItem('selectedSignalSource');
    const valid = ['model_test', 'live_active'];
    if (!valid.includes(saved)) {
        localStorage.setItem('dashboardSourceVersion', SOURCE_VERSION);
        localStorage.setItem('selectedSignalSource', 'model_test');
        return 'model_test';
    }
    if (savedVersion !== SOURCE_VERSION) {
        localStorage.setItem('dashboardSourceVersion', SOURCE_VERSION);
        // New dashboard version: default the selected strategy to the one live in prod.
        localStorage.setItem('selectedStrategy', 'value_aligned');
    }
    return saved;
}

const STRATEGIES = {
    edgebase: {
        name: 'Edge Base / Collector',
        description: 'Regla base del collector con guardrails: evita cuotas extremas y senales de ultimo segundo.',
        allowedBuckets: BUCKETS.slice(),
        minProb: 0.00,
        minEdge: 0.03,
        allowContrarian: true,
        maxEntry: 1.00,
        excludeBelow: 0,
        badge: 'EDGE BASE'
    },
    conservative: {
        name: 'Directional Conservative',
        description: 'Compra solo cuando la senal sigue la direccion principal del modelo.',
        allowedBuckets: [480, 360, 240],
        minProb: 0.55,
        minEdge: 0.05,
        allowContrarian: false,
        maxEntry: 0.95,
        excludeBelow: 120,
        badge: 'LOW RISK'
    },
    value: {
        name: 'Value Bet / Longshot',
        description: 'Busca cuotas baratas con edge matematico, aunque la probabilidad de ganar sea baja.',
        allowedBuckets: BUCKETS.slice(),
        minProb: 0.01,
        minEdge: 0.10,
        allowContrarian: true,
        maxEntry: 0.25,
        excludeBelow: 0,
        minAskSize: 50,
        badge: 'HIGH RISK'
    },
    value_high_edge: {
        name: 'Value High Edge',
        description: 'Como Value Bet pero exige edge >= 15% y entrada <= 0.20. Menos senales, pagos mas grandes.',
        allowedBuckets: BUCKETS.slice(),
        minProb: 0.01,
        minEdge: 0.15,
        allowContrarian: true,
        maxEntry: 0.20,
        excludeBelow: 0,
        minAskSize: 50,
        badge: 'EDGE 15%'
    },
    late_value: {
        name: 'Late Value (T-120 a T-15)',
        description: 'Value bet solo en los ultimos 2 minutos cuando el mercado ya precio bien la ronda.',
        allowedBuckets: [120, 90, 60, 30, 15],
        minProb: 0.01,
        minEdge: 0.10,
        allowContrarian: true,
        maxEntry: 0.25,
        excludeBelow: 0,
        minAskSize: 50,
        badge: 'LATE'
    },
    value_aligned: {
        name: 'Value Aligned',
        description: 'La estrategia LIVE en produccion: entrada barata (<=0.40) solo cuando el modelo coincide con la direccion, edge >= 10%, liquidez >= 50 y sin senales en los ultimos 2 minutos. Unica walk-forward-positiva despues de fees.',
        allowedBuckets: BUCKETS.slice(),
        minProb: 0.40,
        minEdge: 0.10,
        allowContrarian: false,
        maxEntry: 0.40,
        excludeBelow: 120,
        minAskSize: 50,
        badge: 'ALIGNED'
    },
    favorite: {
        name: 'Favorito (anti-longshot)',
        description: 'Compra el lado FAVORITO del mercado (prob alta, 88-96c). Explota el sesgo favorito-longshot medido en la data: los longshots estan sobrevalorados y el favorito esta levemente barato. Edge chico pero el unico robusto.',
        allowedBuckets: BUCKETS.slice(),
        mode: 'favorite',
        minProb: 0.00,
        minEdge: -1.00,
        allowContrarian: true,
        minEntry: 0.88,
        maxEntry: 0.96,
        excludeBelow: 0,
        minAskSize: 0,
        badge: 'FAVORITE'
    },
    custom: {
        name: 'Custom Strategy',
        description: 'Ajusta buckets, probabilidad, edge, contrarian y precio maximo.',
        allowedBuckets: [480, 360, 240],
        minProb: 0.55,
        minEdge: 0.05,
        allowContrarian: false,
        maxEntry: 0.95,
        excludeBelow: 120,
        badge: 'CUSTOM'
    }
};

function initialStrategy() {
    // On a dashboard version bump, jump to the strategy actually live in prod.
    if (localStorage.getItem('dashboardSourceVersion') !== SOURCE_VERSION) return 'value_aligned';
    const saved = localStorage.getItem('selectedStrategy');
    return STRATEGIES[saved] ? saved : 'value_aligned';
}

let state = {
    selectedStrategy: initialStrategy(),
    selectedTimeRange: 'all',
    selectedAlignment: 'all',
    selectedResult: 'all',
    selectedSignalSource: initialSignalSource(),
    signalLimit: Number(localStorage.getItem('signalLimit') || 25),
    current: null,
    dashboard: null,
    chart: null,
    timer: null,
    liveSignalRefreshInFlight: false,
    decisionSignature: null,
    decisionSinceMs: Date.now()
};

loadCustomStrategy();

document.addEventListener('DOMContentLoaded', () => {
    bindControls();
    renderStrategySelector();
    updateAll();
    setInterval(updateAll, 10000);
    setInterval(refreshLiveSignalHistory, 2000);
    setInterval(() => {
        updateCountdown();
        updateDecisionAge();
        updateSignalElapsed();
    }, 1000);
    // Re-render signal rows every 5s so OPEN→RESOLVING flips without waiting for API
    setInterval(() => {
        if (state.dashboard) renderSignalsTable();
    }, 5000);
});

function bindControls() {
    document.getElementById('signal-source').addEventListener('change', (event) => {
        state.selectedSignalSource = event.target.value;
        localStorage.setItem('selectedSignalSource', state.selectedSignalSource);
        renderAll();
    });
    document.getElementById('signal-source').value = state.selectedSignalSource;
    const strategySelect = document.getElementById('strategy-source');
    strategySelect.innerHTML = Object.entries(STRATEGIES)
        .map(([key, strategy]) => `<option value="${key}">${strategy.name}</option>`)
        .join('');
    strategySelect.value = state.selectedStrategy;
    strategySelect.addEventListener('change', (event) => {
        state.selectedStrategy = event.target.value;
        localStorage.setItem('selectedStrategy', state.selectedStrategy);
        renderAll();
    });
    document.getElementById('time-range').addEventListener('change', (event) => {
        state.selectedTimeRange = event.target.value;
        renderAll();
    });
    document.getElementById('filter-alignment').addEventListener('change', (event) => {
        state.selectedAlignment = event.target.value;
        renderSignalsTable();
    });
    document.getElementById('filter-result').addEventListener('change', (event) => {
        state.selectedResult = event.target.value;
        renderSignalsTable();
    });
    document.getElementById('signal-limit').addEventListener('change', (event) => {
        state.signalLimit = Number(event.target.value);
        localStorage.setItem('signalLimit', String(state.signalLimit));
        renderSignalsTable();
    });
    document.getElementById('signal-limit').value = String(state.signalLimit);
    document.getElementById('refresh-signals').addEventListener('click', refreshDashboardNow);
}

async function updateAll() {
    try {
        const [current, dashboard] = await Promise.all([
            fetchJson('/api/predict'),
            fetchJson('/api/dashboard-data')
        ]);
        state.current = current;
        state.dashboard = dashboard;
        renderAll();
    } catch (error) {
        console.error('dashboard update failed', error);
    }
}

async function refreshLiveSignalHistory() {
    if (!state.dashboard || state.selectedSignalSource !== 'live_active' || state.liveSignalRefreshInFlight) return;
    state.liveSignalRefreshInFlight = true;
    try {
        const live = await fetchJson('/api/live-signal-history?prediction_limit=5000&signal_limit=5000');
        state.dashboard = {
            ...state.dashboard,
            generated_at: live.generated_at || state.dashboard.generated_at,
            active_model: live.active_model || state.dashboard.active_model,
            active_model_version: live.active_model_version || state.dashboard.active_model_version,
            edge_threshold: live.edge_threshold ?? state.dashboard.edge_threshold,
            active_model_forecasts: live.active_model_forecasts || state.dashboard.active_model_forecasts || [],
            actual_live_trades: live.actual_live_trades || state.dashboard.actual_live_trades || [],
            live_active_signals: live.live_active_signals || state.dashboard.live_active_signals || [],
            live_blockers_summary: live.live_blockers_summary || state.dashboard.live_blockers_summary || {}
        };
        renderHeader();
        renderStrategyPerformance();
        renderRecentPerformance();
        renderSignalSummary();
        renderSignalsTable();
        renderRoundsTable();
    } catch (error) {
        console.warn('live signal refresh failed', error);
    } finally {
        state.liveSignalRefreshInFlight = false;
    }
}

async function refreshDashboardNow() {
    const button = document.getElementById('refresh-signals');
    if (button) {
        button.disabled = true;
        button.textContent = 'Refreshing';
    }
    try {
        await updateAll();
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = 'Refresh';
        }
    }
}

async function fetchJson(url) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`${url} ${response.status}`);
    return response.json();
}

async function fetchOptionalJson(url) {
    try {
        return await fetchJson(url);
    } catch (error) {
        console.warn(`${url} unavailable`, error);
        return { candles: [] };
    }
}

function renderAll() {
    if (!state.current || !state.dashboard) return;
    renderSourceContext();
    renderHeader();
    renderCurrentRoundPanel();
    renderLiveStrategy();
    renderStrategySelector();
    renderStrategyPerformance();
    renderModelPerformance();
    renderBucketPerformance();
    renderRecentPerformance();
    renderSignalSummary();
    renderSignalsTable();
    renderRoundsTable();
}

function selectedSignals() {
    if (!state.dashboard) return [];
    if (state.selectedSignalSource === 'model_test') return modelTestSignals();
    return liveStrategyReplaySignals();
}

function strategyScopedSignals(signals = selectedSignals()) {
    return (signals || []).filter(s => !s.strategy_version || s.strategy_version === state.selectedStrategy);
}

function roundScopedSignals(signals = selectedSignals()) {
    return strategyScopedSignals(signals);
}

function activeStrategyName() {
    return STRATEGIES[state.selectedStrategy]?.name || 'Selected Strategy';
}

function modelTestSignals() {
    const predictions = state.dashboard?.model_test_predictions || [];
    return edgeBaseReplaySignals(predictions, 'test_split_edge_replay')
        .concat(replaySignalsFromForecasts(predictions, new Set())
            .map(signal => ({ ...signal, model_stage: 'test_split_replay', reason: `Test split replay: ${signal.reason}` })))
        .sort((a, b) => dateMs(b.observed_at) - dateMs(a.observed_at));
}

function liveStrategyReplaySignals() {
    // The strategy actually running in prod is represented by its REAL trades;
    // replaying it on top would double-count it, so skip it in the simulation.
    const skipKeys = new Set([EDGE_BASE_KEY, liveStrategyCardKey()]);
    return edgeBaseSignals()
        .concat(replaySignalsFromForecasts(selectedPredictions(), new Set(), skipKeys)
            .map(signal => ({ ...signal, model_stage: 'live_strategy_replay', reason: `Post-production strategy replay: ${signal.reason}` })))
        .sort((a, b) => dateMs(b.observed_at) - dateMs(a.observed_at));
}

function edgeBaseSignals() {
    // Real collector trades, mapped to the Strategy Lab card of the strategy
    // version that actually generated them (edge-v1 history stays under Edge
    // Base; value-aligned-v1 trades land on Value Aligned).
    return (state.dashboard?.actual_live_trades || state.dashboard?.live_active_signals || [])
        .map(signal => {
            const cardKey = LIVE_STRATEGY_CARD_KEYS[signal.strategy_version] || EDGE_BASE_KEY;
            return {
                ...signal,
                strategy_version: cardKey,
                strategy_name: STRATEGIES[cardKey].name,
                model_stage: signal.model_stage || 'collector_live',
                reason: signal.reason || `Collector live rule (${signal.strategy_version || 'edge-v1'})`
            };
        })
        .filter(signal => signal.strategy_version !== EDGE_BASE_KEY || edgeBaseSignalAllowed(signal));
}

function edgeBaseReplaySignals(forecasts, modelStage) {
    const rows = [];
    const stake = 1;
    const threshold = edgeThreshold();
    (forecasts || []).forEach(forecast => {
        const resolved = forecast.outcome === 'UP' || forecast.outcome === 'DOWN';
        const decision = edgeBaseDecision(forecast, threshold);
        if (!decision) return;
        const won = resolved && decision.side === forecast.outcome;
        const result = !resolved ? 'OPEN' : (won ? 'WIN' : 'LOSS');
        const pnl = resolved ? simulatedPnl(stake, decision.entry_price, won) : null;
        rows.push({
            signal_id: `edge-base-${modelStage}-${forecast.snapshot_id || `${forecast.round_cutoff}-${forecast.observed_at}`}`,
            snapshot_id: forecast.snapshot_id,
            round_id: forecast.round_id,
            round_cutoff: forecast.round_cutoff,
            observed_at: forecast.observed_at,
            seconds_to_cutoff: forecast.seconds_to_cutoff,
            seconds_bucket: forecast.seconds_bucket,
            btc_price: forecast.btc_price,
            baseline: forecast.baseline,
            baseline_source: forecast.baseline_source,
            model_version: forecast.model_version,
            model_stage: modelStage,
            strategy_version: EDGE_BASE_KEY,
            strategy_name: STRATEGIES[EDGE_BASE_KEY].name,
            action: decision.action,
            side: decision.side,
            prediction: forecast.prediction,
            prob_up: forecast.prob_up,
            prob_down: forecast.prob_down,
            confidence: forecast.confidence,
            entry_price: decision.entry_price,
            model_prob: decision.model_prob,
            edge: decision.edge,
            stake,
            result,
            pnl,
            roi: pnl !== null ? pnl / stake : null,
            actual_close: forecast.actual_close,
            outcome: forecast.outcome,
            round_status: forecast.round_status,
            close_source: forecast.close_source,
            round_resolved_at: forecast.round_resolved_at,
            reason: `Edge base: highest edge >= ${pct(threshold)}`
        });
    });
    return rows;
}

function edgeBaseDecision(row, threshold) {
    const probUp = num(row.prob_up);
    const probDown = num(row.prob_down);
    const upAsk = nullableNum(row.up_best_ask);
    const downAsk = nullableNum(row.down_best_ask);
    const candidates = [];
    if (upAsk !== null) candidates.push({ side: 'UP', model_prob: probUp, entry_price: upAsk, edge: probUp - upAsk });
    if (downAsk !== null) candidates.push({ side: 'DOWN', model_prob: probDown, entry_price: downAsk, edge: probDown - downAsk });
    candidates.sort((a, b) => b.edge - a.edge);
    for (const candidate of candidates) {
        const action = candidate.side === 'UP' ? 'BUY_UP' : 'BUY_DOWN';
        if (!edgeBaseSignalAllowed({ ...row, ...candidate, action })) continue;
        if (candidate.edge < threshold) continue;
        return {
            ...candidate,
            action
        };
    }
    return null;
}

function edgeThreshold() {
    return Number(state.dashboard?.edge_threshold ?? STRATEGIES[EDGE_BASE_KEY].minEdge ?? 0.03);
}

function edgeBaseSignalAllowed(signal) {
    const entry = nullableNum(signal.entry_price);
    const bucket = Number(signal.seconds_bucket ?? signal.seconds_to_cutoff);
    if (entry === null || entry < EDGE_BASE_MIN_ENTRY) return false;
    if (!bucketAllowedByGlobalLastAlert(bucket)) return false;
    return true;
}

function bucketAllowedByGlobalLastAlert(bucket) {
    const numericBucket = Number(bucket);
    return !Number.isFinite(numericBucket) || numericBucket >= LAST_ALLOWED_SIGNAL_BUCKET;
}

function selectedPredictions() {
    const allPredictions = state.dashboard?.predictions || [];
    const activePredictions = state.dashboard?.active_model_forecasts || allPredictions;
    const activeVersion = state.dashboard?.active_model_version;
    if (state.selectedSignalSource === 'model_test') return state.dashboard?.model_test_predictions || [];
    return activePredictions.filter(p => p.model_version === activeVersion);
}

function activeModelForecastPredictions() {
    const allPredictions = state.dashboard?.predictions || [];
    const activePredictions = state.dashboard?.active_model_forecasts || allPredictions;
    const activeVersion = state.dashboard?.active_model_version;
    return (activePredictions || []).filter(p => !activeVersion || p.model_version === activeVersion);
}

function predictionsFromSignals(signals) {
    return (signals || []).map(signal => ({
        observed_at: signal.observed_at,
        round_cutoff: signal.round_cutoff,
        seconds_to_cutoff: signal.seconds_to_cutoff,
        seconds_bucket: signal.seconds_bucket,
        baseline: signal.baseline,
        prediction: signal.prediction,
        prob_up: signal.prob_up,
        prob_down: signal.prob_down,
        confidence: signal.confidence,
        recommended_action: signal.action,
        actual_close: signal.actual_close,
        outcome: signal.outcome,
        target_up: signal.outcome === 'UP' ? 1 : signal.outcome === 'DOWN' ? 0 : null,
        model_version: signal.model_version,
        model_stage: signal.model_stage,
        round_status: signal.round_status,
        close_source: signal.close_source,
        round_resolved_at: signal.round_resolved_at
    }));
}

function strategyLabSignals() {
    const backtest = state.dashboard?.strategy_replay?.signals || state.dashboard?.backtest_active_signals || [];
    const seenSnapshots = new Set(backtest.map(s => s.snapshot_id).filter(Boolean));
    const dynamic = replaySignalsFromForecasts(
        state.dashboard?.active_model_forecasts || [],
        seenSnapshots
    );
    return backtest.concat(dynamic).sort((a, b) => dateMs(b.observed_at) - dateMs(a.observed_at));
}

function strategyLabPredictions() {
    const backtest = state.dashboard?.strategy_replay?.signals || state.dashboard?.backtest_active_signals || [];
    const bySnapshot = new Map();
    predictionsFromSignals(backtest).forEach(pred => {
        const key = `${pred.round_cutoff}-${pred.seconds_bucket}-${pred.observed_at}`;
        if (!bySnapshot.has(key)) bySnapshot.set(key, pred);
    });
    const baseKeys = new Set(backtest.map(s => s.snapshot_id).filter(Boolean));
    (state.dashboard?.active_model_forecasts || []).forEach(pred => {
        if (pred.snapshot_id && baseKeys.has(pred.snapshot_id)) return;
        const key = `${pred.round_cutoff}-${pred.seconds_bucket}-${pred.observed_at}`;
        if (!bySnapshot.has(key)) bySnapshot.set(key, pred);
    });
    return Array.from(bySnapshot.values()).sort((a, b) => dateMs(b.observed_at) - dateMs(a.observed_at));
}

function replaySignalsFromForecasts(forecasts, seenSnapshots, skipKeys = new Set([EDGE_BASE_KEY])) {
    const rows = [];
    const stake = 1;
    (forecasts || []).forEach(forecast => {
        if (!forecast.snapshot_id || seenSnapshots.has(forecast.snapshot_id)) return;
        const resolved = forecast.outcome === 'UP' || forecast.outcome === 'DOWN';
        Object.entries(STRATEGIES).forEach(([key, strategy]) => {
            if (skipKeys.has(key)) return;
            const decision = replayDecision(forecast, strategy);
            if (!decision) return;
            const won = resolved && decision.side === forecast.outcome;
            const result = !resolved ? 'OPEN' : (won ? 'WIN' : 'LOSS');
            const pnl = resolved ? simulatedPnl(stake, decision.entry_price, won) : null;
            rows.push({
                signal_id: `dynamic-replay-${key}-${forecast.snapshot_id}`,
                snapshot_id: forecast.snapshot_id,
                round_id: forecast.round_id,
                round_cutoff: forecast.round_cutoff,
                observed_at: forecast.observed_at,
                seconds_to_cutoff: forecast.seconds_to_cutoff,
                seconds_bucket: forecast.seconds_bucket,
                btc_price: forecast.btc_price,
                baseline: forecast.baseline,
                baseline_source: forecast.baseline_source,
                model_version: forecast.model_version,
                model_stage: 'dynamic_replay',
                strategy_version: key,
                action: decision.action,
                side: decision.side,
                prediction: forecast.prediction,
                prob_up: forecast.prob_up,
                prob_down: forecast.prob_down,
                confidence: forecast.confidence,
                entry_price: decision.entry_price,
                model_prob: decision.model_prob,
                edge: decision.edge,
                stake,
                result,
                pnl,
                roi: pnl !== null ? pnl / stake : null,
                actual_close: forecast.actual_close,
                outcome: forecast.outcome,
                round_status: forecast.round_status,
                close_source: forecast.close_source,
                round_resolved_at: forecast.round_resolved_at,
                reason: `Dynamic replay: ${decision.reason}`
            });
        });
    });
    return rows;
}

function replayDecision(row, strategy) {
    const candidates = [];
    const probUp = num(row.prob_up);
    const probDown = num(row.prob_down);
    const upAsk = nullableNum(row.up_best_ask);
    const downAsk = nullableNum(row.down_best_ask);
    const upAskSize = nullableNum(row.up_ask_size);
    const downAskSize = nullableNum(row.down_ask_size);
    const prediction = row.prediction;
    const minAskSize = strategy.minAskSize || 0;
    if (strategy.mode === 'favorite') {
        const favs = [];
        if (upAsk !== null) favs.push({ side: 'UP', model_prob: probUp, entry_price: upAsk, edge: probUp - upAsk, ask_size: upAskSize });
        if (downAsk !== null) favs.push({ side: 'DOWN', model_prob: probDown, entry_price: downAsk, edge: probDown - downAsk, ask_size: downAskSize });
        favs.sort((a, b) => b.entry_price - a.entry_price); // favorite = highest market ask
        const fav = favs[0];
        if (!fav) return null;
        const bucket = Number(row.seconds_bucket);
        const minEntry = strategy.minEntry ?? 0.85;
        if (!bucketAllowedByGlobalLastAlert(bucket)) return null;
        if (!strategy.allowedBuckets.includes(bucket)) return null;
        if (bucket <= strategy.excludeBelow) return null;
        if (fav.entry_price < minEntry || fav.entry_price > strategy.maxEntry) return null;
        if (minAskSize > 0 && (fav.ask_size === null || fav.ask_size < minAskSize)) return null;
        return {
            action: fav.side === 'UP' ? 'BUY_UP' : 'BUY_DOWN',
            side: fav.side,
            model_prob: fav.model_prob,
            entry_price: fav.entry_price,
            edge: fav.edge,
            ask_size: fav.ask_size,
            reason: `${strategy.name}, T-${bucket}, fav ${pct(fav.entry_price)}`
        };
    }
    if (strategy.allowContrarian || prediction === 'UP') {
        if (upAsk !== null) candidates.push({ side: 'UP', model_prob: probUp, entry_price: upAsk, edge: probUp - upAsk, ask_size: upAskSize });
    }
    if (strategy.allowContrarian || prediction === 'DOWN') {
        if (downAsk !== null) candidates.push({ side: 'DOWN', model_prob: probDown, entry_price: downAsk, edge: probDown - downAsk, ask_size: downAskSize });
    }
    candidates.sort((a, b) => b.edge - a.edge);
    for (const candidate of candidates) {
        const action = candidate.side === 'UP' ? 'BUY_UP' : 'BUY_DOWN';
        const alignment = classifySignalAlignment(prediction, action);
        const bucket = Number(row.seconds_bucket);
        if (!bucketAllowedByGlobalLastAlert(bucket)) continue;
        if (!strategy.allowedBuckets.includes(bucket)) continue;
        if (bucket <= strategy.excludeBelow) continue;
        if (candidate.model_prob < strategy.minProb) continue;
        if (candidate.edge < strategy.minEdge) continue;
        if (candidate.entry_price > strategy.maxEntry) continue;
        if (!strategy.allowContrarian && alignment === 'CONTRARIAN') continue;
        if (minAskSize > 0 && (candidate.ask_size === null || candidate.ask_size < minAskSize)) continue;
        return {
            action,
            side: candidate.side,
            model_prob: candidate.model_prob,
            entry_price: candidate.entry_price,
            edge: candidate.edge,
            ask_size: candidate.ask_size,
            reason: `${strategy.name}, T-${bucket}, edge ${pct(candidate.edge)}`
        };
    }
    return null;
}

function sourceLabel() {
    if (state.selectedSignalSource === 'model_test') return testSplitLabel();
    if (state.selectedSignalSource === 'live_active') return 'Actual Live Paper PnL';
    return testSplitLabel();
}

function testSplitLabel() {
    const testSize = state.dashboard?.training_metrics?.market?.dataset?.test_size;
    if (testSize) return `Model Test Split ${Math.round(Number(testSize) * 100)}%`;
    return 'Model Test Split';
}

function sourceCopy() {
    const active = state.dashboard?.active_model_version || 'active model';
    const liveClosed = resolvedSignals(state.dashboard?.actual_live_trades || state.dashboard?.live_active_signals || []).length;
    const forecasts = (state.dashboard?.active_model_forecasts || []).length;
    const resolvedForecasts = (state.dashboard?.active_model_forecasts || []).filter(p => p.outcome === 'UP' || p.outcome === 'DOWN').length;
    const testPredictions = (state.dashboard?.model_test_predictions || []).length;
    if (state.selectedSignalSource === 'model_test') {
        return `${testSplitLabel()}: ${testPredictions} fixed snapshots for ${active}. All strategy signals stop at T-${LAST_ALLOWED_SIGNAL_BUCKET}; later buckets are excluded.`;
    }
    return `Post-production live: ${forecasts} forecasts, ${resolvedForecasts} resolved. All strategy signals stop at T-${LAST_ALLOWED_SIGNAL_BUCKET}; later buckets are excluded.`;
}

function renderSourceContext() {
    const label = sourceLabel();
    const copy = sourceCopy();
    const testOption = document.querySelector('#signal-source option[value="model_test"]');
    if (testOption) testOption.textContent = testSplitLabel();
    const alignmentFilter = document.getElementById('filter-alignment');
    if (alignmentFilter) {
        alignmentFilter.disabled = state.selectedSignalSource === 'model_test';
        if (state.selectedSignalSource === 'model_test') {
            state.selectedAlignment = 'all';
            alignmentFilter.value = 'all';
        }
    }
    const resultFilter = document.getElementById('filter-result');
    if (resultFilter && state.selectedSignalSource === 'model_test' && state.selectedResult === 'OPEN') {
        state.selectedResult = 'all';
        resultFilter.value = 'all';
    }
    setText('strategy-source-title', `${label} by decision logic`);
    setText('strategy-source-copy', copy);
    setText('drift-source-title', `${label} drift`);
    setText('bucket-source-title', `${label}: bucket performance`);
    setText('bucket-source-copy', copy);
    setText('signals-source-title', `${label}: ${activeStrategyName()} signals`);
}

function renderHeader() {
    const current = state.current;
    const signals = resolvedSignals(filteredSignalsByRange(strategyScopedSignals(), 'all'));
    const pnl = sum(signals.map(s => num(s.pnl)));
    const wins = signals.filter(s => s.result === 'WIN').length;
    const winRate = signals.length ? wins / signals.length : null;

    setText('hdr-btc', money(current.current_price));
    setText('hdr-baseline', money(current.window_open));
    setText('hdr-bucket', `T-${currentBucket(current)}`);
    const strategySelect = document.getElementById('strategy-source');
    if (strategySelect && strategySelect.value !== state.selectedStrategy) {
        strategySelect.value = state.selectedStrategy;
    }
    setText('hdr-pnl', signedMoney(pnl));
    setClassByValue('hdr-pnl', pnl);
    setText('hdr-winrate', pct(winRate));
    setText('hdr-signals', signals.length);
    setText('hdr-updated', new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
    updateCountdown();
}

function renderLiveStrategy() {
    const ls = state.dashboard?.live_strategy;
    const badge = document.getElementById('strategy-badge');
    if (badge && ls) {
        badge.textContent = ls.strategy_version || '---';
        badge.className = 'badge good';
    }
    const list = document.getElementById('strategy-gates');
    if (list && ls) {
        const gates = [
            `Edge minimo ${pct(ls.edge_threshold)}`,
            `Entrada maxima ${fmtPrice(ls.max_entry_price)}`,
            ls.require_aligned ? 'Solo el lado alineado con el modelo' : 'Permite lado contrarian',
            ls.exclude_below_seconds > 0 ? `Sin senales despues de T-${ls.exclude_below_seconds}s` : 'Opera hasta el cierre',
            ls.min_ask_size > 0 ? `Liquidez ask >= ${ls.min_ask_size}` : 'Sin filtro de liquidez',
        ];
        list.innerHTML = '';
        gates.forEach((gate) => {
            const li = document.createElement('li');
            li.textContent = gate;
            list.appendChild(li);
        });
    }
    const tick = state.dashboard?.tick_capture;
    const line = document.getElementById('tick-capture-line');
    if (line) {
        if (tick && tick.active) {
            line.textContent = `ON (${tick.files_today} archivos hoy)`;
            line.style.color = 'var(--good, #2ecc71)';
        } else {
            line.textContent = tick ? 'OFF' : '---';
            line.style.color = '';
        }
    }
}

function updateCountdown() {
    if (!state.current || !state.current.next_cutoff) return;
    const diff = Math.max(0, state.current.next_cutoff - Math.floor(Date.now() / 1000));
    const minutes = Math.floor(diff / 60).toString().padStart(2, '0');
    const seconds = (diff % 60).toString().padStart(2, '0');
    setText('hdr-countdown', `${minutes}:${seconds}`);
}

function renderCurrentRoundPanel() {
    const c = state.current;
    const strategy = STRATEGIES[state.selectedStrategy];
    const decision = evaluateStrategy(currentSnapshotFromPrediction(c), strategy);
    const alignment = decision.alignment;
    const bucket = currentBucket(c);
    const dist = num(c.current_price) - num(c.window_open);
    const distPct = c.window_open ? dist / num(c.window_open) : null;
    const quote = c.quote_summary || {};

    setText('decision-title', decision.decision.replace('_', ' '));
    setClassByDecision('decision-title', decision.decision);
    setText('hdr-decision', decision.decision.replace('_', ' '));
    setClassByDecision('hdr-decision', decision.decision);
    updateDecisionSignature(decision, c);
    setText('decision-reason', decision.reason);
    setText('bucket-badge', bucket ? `T-${bucket}` : 'T---');
    setText('alignment-badge', alignment);
    setBadgeClass('alignment-badge', alignment);
    setText('baseline-badge', c.baseline_exact === false ? (c.baseline_action_allowed ? 'PROXY ALLOWED' : 'BASELINE PROXY') : 'BASELINE EXACT');
    document.getElementById('baseline-badge').className = `badge ${c.baseline_exact === false ? 'watch' : 'good'}`;

    setText('baseline-value', money(c.window_open));
    setText('btc-value', money(c.current_price));
    setText('baseline-distance', `${signedMoney(dist)} (${pct(distPct)})`);
    setClassByValue('baseline-distance', dist);
    setText('baseline-copy', dist >= 0 ? 'BTC is currently above the round baseline.' : 'BTC is currently below the round baseline.');

    setText('prediction-value', c.prediction || '---');
    setClassByPrediction('prediction-value', c.prediction);
    setText('prob-up', pct(c.prob_up));
    setText('prob-down', pct(c.prob_down));
    setText('confidence-value', pct(c.confidence));
    setText('prediction-copy', `The model currently thinks ${c.prediction || '---'} is more likely.`);

    setText('market-main', `UP ${fmtPrice(c.up_ask)} / DOWN ${fmtPrice(c.down_ask)}`);
    setText('up-spread', fmtPrice(quote.up_spread));
    setText('down-spread', fmtPrice(quote.down_spread));
    setText('market-copy', `Polymarket is pricing UP at ${fmtPrice(c.up_ask)} and DOWN at ${fmtPrice(c.down_ask)}.`);

    const bestEdge = bestEdgeSide(c);
    setText('edge-main', bestEdge ? `${bestEdge.side} ${pct(bestEdge.edge)}` : 'NO EDGE');
    setClassByValue('edge-main', bestEdge ? bestEdge.edge : 0);
    setText('edge-up', pct(c.edge_up));
    setText('edge-down', pct(c.edge_down));
    setText('edge-copy', edgeCopy(c, decision));

    renderRuleList('passed-rules', decision.passedRules);
    renderRuleList('failed-rules', decision.failedRules);
    setText('alignment-line', `Prediction ${c.prediction || '---'} / Action ${(c.raw_recommended_action || c.recommended_action || 'WAIT').replace('_', ' ')} / ${alignment}`);
    setClassByAlignment('alignment-line', alignment);
}

function updateDecisionSignature(decision, current) {
    const action = decision.decision || 'WAIT';
    const rawAction = current?.raw_recommended_action || current?.recommended_action || 'WAIT';
    const signature = [
        state.selectedStrategy,
        current?.next_cutoff || '',
        action,
        rawAction,
        decision.alignment || ''
    ].join('|');
    if (signature !== state.decisionSignature) {
        state.decisionSignature = signature;
        state.decisionSinceMs = Date.now();
    }
    updateDecisionAge();
}

function updateDecisionAge() {
    const elapsed = Math.max(0, Date.now() - (state.decisionSinceMs || Date.now()));
    const value = formatDuration(elapsed);
    setText('decision-age', value);
    setText('hdr-decision-age', value);
}

function currentSnapshotFromPrediction(c) {
    const action = c.raw_recommended_action || c.recommended_action || 'WAIT';
    const side = action === 'BUY_UP' ? 'UP' : action === 'BUY_DOWN' ? 'DOWN' : null;
    const entry = side === 'UP' ? c.up_ask : side === 'DOWN' ? c.down_ask : null;
    const modelProb = side === 'UP' ? c.prob_up : side === 'DOWN' ? c.prob_down : null;
    const edge = side === 'UP' ? c.edge_up : side === 'DOWN' ? c.edge_down : null;
    return {
        prediction: c.prediction,
        action,
        side,
        entry_price: entry,
        model_prob: modelProb,
        edge,
        baseline_exact: c.baseline_exact,
        baseline_action_allowed: c.baseline_action_allowed,
        seconds_bucket: currentBucket(c),
        seconds_to_cutoff: currentSecondsToCutoff(c)
    };
}

function currentBucket(c) {
    if (Number.isFinite(Number(c.seconds_bucket))) return Number(c.seconds_bucket);
    const seconds = currentSecondsToCutoff(c);
    return BUCKETS.slice().sort((a, b) => a - b).find(bucket => seconds <= bucket) || 895;
}

function currentSecondsToCutoff(c) {
    if (Number.isFinite(Number(c.seconds_to_cutoff))) return Math.max(0, Number(c.seconds_to_cutoff));
    if (!c.next_cutoff) return null;
    return Math.max(0, c.next_cutoff - Math.floor(Date.now() / 1000));
}

function classifySignalAlignment(prediction, action) {
    if (!action || action === 'NO_SIGNAL') return 'NO_SIGNAL';
    if (action === 'WAIT' || action === 'SKIP') return 'WAIT';
    if (action === 'BUY_UP' && prediction === 'UP') return 'ALIGNED';
    if (action === 'BUY_DOWN' && prediction === 'DOWN') return 'ALIGNED';
    if (action === 'BUY_UP' && prediction === 'DOWN') return 'CONTRARIAN';
    if (action === 'BUY_DOWN' && prediction === 'UP') return 'CONTRARIAN';
    return 'NO_SIGNAL';
}

function evaluateStrategy(snapshot, rules) {
    const action = snapshot.action || 'WAIT';
    const alignment = classifySignalAlignment(snapshot.prediction, action);
    const passed = [];
    const failed = [];

    if (action === 'WAIT' || !snapshot.side) {
        return { decision: 'WAIT', reason: 'WAIT: no raw signal with positive edge right now.', alignment, passedRules: [], failedRules: ['no BUY_UP or BUY_DOWN signal'] };
    }

    const bucket = Number(snapshot.seconds_bucket);
    if (bucketAllowedByGlobalLastAlert(bucket)) passed.push(`last alert guardrail: T-${LAST_ALLOWED_SIGNAL_BUCKET} or earlier`);
    else failed.push(`bucket T-${bucket} is after T-${LAST_ALLOWED_SIGNAL_BUCKET}`);

    if (snapshot.baseline_exact === false && !snapshot.baseline_action_allowed) failed.push('exact Polymarket baseline not available yet');
    else passed.push(snapshot.baseline_exact === false ? 'price-feed baseline proxy allowed' : 'baseline exact or historical signal');

    if (rules.allowedBuckets.includes(bucket)) passed.push(`bucket T-${bucket} allowed`);
    else failed.push(`bucket T-${bucket} outside selected strategy`);

    if (bucket > rules.excludeBelow) passed.push(`not inside last ${rules.excludeBelow}s`);
    else failed.push(`bucket T-${bucket} excluded by last-minute filter`);

    if (num(snapshot.model_prob) >= rules.minProb) passed.push(`model probability >= ${pct(rules.minProb)}`);
    else failed.push(`model probability below ${pct(rules.minProb)}`);

    if (num(snapshot.edge) >= rules.minEdge) passed.push(`edge >= ${pct(rules.minEdge)}`);
    else failed.push(`edge below ${pct(rules.minEdge)}`);

    if (snapshot.entry_price === null || snapshot.entry_price === undefined || num(snapshot.entry_price) <= rules.maxEntry) passed.push(`entry price <= ${fmtPrice(rules.maxEntry)}`);
    else failed.push(`entry price above ${fmtPrice(rules.maxEntry)}`);

    if (rules.allowContrarian || alignment !== 'CONTRARIAN') passed.push(rules.allowContrarian ? 'contrarian signals allowed' : 'signal aligns with model');
    else failed.push('signal must align with prediction');

    if (failed.length) {
        const reason = failed.includes('signal must align with prediction')
            ? `SKIP: Contrarian signal not allowed by ${rules.name}.`
            : `SKIP: ${failed[0]}.`;
        return { decision: 'SKIP', reason, alignment, passedRules: passed, failedRules: failed };
    }

    return {
        decision: action,
        reason: `${action}: prediction ${snapshot.prediction}, probability sufficient, edge positive.`,
        alignment,
        passedRules: passed,
        failedRules: []
    };
}

function renderStrategySelector() {
    const root = document.getElementById('strategy-selector');
    root.innerHTML = '';
    Object.entries(STRATEGIES).forEach(([key, strategy]) => {
        const perf = calculateStrategyPerformance(key, strategy, selectedSignals());
        const card = document.createElement('article');
        card.className = `strategy-card ${state.selectedStrategy === key ? 'active' : ''}`;
        const winRateDisplay = perf.total_signals >= 3 ? pct(perf.wins / perf.total_signals) : '---';
        const sampleBadge = perf.total_signals < 10 ? `<span class="badge low-sample">LOW SAMPLE</span>` : '';
        const activeBadge = key === liveStrategyCardKey()
            ? `<span class="badge active-model" title="Estrategia que el collector opera en produccion">LIVE EN PROD</span>`
            : '';
        card.innerHTML = `
            <h3>${strategy.name} ${activeBadge} ${sampleBadge}</h3>
            <p>${strategy.description}</p>
            <div class="rules">
                <span class="badge">${strategy.badge}</span>
                <span class="badge">edge ${pct(key === EDGE_BASE_KEY ? edgeThreshold() : strategy.minEdge)}</span>
                ${key === EDGE_BASE_KEY ? `<span class="badge">min entry ${fmtPrice(EDGE_BASE_MIN_ENTRY)}</span>` : ''}
                <span class="badge">T-${LAST_ALLOWED_SIGNAL_BUCKET}</span>
                <span class="badge">${strategy.allowContrarian ? 'contrarian ok' : 'aligned'}</span>
            </div>
            <div class="card-pnl">
                <span class="card-pnl-label">${perf.total_signals} signals · ${winRateDisplay} win</span>
                <span class="${perf.total_pnl >= 0 ? 'up' : 'down'}">${signedMoney(perf.total_pnl)}</span>
            </div>
        `;
        card.addEventListener('click', () => {
            state.selectedStrategy = key;
            localStorage.setItem('selectedStrategy', key);
            renderAll();
        });
        root.appendChild(card);
    });
    renderCustomStrategy();
}

function renderCustomStrategy() {
    const root = document.getElementById('custom-strategy');
    const active = state.selectedStrategy === 'custom';
    root.className = `custom-strategy ${active ? 'active' : ''}`;
    if (!active) {
        root.innerHTML = '';
        return;
    }
    const s = STRATEGIES.custom;
    root.innerHTML = `
        ${field('Buckets', 'custom-buckets', s.allowedBuckets.join(','))}
        ${field('Min prob', 'custom-prob', s.minProb)}
        ${field('Min edge', 'custom-edge', s.minEdge)}
        ${field('Max entry', 'custom-entry', s.maxEntry)}
        ${field('Exclude <= sec', 'custom-exclude', s.excludeBelow)}
        <label class="field"><span class="label">Contrarian</span><select id="custom-contrarian"><option value="false">No</option><option value="true">Yes</option></select></label>
    `;
    document.getElementById('custom-contrarian').value = String(s.allowContrarian);
    ['custom-buckets', 'custom-prob', 'custom-edge', 'custom-entry', 'custom-exclude', 'custom-contrarian'].forEach(id => {
        document.getElementById(id).addEventListener('change', updateCustomStrategy);
    });
}

function field(label, id, value) {
    return `<label class="field"><span class="label">${label}</span><input id="${id}" value="${value}"></label>`;
}

function updateCustomStrategy() {
    STRATEGIES.custom.allowedBuckets = document.getElementById('custom-buckets').value.split(',').map(x => Number(x.trim())).filter(Boolean);
    STRATEGIES.custom.minProb = Number(document.getElementById('custom-prob').value);
    STRATEGIES.custom.minEdge = Number(document.getElementById('custom-edge').value);
    STRATEGIES.custom.maxEntry = Number(document.getElementById('custom-entry').value);
    STRATEGIES.custom.excludeBelow = Number(document.getElementById('custom-exclude').value);
    STRATEGIES.custom.allowContrarian = document.getElementById('custom-contrarian').value === 'true';
    localStorage.setItem('customStrategy', JSON.stringify({
        allowedBuckets: STRATEGIES.custom.allowedBuckets,
        minProb: STRATEGIES.custom.minProb,
        minEdge: STRATEGIES.custom.minEdge,
        maxEntry: STRATEGIES.custom.maxEntry,
        excludeBelow: STRATEGIES.custom.excludeBelow,
        allowContrarian: STRATEGIES.custom.allowContrarian
    }));
    renderAll();
}

function loadCustomStrategy() {
    try {
        const saved = JSON.parse(localStorage.getItem('customStrategy') || 'null');
        if (!saved) return;
        Object.assign(STRATEGIES.custom, saved);
    } catch (error) {
        console.warn('custom strategy load failed', error);
    }
}

function calculateStrategyPerformance(strategyKey, strategy, signals) {
    const scoped = resolvedSignals(filteredSignalsByRange(signals, state.selectedTimeRange));
    const rows = scoped.some(s => s.strategy_version)
        ? scoped.filter(s => s.strategy_version === strategyKey)
        : scoped.filter(s => strategyAllowsSignal(s, strategy));
    const wins = rows.filter(s => s.result === 'WIN');
    const losses = rows.filter(s => s.result === 'LOSS');
    const pnlValues = rows.map(s => num(s.pnl));
    const grossWon = sum(pnlValues.filter(value => value > 0));
    const grossLost = sum(pnlValues.filter(value => value < 0));
    const pnl = grossWon + grossLost;
    const roiRows = rows.filter(s => s.roi !== null && s.roi !== undefined);
    const byBucket = groupBy(rows, s => s.seconds_bucket);
    const bucketStats = Object.entries(byBucket).map(([bucket, values]) => ({ bucket, pnl: sum(values.map(v => num(v.pnl))) }));
    bucketStats.sort((a, b) => b.pnl - a.pnl);
    const aligned = rows.filter(s => classifySignalAlignment(s.prediction, s.action) === 'ALIGNED');
    const contrarian = rows.filter(s => classifySignalAlignment(s.prediction, s.action) === 'CONTRARIAN');
    return {
        total_signals: rows.length,
        wins: wins.length,
        losses: losses.length,
        win_rate: rows.length ? wins.length / rows.length : null,
        gross_won: grossWon,
        gross_lost: grossLost,
        total_pnl: pnl,
        avg_pnl: rows.length ? pnl / rows.length : null,
        avg_roi: roiRows.length ? avg(roiRows.map(s => num(s.roi))) : null,
        best_bucket: bucketStats[0] ? `T-${bucketStats[0].bucket}` : '---',
        worst_bucket: bucketStats[bucketStats.length - 1] ? `T-${bucketStats[bucketStats.length - 1].bucket}` : '---',
        aligned_signals: aligned.length,
        contrarian_signals: contrarian.length,
        aligned_pnl: sum(aligned.map(s => num(s.pnl))),
        contrarian_pnl: sum(contrarian.map(s => num(s.pnl))),
        recent_pnl: sum(resolvedSignals(filteredSignalsByRange(signals, '10r')).filter(s => (
            s.strategy_version ? s.strategy_version === strategyKey : strategyAllowsSignal(s, strategy)
        )).map(s => num(s.pnl)))
    };
}

function strategyAllowsSignal(signal, strategy) {
    const decision = evaluateStrategy(signal, strategy);
    return decision.decision === signal.action;
}

function renderStrategyPerformance() {
    const tbody = document.getElementById('strategy-performance-body');
    tbody.innerHTML = '';
    Object.entries(STRATEGIES).forEach(([key, strategy]) => {
        const perf = calculateStrategyPerformance(key, strategy, selectedSignals());
        const sampleBadge = perf.total_signals < 10 ? '<span class="badge low-sample">LOW SAMPLE</span>' : '';
        const alignmentQuality = perf.total_signals ? `${Math.round(perf.aligned_signals / perf.total_signals * 100)}% aligned` : '---';
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${strategy.name} ${key === state.selectedStrategy ? '<span class="badge active-model">ACTIVE</span>' : ''} ${sampleBadge}</td>
            <td>${perf.total_signals}</td>
            <td>${pct(perf.win_rate)}</td>
            <td class="up">${signedMoney(perf.gross_won)}</td>
            <td class="down">${signedMoney(perf.gross_lost)}</td>
            <td class="${perf.total_pnl >= 0 ? 'up' : 'down'}">${signedMoney(perf.total_pnl)}</td>
            <td>${pct(perf.avg_roi)}</td>
            <td>${perf.best_bucket}</td>
            <td class="${perf.recent_pnl >= 0 ? 'up' : 'down'}">${signedMoney(perf.recent_pnl)}</td>
            <td>${alignmentQuality}</td>
        `;
        tbody.appendChild(tr);
    });
}

function calculateModelPerformance(predictions, signals, modelVersion) {
    const scopedPredictions = modelVersion
        ? predictions.filter(p => p.model_version === modelVersion)
        : predictions;
    const scopedSignals = modelVersion
        ? signals.filter(s => !s.model_version || s.model_version === modelVersion)
        : signals;
    const scopedResolved = scopedPredictions.filter(p => p.outcome === 'UP' || p.outcome === 'DOWN');
    const sourcePredictions = modelVersion ? scopedPredictions : predictions;
    const sourceSignals = modelVersion ? scopedSignals : signals;
    const resolved = sourcePredictions.filter(p => p.outcome === 'UP' || p.outcome === 'DOWN');
    const correct = resolved.filter(p => p.prediction === p.outcome);
    const recent = filteredPredictionsByRange(resolved, state.selectedTimeRange);
    const recentCorrect = recent.filter(p => p.prediction === p.outcome);
    const byBucket = Object.entries(groupBy(resolved, p => p.seconds_bucket)).map(([bucket, rows]) => {
        const bucketCorrect = rows.filter(p => p.prediction === p.outcome);
        const edgeValues = rows.map(r => {
            const eu = nullableNum(r.edge_up);
            const ed = nullableNum(r.edge_down);
            if (eu === null && ed === null) return null;
            return Math.max(eu ?? -1, ed ?? -1);
        }).filter(v => v !== null);
        return {
            bucket: Number(bucket),
            predictions: rows.length,
            correct: bucketCorrect.length,
            accuracy: rows.length ? bucketCorrect.length / rows.length : null,
            avg_confidence: avg(rows.map(r => num(r.confidence))),
            avg_edge: edgeValues.length ? avg(edgeValues) : null,
        };
    }).sort((a, b) => b.bucket - a.bucket);
    return {
        total_predictions: resolved.length,
        correct_predictions: correct.length,
        accuracy: resolved.length ? correct.length / resolved.length : null,
        recent_accuracy: recent.length ? recentCorrect.length / recent.length : null,
        avg_confidence: avg(resolved.map(r => num(r.confidence))),
        byBucket,
        scoped_to_model: modelVersion || null,
        scoped_resolved_count: scopedResolved.length,
        used_all_predictions_fallback: false
    };
}

function renderModelPerformance() {
    const activeVersion = state.dashboard.active_model_version || state.current?.model_version || state.dashboard.active_model;
    const activeForecasts = state.dashboard.active_model_forecasts || [];
    const actualLiveTrades = state.dashboard.actual_live_trades || state.dashboard.live_active_signals || [];
    const isModelTest = state.selectedSignalSource === 'model_test';
    const performancePredictions = state.selectedSignalSource === 'model_test' ? selectedPredictions() : activeForecasts;
    const performanceSignals = state.selectedSignalSource === 'model_test' ? [] : actualLiveTrades;
    const perf = calculateModelPerformance(performancePredictions, performanceSignals, activeVersion);
    const marketMetrics = state.dashboard.training_metrics?.market || {};
    const marketDataset = marketMetrics.dataset || {};
    const trainingRows = marketDataset.rows || 0;
    const trainingRounds = marketDataset.unique_rounds || 0;
    const testAccuracy = marketMetrics.accuracy ?? null;
    const blocker = state.dashboard.live_blockers_summary || {};
    const waitCount = blocker.actions?.WAIT || 0;
    const buyCount = (blocker.actions?.BUY_UP || 0) + (blocker.actions?.BUY_DOWN || 0);
    const liveClosed = resolvedSignals(actualLiveTrades);
    // Recalculate at stake=$1 + 2% fee so this matches the Strategy Lab display
    const livePnl = sum(liveClosed.map(s => simulatedPnl(1, s.entry_price, s.result === 'WIN')));

    setText('model-summary-title', activeVersion || '---');
    setText('model-total-preds', trainingRows ? `${trainingRounds} rounds / ${trainingRows} rows` : '---');
    setText('model-accuracy', pct(testAccuracy));
    setText('model-live-resolved', perf.scoped_resolved_count || 0);
    setText('model-recent-accuracy', perf.scoped_resolved_count ? pct(perf.recent_accuracy) : 'pending');

    setText('model-copy', `Training used ${trainingRounds} resolved rounds and ${trainingRows} snapshots. Test accuracy is ${pct(testAccuracy)}.`);

    if (isModelTest) {
        setText('live-forecast-title', `${performancePredictions.length} test snapshots`);
        setText('live-buy-signals', '---');
        setText('live-wait-decisions', '---');
        setText('live-baseline-exact', '---');
        setText('live-paper-pnl', '---');
        document.getElementById('live-paper-pnl').className = '';
        setText('live-forecast-copy', 'This view uses only the held-out temporal test split from training. It is not live post-production performance.');
        setText('live-blocker-title', 'test only');
        setText('live-blocker-copy', 'No paper PnL is calculated here because this view is for model prediction accuracy, not executed signals.');
    } else {
        setText('live-forecast-title', `${activeForecasts.length} forecasts`);
        setText('live-buy-signals', buyCount);
        setText('live-wait-decisions', waitCount);
        setText('live-baseline-exact', pct(blocker.baseline_exact_rate));
        setText('live-paper-pnl', signedMoney(livePnl));
        setClassByValue('live-paper-pnl', livePnl);
        setText('live-forecast-copy', perf.scoped_resolved_count
            ? `Forecast accuracy is based on resolved predictions from this exact model version.`
            : 'Forecast accuracy is pending for this active model.');

        const topBlocker = blocker.top_blocker || 'none';
        const blockerText = topBlocker === 'baseline_proxy'
            ? 'Most WAIT decisions are blocked because the baseline is still proxy, so the collector refuses live BUY signals.'
            : topBlocker === 'edge_below_threshold'
                ? 'Most WAIT decisions do not have enough edge to become BUY signals.'
                : topBlocker === 'missing_quotes'
                    ? 'Most WAIT decisions are missing usable Polymarket quotes.'
                    : topBlocker === 'buy_signal'
                        ? 'The active model is producing BUY signals.'
                        : 'No dominant blocker detected yet.';
        setText('live-blocker-title', topBlocker.replaceAll('_', ' '));
        setText('live-blocker-copy', `${blocker.resolved_forecasts || 0} resolved forecasts, ${liveClosed.length} closed live trades. ${blockerText}`);
    }

    const tbody = document.getElementById('model-bucket-body');
    tbody.innerHTML = '';
    if (!perf.byBucket.length) {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td colspan="7">No resolved rows yet for ${activeVersion} in the selected source. This will update as rounds resolve.</td>
        `;
        tbody.appendChild(tr);
    }
    perf.byBucket.forEach(row => {
        const acc = row.accuracy;
        const edge = row.avg_edge;
        const accClass = acc === null ? '' : acc >= 0.70 ? 'up' : acc >= 0.58 ? 'warn' : 'down';
        const edgeClass = edge === null ? '' : edge > 0.08 ? 'up' : edge > 0 ? 'warn' : 'down';
        const comment = isModelTest
            ? acc >= 0.70 ? 'Strong model signal' : acc >= 0.58 ? 'Moderate accuracy' : 'Below baseline — monitor'
            : acc >= 0.70 && edge > 0.08 ? 'Strong model + edge'
            : acc >= 0.70 ? 'High accuracy, low market edge'
            : acc >= 0.58 ? 'Moderate — validate with edge'
            : 'Below baseline — use with caution';
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td><span class="badge">${isModelTest ? 'TEST' : 'LIVE'}</span> T-${row.bucket}</td>
            <td>${row.predictions}</td>
            <td>${row.correct}</td>
            <td class="${accClass}">${pct(row.accuracy)}</td>
            <td>${pct(row.avg_confidence)}</td>
            <td class="${edgeClass}">${edge === null ? '---' : signedPct(edge)}</td>
            <td class="muted">${comment}</td>
        `;
        tbody.appendChild(tr);
    });
}

function calculateBucketPerformance(predictions, signals) {
    const resolvedPreds = predictions.filter(p => p.outcome === 'UP' || p.outcome === 'DOWN');
    const resolvedSigs = resolvedSignals(signals);
    return BUCKETS.map(bucket => {
        const preds = resolvedPreds.filter(p => Number(p.seconds_bucket) === bucket);
        const sigs = resolvedSigs.filter(s => Number(s.seconds_bucket) === bucket);
        const correct = preds.filter(p => p.prediction === p.outcome).length;
        const wins = sigs.filter(s => s.result === 'WIN').length;
        const losses = sigs.filter(s => s.result === 'LOSS').length;
        const pnl = sum(sigs.map(s => num(s.pnl)));
        const aligned = sigs.filter(s => classifySignalAlignment(s.prediction, s.action) === 'ALIGNED').length;
        const recommendation = bucketRecommendation(sigs.length, pnl, wins, losses);
        return {
            bucket,
            predictions: preds.length,
            correct,
            accuracy: preds.length ? correct / preds.length : null,
            signals: sigs.length,
            wins,
            losses,
            win_rate: sigs.length ? wins / sigs.length : null,
            total_pnl: pnl,
            avg_roi: sigs.length ? avg(sigs.map(s => num(s.roi))) : null,
            avg_entry: sigs.length ? avg(sigs.map(s => num(s.entry_price))) : null,
            avg_edge: sigs.length ? avg(sigs.map(s => num(s.edge))) : null,
            aligned_pct: sigs.length ? aligned / sigs.length : null,
            recommendation
        };
    });
}

function bucketRecommendation(signals, pnl, wins, losses) {
    if (signals < 5) return 'LOW_SAMPLE';
    if (pnl > 0 && wins >= losses) return 'GOOD';
    if (pnl < 0) return 'AVOID';
    return 'WATCH';
}

function renderBucketPerformance() {
    const rows = calculateBucketPerformance(selectedPredictions(), strategyScopedSignals());
    const trading = rows.filter(r => r.signals > 0).slice().sort((a, b) => b.total_pnl - a.total_pnl);
    const pred = rows.filter(r => r.predictions > 0).slice().sort((a, b) => (b.accuracy || 0) - (a.accuracy || 0));
    setText('best-trading-bucket', trading[0] ? `T-${trading[0].bucket}` : '---');
    setText('best-prediction-bucket', pred[0] ? `T-${pred[0].bucket}` : '---');
    setText('operating-zone', bestZone(trading));
    setText('worst-zone', worstZone(rows));

    renderBarChart('pnl-by-bucket', rows.filter(r => r.signals > 0).map(r => ({ label: `T-${r.bucket}`, value: r.total_pnl, format: signedMoney })));
    renderBarChart('accuracy-by-bucket', rows.filter(r => r.predictions > 0).map(r => ({ label: `T-${r.bucket}`, value: (r.accuracy || 0) * 100, format: v => `${v.toFixed(1)}%`, positiveOnly: true })));
    const alignRows = alignmentPnl(strategyScopedSignals());
    renderBarChart('alignment-pnl', alignRows.map(r => ({ label: r.label, value: r.pnl, format: signedMoney })));

    const tbody = document.getElementById('bucket-performance-body');
    tbody.innerHTML = '';
    rows.forEach(row => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>T-${row.bucket}</td>
            <td>${row.predictions}</td>
            <td>${pct(row.accuracy)}</td>
            <td>${row.signals}</td>
            <td>${pct(row.win_rate)}</td>
            <td class="${row.total_pnl >= 0 ? 'up' : 'down'}">${signedMoney(row.total_pnl)}</td>
            <td>${pct(row.avg_roi)}</td>
            <td>${fmtPrice(row.avg_entry)}</td>
            <td>${pct(row.aligned_pct)}</td>
            <td><span class="badge ${row.recommendation.toLowerCase().replace('_', '-')}">${row.recommendation}</span></td>
        `;
        tbody.appendChild(tr);
    });
}

function renderRecentPerformance() {
    const allSignals = resolvedSignals(strategyScopedSignals());
    const recentSignals = resolvedSignals(filteredSignalsByRange(strategyScopedSignals(), state.selectedTimeRange));
    const allPnl = sum(allSignals.map(s => num(s.pnl)));
    const recentPnl = sum(recentSignals.map(s => num(s.pnl)));
    const recentPreds = filteredPredictionsByRange(selectedPredictions().filter(p => p.outcome === 'UP' || p.outcome === 'DOWN'), state.selectedTimeRange);
    const recentAcc = recentPreds.length ? recentPreds.filter(p => p.prediction === p.outcome).length / recentPreds.length : null;
    const status = driftStatus(allPnl, recentPnl, recentSignals.length);

    setText('drift-all-pnl', signedMoney(allPnl));
    setClassByValue('drift-all-pnl', allPnl);
    setText('drift-recent-pnl', signedMoney(recentPnl));
    setClassByValue('drift-recent-pnl', recentPnl);
    setText('drift-signals', recentSignals.length);
    setText('drift-accuracy', pct(recentAcc));
    setText('drift-badge', status);
    document.getElementById('drift-badge').className = `badge ${status.toLowerCase().replace(' ', '-')}`;
    setText('drift-copy', recentCopy(status, recentPnl, allPnl));
    renderRecentPnlTrend(allSignals.slice().reverse().slice(-16));
}

function driftStatus(allPnl, recentPnl, recentCount) {
    if (recentCount < 5) return 'LOW SAMPLE';
    if (recentPnl > 0 && allPnl >= 0) return 'STABLE';
    if (recentPnl > 0 && allPnl < 0) return 'IMPROVING';
    if (recentPnl < 0 && allPnl > 0) return 'DETERIORATING';
    return 'WATCH';
}

function recentCopy(status, recentPnl, allPnl) {
    if (status === 'DETERIORATING') return `Recent performance is weaker than all-time performance. Recent PnL is ${signedMoney(recentPnl)} vs all-time ${signedMoney(allPnl)}.`;
    if (status === 'IMPROVING') return `Recent performance is improving. Recent PnL is ${signedMoney(recentPnl)}.`;
    if (status === 'LOW SAMPLE') return 'Low sample size: results may not be reliable yet.';
    return `Recent performance is ${status.toLowerCase()}. Keep comparing bucket and alignment drift.`;
}

function renderSignalSummary() {
    const rows = strategyScopedSignals();
    const resolved = resolvedSignals(rows);
    const wins = resolved.filter(s => s.result === 'WIN').length;
    const pnl = sum(resolved.map(s => num(s.pnl)));
    const stakeSpent = sum(rows.map(signalStake));
    const isModelTest = state.selectedSignalSource === 'model_test';

    setText('signal-summary-last-label', isModelTest ? 'Last Test Signal' : 'Since Last Signal');
    setText('signal-summary-avg-label', isModelTest ? 'Avg Test Signal Gap' : 'Avg Signal Interval');
    setText('signal-summary-accuracy', resolved.length ? pct(wins / resolved.length) : 'pending');
    setText('signal-summary-pnl', signedMoney(pnl));
    setClassByValue('signal-summary-pnl', pnl);
    setText('signal-summary-stake', money(stakeSpent));
    setText('signal-summary-avg-gap', averageSignalInterval(rows));
    setText('signal-summary-count', rows.length);
    if (isModelTest) {
        const latest = Math.max(0, ...rows.map(s => dateMs(s.observed_at)).filter(Boolean));
        setText('signal-summary-last', latest ? time(latest) : '---');
    } else {
        updateSignalElapsed(rows);
    }
}

function averagePredictionInterval(predictions) {
    const times = [...new Set((predictions || []).map(p => dateMs(p.observed_at)).filter(Boolean))]
        .sort((a, b) => a - b);
    if (times.length < 2) return '---';
    const gaps = times.slice(1).map((value, index) => value - times[index]);
    return formatDuration(avg(gaps));
}

function updateSignalElapsed(rows = null) {
    const el = document.getElementById('signal-summary-last');
    if (!el || !state.dashboard) return;
    if (state.selectedSignalSource === 'model_test') return;
    const signals = rows || strategyScopedSignals();
    const latest = Math.max(0, ...signals.map(s => dateMs(s.observed_at)).filter(Boolean));
    setText('signal-summary-last', latest ? formatDuration(Date.now() - latest) : '---');
}

function averageSignalInterval(signals) {
    const times = [...new Set((signals || []).map(s => dateMs(s.observed_at)).filter(Boolean))]
        .sort((a, b) => a - b);
    if (times.length < 2) return '---';
    const gaps = times.slice(1).map((value, index) => value - times[index]);
    return formatDuration(avg(gaps));
}

function signalStake(signal) {
    const stake = nullableNum(signal?.stake);
    return stake === null ? 1 : stake;
}

function signalIsProvisional(signal) {
    return signal?.round_status === 'provisional' || String(signal?.close_source || '').startsWith('provisional_');
}

function signalDisplayResult(signal) {
    if (signal?.result && signal.result !== 'OPEN') {
        return signalIsProvisional(signal) ? `PROVISIONAL ${signal.result}` : signal.result;
    }
    const cutoff = Number(signal?.round_cutoff);
    if (!Number.isFinite(cutoff)) return 'OPEN';
    const now = Date.now() / 1000;
    if (now <= cutoff) return 'OPEN';
    if (now <= cutoff + PROVISIONAL_CLOSE_AFTER_SECONDS) return 'CLOSING';
    return 'RESOLVING';
}

function signalMatchesResultFilter(signal) {
    if (state.selectedResult === 'all') return true;
    if (state.selectedResult === 'PROVISIONAL') return Boolean(signal?.result && signalIsProvisional(signal));
    if (state.selectedResult === 'WIN' || state.selectedResult === 'LOSS') return signal?.result === state.selectedResult;
    return signalDisplayResult(signal) === state.selectedResult;
}

function resultClassName(result) {
    return `res-${String(result).toLowerCase().replace(/\s+/g, '-')}`;
}

function renderSignalsTable() {
    const tbody = document.getElementById('signals-body');
    const rows = strategyScopedSignals()
        .filter(s => state.selectedAlignment === 'all' || classifySignalAlignment(s.prediction, s.action) === state.selectedAlignment)
        .filter(signalMatchesResultFilter)
        .slice(0, state.signalLimit);
    setText('signals-showing', `${rows.length} shown`);
    tbody.innerHTML = '';
    rows.forEach(signal => {
        const alignment = classifySignalAlignment(signal.prediction, signal.action);
        const reason = signalReason(signal, alignment);
        const displayResult = signalDisplayResult(signal);
        const tr = document.createElement('tr');
        const stake = signalStake(signal);
        const cutoffTs = Number(signal.round_cutoff);
        const cutoffStr = cutoffTs ? time(new Date(cutoffTs * 1000).toISOString()) : '---';
        tr.innerHTML = `
            <td>${time(signal.observed_at)}</td>
            <td class="muted">${cutoffStr}</td>
            <td>T-${signal.seconds_bucket}</td>
            <td class="pred-${String(signal.prediction).toLowerCase()}">${signal.prediction} ${pct(signal.prob_up)}</td>
            <td>${signal.action}</td>
            <td class="${alignment === 'ALIGNED' ? 'up' : alignment === 'CONTRARIAN' ? 'warn' : 'neutral'}">${alignment}</td>
            <td>${fmtPrice(signal.entry_price)}</td>
            <td>${money(stake)}</td>
            <td>${pct(signal.model_prob)}</td>
            <td>${pct(signal.edge)}</td>
            <td class="${resultClassName(displayResult)}">${displayResult}</td>
            <td class="${num(signal.pnl) >= 0 ? 'up' : 'down'}">${signal.pnl === null || signal.pnl === undefined ? '---' : signedMoney(signal.pnl)}</td>
            <td>${reason}</td>
        `;
        tbody.appendChild(tr);
    });
}

function renderRoundsTable() {
    const tbody = document.getElementById('rounds-body');
    const signalsByRound = groupBy(roundScopedSignals(), s => s.round_cutoff);
    const predictionRounds = roundsFromPredictions(selectedPredictions());
    const rows = predictionRounds.length ? predictionRounds : [];
    renderRoundSummary();
    tbody.innerHTML = '';
    rows.slice(0, 80).forEach(row => {
        const sigs = signalsByRound[row.round_cutoff] || [];
        const resolved = resolvedSignals(sigs);
        const tradePnl = sum(resolved.map(s => num(s.pnl)));
        const initialCorrect = row.outcome && row.initial_prediction ? row.initial_prediction === row.outcome : null;
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${dateTimeFromTs(row.round_cutoff)}</td>
            <td>${money(row.baseline)}</td>
            <td class="pred-${String(row.initial_prediction || '').toLowerCase()}">${row.initial_prediction || '---'} ${pct(row.initial_prob_up)}</td>
            <td>${row.actual_close === null || row.actual_close === undefined ? '---' : money(row.actual_close)}</td>
            <td class="pred-${String(row.outcome || '').toLowerCase()}">${row.outcome || '---'}</td>
            <td>${boolText(initialCorrect)}</td>
            <td>${sigs.length ? `${sigs.length} ${selectedSignalScopeLabel()}` : 'No'}</td>
            <td class="${tradePnl >= 0 ? 'up' : 'down'}">${sigs.length ? signedMoney(tradePnl) : '---'}</td>
        `;
        tbody.appendChild(tr);
    });
}

function renderRoundSummary() {
    const sourcePredictions = selectedPredictions();
    const predictionRounds = roundsFromPredictions(sourcePredictions);
    const rows = (predictionRounds.length ? predictionRounds : [])
        .filter(r => r.outcome === 'UP' || r.outcome === 'DOWN');
    const allResolvedPredictions = sourcePredictions
        .filter(p => (p.outcome === 'UP' || p.outcome === 'DOWN') && (p.prediction === 'UP' || p.prediction === 'DOWN'));
    const initialRows = rows.filter(r => r.initial_prediction === 'UP' || r.initial_prediction === 'DOWN');
    const initialCorrect = initialRows.filter(r => r.initial_prediction === r.outcome).length;
    const initialPredictionPnlRows = initialRows.filter(r => r.initial_entry_price !== null && r.initial_entry_price !== undefined);
    const initialPredictionPnl = sum(initialPredictionPnlRows.map(r => simulatedPnl(1, r.initial_entry_price, r.initial_prediction === r.outcome)));
    const allPredictionCorrect = allResolvedPredictions.filter(r => r.prediction === r.outcome).length;
    const selectedResolvedSignals = resolvedSignals(roundScopedSignals());
    const selectedSignalWins = selectedResolvedSignals.filter(s => s.result === 'WIN').length;
    const selectedSignalPnl = sum(selectedResolvedSignals.map(s => num(s.pnl)));
    const isModelTest = state.selectedSignalSource === 'model_test';
    const signalLabel = selectedSignalScopeLabel();

    setText('round-source-title', isModelTest
        ? `Model Test Split: ML predictions + ${activeStrategyName()} replay`
        : `Post-production Live: ML predictions + ${activeStrategyName()} replay`);
    setText('round-initial-label', isModelTest ? 'Model Test Initial Accuracy' : 'Live Initial Accuracy');
    setText('round-initial-pnl-label', isModelTest ? 'Model Test Initial Prediction PnL' : 'Live Initial Prediction PnL');
    setText('round-final-label', isModelTest ? 'Model Test All Timeframe Accuracy' : 'Live All Timeframe Accuracy');
    setText('round-resolved-label', isModelTest ? 'Test Rounds' : 'Resolved Rounds');
    setText('round-initial-accuracy', initialRows.length ? pct(initialCorrect / initialRows.length) : 'pending');
    setText('round-initial-pnl', initialPredictionPnlRows.length ? signedMoney(initialPredictionPnl) : 'pending');
    setClassByValue('round-initial-pnl', initialPredictionPnl);
    setText('round-final-accuracy', allResolvedPredictions.length ? pct(allPredictionCorrect / allResolvedPredictions.length) : 'pending');
    setText('round-signal-accuracy-label', `${signalLabel} Accuracy`);
    setText('round-signal-accuracy', selectedResolvedSignals.length ? pct(selectedSignalWins / selectedResolvedSignals.length) : 'pending');
    setText('round-resolved-count', rows.length);
    setText('round-trade-pnl-label', `${signalLabel} PnL`);
    setText('round-trade-pnl', signedMoney(selectedSignalPnl));
    setClassByValue('round-trade-pnl', selectedSignalPnl);
    setText('round-signals-th', isModelTest ? `${activeStrategyName()} Test Signals` : `${activeStrategyName()} Live Strategy Signals`);
    setText('round-pnl-th', isModelTest ? `${activeStrategyName()} Test Signal PnL` : `${activeStrategyName()} Live Strategy PnL`);
}

function selectedSignalScopeLabel() {
    if (state.selectedSignalSource === 'model_test') return `${activeStrategyName()} Test Signal`;
    return `${activeStrategyName()} Live Strategy Signal`;
}

function roundsFromPredictions(predictions) {
    const resolved = (predictions || []).filter(p => p.round_cutoff && (p.outcome === 'UP' || p.outcome === 'DOWN' || p.outcome === null || p.outcome === undefined));
    const byRound = groupBy(resolved, p => p.round_cutoff);
    return Object.entries(byRound).map(([roundCutoff, values]) => {
        const sortedInitial = values.slice().sort((a, b) => {
            const secDiff = num(b.seconds_to_cutoff) - num(a.seconds_to_cutoff);
            if (secDiff) return secDiff;
            return String(a.observed_at || '').localeCompare(String(b.observed_at || ''));
        });
        const sortedFinal = values.slice().sort((a, b) => {
            const secDiff = num(a.seconds_to_cutoff) - num(b.seconds_to_cutoff);
            if (secDiff) return secDiff;
            return String(b.observed_at || '').localeCompare(String(a.observed_at || ''));
        });
        const initial = sortedInitial[0] || {};
        const final = sortedFinal[0] || initial;
        const initialEntryPrice = initial.prediction === 'UP'
            ? initial.up_best_ask
            : initial.prediction === 'DOWN'
                ? initial.down_best_ask
                : null;
        return {
            round_cutoff: Number(roundCutoff),
            baseline: final.baseline,
            initial_prediction: initial.prediction,
            initial_prob_up: initial.prob_up,
            initial_entry_price: initialEntryPrice,
            initial_observed_at: initial.observed_at,
            prediction: final.prediction,
            prob_up: final.prob_up,
            actual_close: final.actual_close,
            outcome: final.outcome,
            target_up: final.target_up,
            model_version: final.model_version,
            model_stage: final.model_stage,
        };
    }).sort((a, b) => b.round_cutoff - a.round_cutoff);
}

function renderBtcChart() {
    const canvas = document.getElementById('btc-chart');
    if (!canvas || !state.chart || !state.chart.candles) return;
    const candles = state.chart.candles.slice(-120);
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.floor(rect.width * dpr);
    canvas.height = Math.floor(rect.height * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);

    if (!candles.length) return;
    const pad = { left: 56, right: 76, top: 20, bottom: 34 };
    const innerW = rect.width - pad.left - pad.right;
    const innerH = rect.height - pad.top - pad.bottom;
    const highs = candles.map(c => num(c.high));
    const lows = candles.map(c => num(c.low));
    const maxPrice = Math.max(...highs, num(state.current.window_open || 0));
    const minPrice = Math.min(...lows, num(state.current.window_open || highs[0]));
    const span = Math.max(1, maxPrice - minPrice);
    const y = price => pad.top + (maxPrice - price) / span * innerH;
    const xStep = innerW / candles.length;

    ctx.fillStyle = '#0d1422';
    ctx.fillRect(0, 0, rect.width, rect.height);
    ctx.strokeStyle = 'rgba(148, 163, 184, 0.14)';
    ctx.lineWidth = 1;
    ctx.font = '11px JetBrains Mono, monospace';
    ctx.fillStyle = '#93a4ba';

    for (let i = 0; i <= 4; i++) {
        const yy = pad.top + innerH * i / 4;
        const price = maxPrice - span * i / 4;
        ctx.beginPath();
        ctx.moveTo(pad.left, yy);
        ctx.lineTo(rect.width - pad.right, yy);
        ctx.stroke();
        ctx.fillText(money(price), rect.width - pad.right + 10, yy + 4);
    }

    candles.forEach((c, i) => {
        const x = pad.left + i * xStep + xStep / 2;
        const open = num(c.open);
        const close = num(c.close);
        const high = num(c.high);
        const low = num(c.low);
        const color = close >= open ? '#22c55e' : '#ff4d5e';
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(x, y(high));
        ctx.lineTo(x, y(low));
        ctx.stroke();
        const bodyTop = Math.min(y(open), y(close));
        const bodyH = Math.max(2, Math.abs(y(open) - y(close)));
        ctx.fillRect(x - Math.max(2, xStep * 0.28), bodyTop, Math.max(4, xStep * 0.56), bodyH);
    });

    const baseline = num(state.current.window_open);
    if (baseline) {
        const yy = y(baseline);
        ctx.strokeStyle = '#f59e0b';
        ctx.setLineDash([6, 5]);
        ctx.beginPath();
        ctx.moveTo(pad.left, yy);
        ctx.lineTo(rect.width - pad.right, yy);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = '#f59e0b';
        ctx.fillText(`Baseline ${money(baseline)}`, pad.left + 8, yy - 8);
    }

    const last = candles[candles.length - 1];
    setText('chart-price', money(last.close));
    setText('chart-source', `${state.chart.market_interval || '15m'} / ${state.chart.candles.length} candles`);
}

function filteredSignalsByRange(signals, range) {
    return filterByRange(signals, range, 'observed_at', 'round_cutoff');
}

function filteredPredictionsByRange(predictions, range) {
    return filterByRange(predictions, range, 'observed_at', 'round_cutoff');
}

function filterByRange(rows, range, timeField, roundField) {
    if (!rows || range === 'all') return rows || [];
    const now = Date.now();
    if (range === 'today') {
        const today = new Date();
        today.setHours(0, 0, 0, 0);
        return rows.filter(row => dateMs(row[timeField]) >= today.getTime());
    }
    if (range.endsWith('h')) {
        const hours = Number(range.replace('h', ''));
        return rows.filter(row => dateMs(row[timeField]) >= now - hours * 3600 * 1000);
    }
    if (range.endsWith('r')) {
        const count = Number(range.replace('r', ''));
        const recentRounds = [...new Set(rows.map(row => Number(row[roundField])).filter(Boolean))].sort((a, b) => b - a).slice(0, count);
        return rows.filter(row => recentRounds.includes(Number(row[roundField])));
    }
    return rows;
}

function resolvedSignals(signals) {
    return (signals || []).filter(s => s.result === 'WIN' || s.result === 'LOSS');
}

function alignmentPnl(signals) {
    const rows = resolvedSignals(signals);
    return ['ALIGNED', 'CONTRARIAN', 'WAIT', 'NO_SIGNAL'].map(label => ({
        label,
        pnl: sum(rows.filter(s => classifySignalAlignment(s.prediction, s.action) === label).map(s => num(s.pnl)))
    })).filter(r => r.pnl !== 0);
}

function bestZone(rows) {
    const good = rows.filter(r => r.total_pnl > 0).slice(0, 3).map(r => `T-${r.bucket}`);
    return good.length ? good.join(' to ') : 'Needs more data';
}

function worstZone(rows) {
    const sorted = rows.filter(r => r.signals > 0).slice().sort((a, b) => a.total_pnl - b.total_pnl);
    return sorted[0] ? `T-${sorted[0].bucket}` : 'Needs more data';
}

function renderBarChart(id, rows) {
    const root = document.getElementById(id);
    root.innerHTML = '';
    const maxAbs = Math.max(1, ...rows.map(r => Math.abs(r.value)));
    rows.slice(0, 14).forEach(row => {
        const pctWidth = Math.max(2, Math.abs(row.value) / maxAbs * 100);
        const div = document.createElement('div');
        div.className = 'bar-row';
        div.innerHTML = `
            <span>${row.label}</span>
            <div class="bar-track"><div class="bar-fill" style="width:${pctWidth}%;background:${row.value >= 0 ? 'var(--green)' : 'var(--red)'}"></div></div>
            <span class="${row.value >= 0 ? 'up' : 'down'}">${row.format(row.value)}</span>
        `;
        root.appendChild(div);
    });
}

function renderRecentPnlTrend(rows) {
    const root = document.getElementById('recent-pnl-trend');
    root.innerHTML = '';
    const maxAbs = Math.max(1, ...rows.map(r => Math.abs(num(r.pnl))));
    rows.forEach(row => {
        const h = Math.max(4, Math.abs(num(row.pnl)) / maxAbs * 68);
        const div = document.createElement('div');
        div.className = 'mini-bar';
        div.style.height = `${h}px`;
        div.style.background = num(row.pnl) >= 0 ? 'var(--green)' : 'var(--red)';
        div.title = `${dateTimeFromTs(row.round_cutoff)} ${signedMoney(row.pnl)}`;
        root.appendChild(div);
    });
}

function signalReason(signal, alignment) {
    if (signal.reason) return signal.reason;
    if (alignment === 'CONTRARIAN') return 'Longshot bet: bought cheap side against model prediction';
    if (alignment === 'ALIGNED') return 'Directional bet: follows model prediction';
    return 'No directional signal';
}

function edgeCopy(c, decision) {
    const raw = c.raw_recommended_action || c.recommended_action || 'WAIT';
    if (decision.alignment === 'CONTRARIAN') return `${raw.replace('_', ' ')} has positive edge, but it is contrarian.`;
    if (decision.alignment === 'ALIGNED') return `${raw.replace('_', ' ')} is aligned with the model and has positive edge.`;
    return 'No valid edge under the selected strategy.';
}

function bestEdgeSide(c) {
    const edges = [
        { side: 'UP', edge: c.edge_up },
        { side: 'DOWN', edge: c.edge_down }
    ].filter(x => x.edge !== null && x.edge !== undefined);
    if (!edges.length) return null;
    edges.sort((a, b) => b.edge - a.edge);
    return edges[0];
}

function groupBy(rows, fn) {
    return (rows || []).reduce((acc, row) => {
        const key = fn(row);
        if (key === null || key === undefined) return acc;
        acc[key] = acc[key] || [];
        acc[key].push(row);
        return acc;
    }, {});
}

function avg(values) {
    const clean = values.filter(v => Number.isFinite(v));
    return clean.length ? sum(clean) / clean.length : null;
}

function sum(values) {
    return values.reduce((acc, value) => acc + (Number.isFinite(value) ? value : 0), 0);
}

function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
}

function nullableNum(value) {
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
}

function simulatedPnl(stake, entryPrice, won) {
    if (!won) return -Number(stake || 0);
    const entry = Number(entryPrice);
    if (!entry) return 0;
    // 2% Polymarket fee on gross profit
    return Number(stake || 0) * ((1 / entry) - 1) * 0.98;
}

function pct(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '---';
    return `${(Number(value) * 100).toFixed(1)}%`;
}

function signedPct(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '---';
    const n = Number(value) * 100;
    return `${n >= 0 ? '+' : ''}${n.toFixed(1)}%`;
}

function money(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '$---';
    return `$${Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function signedMoney(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '---';
    const n = Number(value);
    return `${n >= 0 ? '+' : '-'}$${Math.abs(n).toFixed(2)}`;
}

function fmtPrice(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return '--';
    return Number(value).toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
}

function time(value) {
    if (!value) return '---';
    return parseDashboardDate(value).toLocaleTimeString([], {
        timeZone: 'America/Lima',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit'
    });
}

function dateMs(value) {
    return value ? parseDashboardDate(value).getTime() : 0;
}

function dateTimeFromTs(ts) {
    if (!ts) return '---';
    return new Date(Number(ts) * 1000).toLocaleTimeString([], {
        timeZone: 'America/Lima',
        hour: '2-digit',
        minute: '2-digit'
    });
}

function formatDuration(ms) {
    if (ms === null || ms === undefined || !Number.isFinite(Number(ms))) return '---';
    const totalSeconds = Math.max(0, Math.floor(Number(ms) / 1000));
    const seconds = totalSeconds % 60;
    const totalMinutes = Math.floor(totalSeconds / 60);
    const minutes = totalMinutes % 60;
    const totalHours = Math.floor(totalMinutes / 60);
    const hours = totalHours % 24;
    const days = Math.floor(totalHours / 24);
    if (days) return `${days}d ${hours}h`;
    if (totalHours) return `${totalHours}h ${minutes}m`;
    if (totalMinutes) return `${totalMinutes}m ${seconds}s`;
    return `${seconds}s`;
}

function parseDashboardDate(value) {
    if (value instanceof Date) return value;
    if (typeof value !== 'string') return new Date(value);
    const hasTimezone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(value);
    return new Date(hasTimezone ? value : `${value}Z`);
}

function boolText(value) {
    if (value === null || value === undefined) return '---';
    return value ? 'YES' : 'NO';
}

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
}

function setClassByValue(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('up', 'down', 'neutral');
    el.classList.add(value > 0 ? 'up' : value < 0 ? 'down' : 'neutral');
}

function setClassByDecision(id, decision) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = decision === 'BUY_UP' ? 'up' : decision === 'BUY_DOWN' ? 'down' : decision === 'SKIP' ? 'warn' : 'neutral';
}

function setClassByPrediction(id, prediction) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = prediction === 'UP' ? 'up' : prediction === 'DOWN' ? 'down' : 'neutral';
}

function setClassByAlignment(id, alignment) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = `alignment-line ${alignment === 'ALIGNED' ? 'up' : alignment === 'CONTRARIAN' ? 'warn' : 'neutral'}`;
}

function setBadgeClass(id, alignment) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = `badge ${alignment === 'ALIGNED' ? 'good' : alignment === 'CONTRARIAN' ? 'watch' : 'neutral'}`;
}

function renderRuleList(id, items) {
    const root = document.getElementById(id);
    root.innerHTML = '';
    if (!items.length) {
        const li = document.createElement('li');
        li.textContent = 'None';
        root.appendChild(li);
        return;
    }
    items.forEach(item => {
        const li = document.createElement('li');
        li.textContent = item;
        root.appendChild(li);
    });
}
