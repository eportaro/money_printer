create extension if not exists pgcrypto;

create table if not exists rounds (
    id uuid primary key default gen_random_uuid(),
    round_id text unique not null,
    event_slug text,
    condition_id text,
    window_start bigint not null,
    round_cutoff bigint not null unique,
    baseline numeric,
    baseline_source text not null default 'binance_prev_close',
    resolution_source text,
    close_source text,
    status text not null default 'open' check (status in ('open', 'resolved', 'skipped')),
    resolved_at timestamptz,
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists idx_rounds_status_cutoff
    on rounds (status, round_cutoff desc);

create table if not exists reference_prices (
    id uuid primary key default gen_random_uuid(),
    round_id uuid references rounds(id) on delete cascade,
    observed_at timestamptz not null default now(),
    source text not null,
    symbol text not null default 'BTCUSDT',
    price numeric not null,
    seconds_to_cutoff integer,
    purpose text not null check (purpose in ('baseline', 'snapshot', 'close', 'manual_sync')),
    raw jsonb not null default '{}'::jsonb
);

create index if not exists idx_reference_prices_round_time
    on reference_prices (round_id, observed_at desc);

create table if not exists decision_snapshots (
    id uuid primary key default gen_random_uuid(),
    round_id uuid not null references rounds(id) on delete cascade,
    observed_at timestamptz not null default now(),
    seconds_to_cutoff integer not null,
    seconds_bucket integer not null,
    capture_reason text not null default 'scheduled',
    btc_price numeric not null,
    baseline numeric,
    dist_to_baseline numeric,
    dist_to_baseline_pct numeric,
    baseline_source text,
    collector_version text not null default 'collector-v2',
    raw jsonb not null default '{}'::jsonb
);

create unique index if not exists uq_decision_snapshots_scheduled_bucket
    on decision_snapshots (round_id, seconds_bucket)
    where capture_reason = 'scheduled';

create index if not exists idx_decision_snapshots_round_time
    on decision_snapshots (round_id, observed_at desc);

create table if not exists market_quotes (
    id uuid primary key default gen_random_uuid(),
    snapshot_id uuid not null references decision_snapshots(id) on delete cascade,
    round_id uuid not null references rounds(id) on delete cascade,
    observed_at timestamptz not null default now(),
    outcome text not null check (outcome in ('UP', 'DOWN')),
    token_id text,
    best_bid numeric,
    best_ask numeric,
    midpoint numeric,
    spread numeric,
    bid_size numeric,
    ask_size numeric,
    last_trade_price numeric,
    book_hash text,
    raw jsonb not null default '{}'::jsonb
);

create unique index if not exists uq_market_quotes_snapshot_outcome
    on market_quotes (snapshot_id, outcome);

create index if not exists idx_market_quotes_round_time
    on market_quotes (round_id, observed_at desc);

create table if not exists feature_snapshots (
    id uuid primary key default gen_random_uuid(),
    snapshot_id uuid not null references decision_snapshots(id) on delete cascade,
    round_id uuid not null references rounds(id) on delete cascade,
    observed_at timestamptz not null default now(),
    feature_set_version text not null,
    features jsonb not null default '{}'::jsonb
);

create unique index if not exists uq_feature_snapshots_snapshot_version
    on feature_snapshots (snapshot_id, feature_set_version);

create table if not exists predictions_v2 (
    id uuid primary key default gen_random_uuid(),
    snapshot_id uuid not null references decision_snapshots(id) on delete cascade,
    round_id uuid not null references rounds(id) on delete cascade,
    observed_at timestamptz not null default now(),
    model_version text not null,
    model_stage text not null default 'production' check (model_stage in ('production', 'shadow', 'candidate')),
    prediction text not null check (prediction in ('UP', 'DOWN')),
    prob_up numeric not null,
    prob_down numeric not null,
    confidence numeric not null,
    edge_up numeric,
    edge_down numeric,
    recommended_action text,
    feature_set_version text,
    raw jsonb not null default '{}'::jsonb
);

create unique index if not exists uq_predictions_v2_snapshot_model_stage
    on predictions_v2 (snapshot_id, model_version, model_stage);

create index if not exists idx_predictions_v2_round_time
    on predictions_v2 (round_id, observed_at desc);

create table if not exists signals_v2 (
    id uuid primary key default gen_random_uuid(),
    prediction_id uuid not null references predictions_v2(id) on delete cascade,
    snapshot_id uuid not null references decision_snapshots(id) on delete cascade,
    round_id uuid not null references rounds(id) on delete cascade,
    observed_at timestamptz not null default now(),
    strategy_version text not null,
    action text not null,
    side text check (side in ('UP', 'DOWN')),
    entry_price numeric,
    model_prob numeric,
    edge numeric,
    stake numeric not null default 10,
    raw jsonb not null default '{}'::jsonb
);

create unique index if not exists uq_signals_v2_prediction_strategy
    on signals_v2 (prediction_id, strategy_version);

create index if not exists idx_signals_v2_round_time
    on signals_v2 (round_id, observed_at desc);

create table if not exists trade_results_v2 (
    id uuid primary key default gen_random_uuid(),
    signal_id uuid not null unique references signals_v2(id) on delete cascade,
    round_id uuid not null references rounds(id) on delete cascade,
    result text not null check (result in ('WIN', 'LOSS', 'TIE')),
    pnl numeric,
    roi numeric,
    resolved_at timestamptz not null default now(),
    raw jsonb not null default '{}'::jsonb
);

create index if not exists idx_trade_results_v2_round
    on trade_results_v2 (round_id, resolved_at desc);

create table if not exists dataset_versions (
    id uuid primary key default gen_random_uuid(),
    dataset_version text unique not null,
    source_query_hash text not null,
    row_count integer not null,
    round_count integer not null,
    start_time timestamptz,
    end_time timestamptz,
    filters jsonb not null default '{}'::jsonb,
    parquet_path text,
    raw jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

create table if not exists model_runs (
    id uuid primary key default gen_random_uuid(),
    run_id text unique not null,
    model_version text not null,
    dataset_version text,
    feature_set_version text,
    algorithm text not null,
    hyperparameters jsonb not null default '{}'::jsonb,
    metrics jsonb not null default '{}'::jsonb,
    artifact_path text,
    model_stage text not null default 'candidate' check (model_stage in ('production', 'shadow', 'candidate', 'archived')),
    notes text,
    created_at timestamptz not null default now()
);

create or replace view training_decision_snapshots as
with quote_pivot as (
    select
        snapshot_id,
        max(best_bid) filter (where outcome = 'UP') as up_best_bid,
        max(best_ask) filter (where outcome = 'UP') as up_best_ask,
        max(midpoint) filter (where outcome = 'UP') as up_midpoint,
        max(spread) filter (where outcome = 'UP') as up_spread,
        max(bid_size) filter (where outcome = 'UP') as up_bid_size,
        max(ask_size) filter (where outcome = 'UP') as up_ask_size,
        max(last_trade_price) filter (where outcome = 'UP') as up_last_trade_price,
        max(best_bid) filter (where outcome = 'DOWN') as down_best_bid,
        max(best_ask) filter (where outcome = 'DOWN') as down_best_ask,
        max(midpoint) filter (where outcome = 'DOWN') as down_midpoint,
        max(spread) filter (where outcome = 'DOWN') as down_spread,
        max(bid_size) filter (where outcome = 'DOWN') as down_bid_size,
        max(ask_size) filter (where outcome = 'DOWN') as down_ask_size,
        max(last_trade_price) filter (where outcome = 'DOWN') as down_last_trade_price
    from market_quotes
    group by snapshot_id
),
production_predictions as (
    select distinct on (snapshot_id)
        *
    from predictions_v2
    where model_stage = 'production'
    order by snapshot_id, observed_at desc
)
select
    ds.id as snapshot_id,
    r.id as round_pk,
    r.round_id,
    r.event_slug,
    r.condition_id,
    r.window_start,
    r.round_cutoff,
    ds.observed_at,
    ds.seconds_to_cutoff,
    ds.seconds_bucket,
    ds.capture_reason,
    ds.btc_price,
    ds.baseline,
    ds.dist_to_baseline,
    ds.dist_to_baseline_pct,
    r.baseline_source,
    r.resolution_source,
    qp.up_best_bid,
    qp.up_best_ask,
    qp.up_midpoint,
    qp.up_spread,
    qp.up_bid_size,
    qp.up_ask_size,
    qp.up_last_trade_price,
    qp.down_best_bid,
    qp.down_best_ask,
    qp.down_midpoint,
    qp.down_spread,
    qp.down_bid_size,
    qp.down_ask_size,
    qp.down_last_trade_price,
    fs.feature_set_version,
    fs.features,
    p.id as prediction_id,
    p.model_version,
    p.model_stage,
    p.prediction,
    p.prob_up,
    p.prob_down,
    p.confidence,
    p.edge_up,
    p.edge_down,
    p.recommended_action,
    rr.actual_close,
    rr.outcome,
    case
        when rr.outcome = 'UP' then 1
        when rr.outcome = 'DOWN' then 0
        else null
    end as target_up
from decision_snapshots ds
join rounds r on r.id = ds.round_id
left join quote_pivot qp on qp.snapshot_id = ds.id
left join feature_snapshots fs on fs.snapshot_id = ds.id
left join production_predictions p on p.snapshot_id = ds.id
left join round_results rr on rr.round_cutoff = r.round_cutoff;

create or replace view strategy_performance_v2 as
select
    s.id as signal_id,
    s.observed_at,
    r.round_id,
    r.round_cutoff,
    ds.seconds_to_cutoff,
    ds.seconds_bucket,
    ds.btc_price,
    ds.baseline,
    ds.dist_to_baseline,
    ds.dist_to_baseline_pct,
    r.baseline_source,
    r.resolution_source,
    p.model_version,
    p.model_stage,
    s.strategy_version,
    s.action,
    s.side,
    s.entry_price,
    s.stake,
    s.model_prob,
    s.edge,
    tr.result,
    tr.pnl,
    tr.roi,
    tr.resolved_at,
    rr.actual_close,
    rr.outcome
from signals_v2 s
join rounds r on r.id = s.round_id
join decision_snapshots ds on ds.id = s.snapshot_id
join predictions_v2 p on p.id = s.prediction_id
left join trade_results_v2 tr on tr.signal_id = s.id
left join round_results rr on rr.round_cutoff = r.round_cutoff;

create or replace view model_performance_by_version as
select
    p.model_version,
    p.model_stage,
    count(*) filter (where rr.outcome in ('UP', 'DOWN')) as rows_scored,
    count(distinct r.round_cutoff) filter (where rr.outcome in ('UP', 'DOWN')) as unique_rounds,
    round(avg(case when p.prediction = rr.outcome then 1.0 else 0.0 end) * 100, 2) as row_accuracy,
    round(avg(power(p.prob_up - case when rr.outcome = 'UP' then 1.0 else 0.0 end, 2)), 6) as brier_score,
    min(p.observed_at) as first_prediction_at,
    max(p.observed_at) as last_prediction_at
from predictions_v2 p
join rounds r on r.id = p.round_id
join round_results rr on rr.round_cutoff = r.round_cutoff
where rr.outcome in ('UP', 'DOWN')
group by 1, 2;

create or replace view bucket_performance_v2 as
select
    seconds_bucket,
    width_bucket(edge, -0.20, 0.20, 16) as edge_bucket,
    model_version,
    strategy_version,
    side,
    count(*) as signals,
    count(*) filter (where result = 'WIN') as wins,
    count(*) filter (where result = 'LOSS') as losses,
    round(avg(case when result = 'WIN' then 1.0 when result = 'LOSS' then 0.0 end) * 100, 2) as win_rate,
    round(sum(coalesce(pnl, 0)), 4) as total_pnl,
    round(avg(roi), 4) as avg_roi
from strategy_performance_v2
where result in ('WIN', 'LOSS')
group by 1, 2, 3, 4, 5;
