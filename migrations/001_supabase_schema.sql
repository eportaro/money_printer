create extension if not exists pgcrypto;

create table if not exists polymarket_markets (
    id uuid primary key default gen_random_uuid(),
    event_id text,
    market_id text,
    condition_id text unique,
    question text,
    slug text,
    event_slug text,
    start_time timestamptz,
    end_time timestamptz,
    baseline numeric,
    token_up text,
    token_down text,
    outcomes jsonb,
    raw jsonb,
    active boolean default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists round_snapshots (
    id uuid primary key default gen_random_uuid(),
    observed_at timestamptz not null default now(),
    round_cutoff bigint not null,
    window_start bigint not null,
    seconds_to_cutoff integer not null,
    symbol text not null default 'BTCUSDT',
    btc_price numeric not null,
    baseline numeric,
    dist_to_baseline numeric,
    dist_to_baseline_pct numeric,
    source text not null default 'binance',
    market_condition_id text,
    raw jsonb
);

create index if not exists idx_round_snapshots_cutoff_time
    on round_snapshots (round_cutoff, observed_at desc);

create table if not exists polymarket_quotes (
    id uuid primary key default gen_random_uuid(),
    observed_at timestamptz not null default now(),
    round_cutoff bigint,
    market_condition_id text,
    token_id text not null,
    outcome text not null,
    best_bid numeric,
    best_ask numeric,
    midpoint numeric,
    spread numeric,
    last_trade_price numeric,
    bid_size numeric,
    ask_size numeric,
    book_hash text,
    raw jsonb
);

create index if not exists idx_polymarket_quotes_cutoff_time
    on polymarket_quotes (round_cutoff, observed_at desc);

create table if not exists model_predictions (
    id uuid primary key default gen_random_uuid(),
    observed_at timestamptz not null default now(),
    round_cutoff bigint not null,
    model_version text not null default 'local-hgb-v1',
    prediction text not null check (prediction in ('UP', 'DOWN')),
    prob_up numeric not null,
    prob_down numeric not null,
    confidence numeric not null,
    edge_up numeric,
    edge_down numeric,
    recommended_action text,
    feature_values jsonb,
    source_snapshot_id uuid references round_snapshots(id),
    raw jsonb
);

create index if not exists idx_model_predictions_cutoff_time
    on model_predictions (round_cutoff, observed_at desc);

create table if not exists round_results (
    id uuid primary key default gen_random_uuid(),
    round_cutoff bigint not null unique,
    resolved_at timestamptz not null default now(),
    baseline numeric,
    actual_close numeric not null,
    outcome text not null check (outcome in ('UP', 'DOWN', 'TIE')),
    raw jsonb
);

create table if not exists simulated_bets (
    id uuid primary key default gen_random_uuid(),
    observed_at timestamptz not null default now(),
    round_cutoff bigint not null,
    side text not null check (side in ('UP', 'DOWN')),
    entry_price numeric not null,
    stake numeric not null default 10,
    model_prob numeric,
    edge numeric,
    result text check (result in ('WIN', 'LOSS', 'OPEN')),
    pnl numeric,
    model_prediction_id uuid references model_predictions(id),
    raw jsonb
);

create index if not exists idx_simulated_bets_cutoff_time
    on simulated_bets (round_cutoff, observed_at desc);
