set quoted_identifier on;
set ansi_nulls on;
go

if db_id(N'PolymarketBot') is null
begin
    create database PolymarketBot;
end
go

use PolymarketBot;
go

set quoted_identifier on;
set ansi_nulls on;
go

if object_id(N'dbo.rounds', N'U') is null
begin
    create table dbo.rounds (
        id uniqueidentifier not null default newid() primary key,
        round_id nvarchar(80) not null unique,
        event_slug nvarchar(200) null,
        condition_id nvarchar(200) null,
        window_start bigint not null,
        round_cutoff bigint not null unique,
        baseline decimal(19, 6) null,
        baseline_source nvarchar(80) not null default N'coinbase_btc_usd_prev_close',
        resolution_source nvarchar(300) null,
        close_source nvarchar(80) null,
        status nvarchar(20) not null default N'open',
        resolved_at datetime2 null,
        raw nvarchar(max) not null default N'{}',
        created_at datetime2 not null default sysutcdatetime(),
        updated_at datetime2 not null default sysutcdatetime()
    );
end
go

if object_id(N'dbo.reference_prices', N'U') is null
begin
    create table dbo.reference_prices (
        id uniqueidentifier not null default newid() primary key,
        round_id uniqueidentifier null references dbo.rounds(id),
        observed_at datetime2 not null default sysutcdatetime(),
        source nvarchar(80) not null,
        symbol nvarchar(40) not null default N'BTC-USD',
        price decimal(19, 6) not null,
        seconds_to_cutoff int null,
        purpose nvarchar(30) not null,
        raw nvarchar(max) not null default N'{}'
    );
end
go

if object_id(N'dbo.decision_snapshots', N'U') is null
begin
    create table dbo.decision_snapshots (
        id uniqueidentifier not null default newid() primary key,
        round_id uniqueidentifier not null references dbo.rounds(id),
        observed_at datetime2 not null default sysutcdatetime(),
        seconds_to_cutoff int not null,
        seconds_bucket int not null,
        capture_reason nvarchar(50) not null default N'scheduled',
        btc_price decimal(19, 6) not null,
        baseline decimal(19, 6) null,
        dist_to_baseline decimal(19, 6) null,
        dist_to_baseline_pct decimal(19, 8) null,
        baseline_source nvarchar(80) null,
        collector_version nvarchar(80) not null default N'collector-v2',
        raw nvarchar(max) not null default N'{}'
    );
end
go

if not exists (select 1 from sys.indexes where name = N'uq_decision_snapshots_round_bucket' and object_id = object_id(N'dbo.decision_snapshots'))
begin
    create unique index uq_decision_snapshots_round_bucket
        on dbo.decision_snapshots(round_id, seconds_bucket)
        where capture_reason = N'scheduled';
end
go

if object_id(N'dbo.market_quotes', N'U') is null
begin
    create table dbo.market_quotes (
        id uniqueidentifier not null default newid() primary key,
        snapshot_id uniqueidentifier not null references dbo.decision_snapshots(id),
        round_id uniqueidentifier not null references dbo.rounds(id),
        observed_at datetime2 not null default sysutcdatetime(),
        outcome nvarchar(10) not null,
        token_id nvarchar(200) null,
        best_bid decimal(12, 6) null,
        best_ask decimal(12, 6) null,
        midpoint decimal(12, 6) null,
        spread decimal(12, 6) null,
        bid_size decimal(19, 6) null,
        ask_size decimal(19, 6) null,
        last_trade_price decimal(12, 6) null,
        book_hash nvarchar(200) null,
        raw nvarchar(max) not null default N'{}',
        constraint uq_market_quotes_snapshot_outcome unique(snapshot_id, outcome)
    );
end
go

if object_id(N'dbo.feature_snapshots', N'U') is null
begin
    create table dbo.feature_snapshots (
        id uniqueidentifier not null default newid() primary key,
        snapshot_id uniqueidentifier not null references dbo.decision_snapshots(id),
        round_id uniqueidentifier not null references dbo.rounds(id),
        observed_at datetime2 not null default sysutcdatetime(),
        feature_set_version nvarchar(80) not null,
        features nvarchar(max) not null default N'{}',
        constraint uq_feature_snapshots_snapshot_version unique(snapshot_id, feature_set_version)
    );
end
go

if object_id(N'dbo.predictions_v2', N'U') is null
begin
    create table dbo.predictions_v2 (
        id uniqueidentifier not null default newid() primary key,
        snapshot_id uniqueidentifier not null references dbo.decision_snapshots(id),
        round_id uniqueidentifier not null references dbo.rounds(id),
        observed_at datetime2 not null default sysutcdatetime(),
        model_version nvarchar(120) not null,
        model_stage nvarchar(30) not null default N'production',
        prediction nvarchar(10) not null,
        prob_up decimal(12, 8) not null,
        prob_down decimal(12, 8) not null,
        confidence decimal(12, 8) not null,
        edge_up decimal(12, 8) null,
        edge_down decimal(12, 8) null,
        recommended_action nvarchar(30) null,
        feature_set_version nvarchar(80) null,
        raw nvarchar(max) not null default N'{}',
        constraint uq_predictions_snapshot_model_stage unique(snapshot_id, model_version, model_stage)
    );
end
go

if object_id(N'dbo.signals_v2', N'U') is null
begin
    create table dbo.signals_v2 (
        id uniqueidentifier not null default newid() primary key,
        prediction_id uniqueidentifier not null references dbo.predictions_v2(id),
        snapshot_id uniqueidentifier not null references dbo.decision_snapshots(id),
        round_id uniqueidentifier not null references dbo.rounds(id),
        observed_at datetime2 not null default sysutcdatetime(),
        strategy_version nvarchar(80) not null,
        action nvarchar(30) not null,
        side nvarchar(10) null,
        entry_price decimal(12, 6) null,
        model_prob decimal(12, 8) null,
        edge decimal(12, 8) null,
        stake decimal(19, 6) not null default 10,
        raw nvarchar(max) not null default N'{}',
        constraint uq_signals_prediction_strategy unique(prediction_id, strategy_version)
    );
end
go

if object_id(N'dbo.round_results', N'U') is null
begin
    create table dbo.round_results (
        id uniqueidentifier not null default newid() primary key,
        round_cutoff bigint not null unique,
        resolved_at datetime2 not null default sysutcdatetime(),
        baseline decimal(19, 6) null,
        actual_close decimal(19, 6) not null,
        outcome nvarchar(10) not null,
        raw nvarchar(max) not null default N'{}'
    );
end
go

if object_id(N'dbo.trade_results_v2', N'U') is null
begin
    create table dbo.trade_results_v2 (
        id uniqueidentifier not null default newid() primary key,
        signal_id uniqueidentifier not null unique references dbo.signals_v2(id),
        round_id uniqueidentifier not null references dbo.rounds(id),
        result nvarchar(10) not null,
        pnl decimal(19, 6) null,
        roi decimal(19, 8) null,
        resolved_at datetime2 not null default sysutcdatetime(),
        raw nvarchar(max) not null default N'{}'
    );
end
go

if object_id(N'dbo.dataset_versions', N'U') is null
begin
    create table dbo.dataset_versions (
        id uniqueidentifier not null default newid() primary key,
        dataset_version nvarchar(120) not null unique,
        source_query_hash nvarchar(80) not null,
        row_count int not null,
        round_count int not null,
        start_time datetime2 null,
        end_time datetime2 null,
        filters nvarchar(max) not null default N'{}',
        parquet_path nvarchar(500) null,
        raw nvarchar(max) not null default N'{}',
        created_at datetime2 not null default sysutcdatetime()
    );
end
go

if object_id(N'dbo.model_runs', N'U') is null
begin
    create table dbo.model_runs (
        id uniqueidentifier not null default newid() primary key,
        run_id nvarchar(120) not null unique,
        model_version nvarchar(120) not null,
        dataset_version nvarchar(120) null,
        feature_set_version nvarchar(120) null,
        algorithm nvarchar(80) not null,
        hyperparameters nvarchar(max) not null default N'{}',
        metrics nvarchar(max) not null default N'{}',
        artifact_path nvarchar(500) null,
        model_stage nvarchar(30) not null default N'candidate',
        notes nvarchar(max) null,
        created_at datetime2 not null default sysutcdatetime()
    );
end
go

if object_id(N'dbo.strategy_backtest_runs', N'U') is null
begin
    create table dbo.strategy_backtest_runs (
        id uniqueidentifier not null default newid() primary key,
        run_id nvarchar(120) not null unique,
        model_version nvarchar(120) not null,
        dataset_version nvarchar(120) null,
        strategy_set_version nvarchar(120) not null,
        source_row_count int not null default 0,
        resolved_round_count int not null default 0,
        signal_count int not null default 0,
        started_at datetime2 not null default sysutcdatetime(),
        completed_at datetime2 null,
        metrics nvarchar(max) not null default N'{}',
        raw nvarchar(max) not null default N'{}'
    );
end
go

if object_id(N'dbo.strategy_backtest_signals', N'U') is null
begin
    create table dbo.strategy_backtest_signals (
        id uniqueidentifier not null default newid() primary key,
        backtest_run_id uniqueidentifier not null references dbo.strategy_backtest_runs(id),
        snapshot_id uniqueidentifier not null references dbo.decision_snapshots(id),
        round_id uniqueidentifier not null references dbo.rounds(id),
        observed_at datetime2 not null,
        round_cutoff bigint not null,
        seconds_to_cutoff int null,
        seconds_bucket int null,
        model_version nvarchar(120) not null,
        model_stage nvarchar(30) not null default N'backtest',
        strategy_version nvarchar(80) not null,
        action nvarchar(30) not null,
        side nvarchar(10) not null,
        prediction nvarchar(10) not null,
        prob_up decimal(12, 8) not null,
        prob_down decimal(12, 8) not null,
        confidence decimal(12, 8) not null,
        entry_price decimal(12, 6) not null,
        model_prob decimal(12, 8) not null,
        edge decimal(12, 8) not null,
        stake decimal(19, 6) not null default 10,
        alignment nvarchar(20) not null,
        result nvarchar(10) not null,
        pnl decimal(19, 6) null,
        roi decimal(19, 8) null,
        actual_close decimal(19, 6) null,
        outcome nvarchar(10) null,
        reason nvarchar(500) null,
        raw nvarchar(max) not null default N'{}',
        constraint uq_backtest_signal unique(backtest_run_id, snapshot_id, strategy_version)
    );
end
go

if object_id(N'dbo.training_decision_snapshots', N'V') is not null drop view dbo.training_decision_snapshots;
go
create view dbo.training_decision_snapshots as
with quote_pivot as (
    select
        snapshot_id,
        max(case when outcome = N'UP' then best_bid end) as up_best_bid,
        max(case when outcome = N'UP' then best_ask end) as up_best_ask,
        max(case when outcome = N'UP' then midpoint end) as up_midpoint,
        max(case when outcome = N'UP' then spread end) as up_spread,
        max(case when outcome = N'UP' then bid_size end) as up_bid_size,
        max(case when outcome = N'UP' then ask_size end) as up_ask_size,
        max(case when outcome = N'UP' then last_trade_price end) as up_last_trade_price,
        max(case when outcome = N'DOWN' then best_bid end) as down_best_bid,
        max(case when outcome = N'DOWN' then best_ask end) as down_best_ask,
        max(case when outcome = N'DOWN' then midpoint end) as down_midpoint,
        max(case when outcome = N'DOWN' then spread end) as down_spread,
        max(case when outcome = N'DOWN' then bid_size end) as down_bid_size,
        max(case when outcome = N'DOWN' then ask_size end) as down_ask_size,
        max(case when outcome = N'DOWN' then last_trade_price end) as down_last_trade_price
    from dbo.market_quotes
    group by snapshot_id
),
production_predictions as (
    select *
    from (
        select
            p.*,
            row_number() over(partition by p.snapshot_id order by p.observed_at desc) as rn
        from dbo.predictions_v2 p
        where p.model_stage = N'production'
    ) ranked
    where rn = 1
)
select
    cast(ds.id as nvarchar(36)) as snapshot_id,
    cast(r.id as nvarchar(36)) as round_pk,
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
    cast(p.id as nvarchar(36)) as prediction_id,
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
        when rr.outcome = N'UP' then 1
        when rr.outcome = N'DOWN' then 0
        else null
    end as target_up
from dbo.decision_snapshots ds
join dbo.rounds r on r.id = ds.round_id
left join quote_pivot qp on qp.snapshot_id = ds.id
left join dbo.feature_snapshots fs on fs.snapshot_id = ds.id
left join production_predictions p on p.snapshot_id = ds.id
left join dbo.round_results rr on rr.round_cutoff = r.round_cutoff;
go

if object_id(N'dbo.strategy_performance_v2', N'V') is not null drop view dbo.strategy_performance_v2;
go
create view dbo.strategy_performance_v2 as
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
from dbo.signals_v2 s
join dbo.rounds r on r.id = s.round_id
join dbo.decision_snapshots ds on ds.id = s.snapshot_id
join dbo.predictions_v2 p on p.id = s.prediction_id
left join dbo.trade_results_v2 tr on tr.signal_id = s.id
left join dbo.round_results rr on rr.round_cutoff = r.round_cutoff;
go

if object_id(N'dbo.model_performance_by_version', N'V') is not null drop view dbo.model_performance_by_version;
go
create view dbo.model_performance_by_version as
select
    p.model_version,
    p.model_stage,
    count(*) as rows_scored,
    count(distinct r.round_cutoff) as unique_rounds,
    cast(round(avg(case when p.prediction = rr.outcome then 1.0 else 0.0 end) * 100, 2) as decimal(8,2)) as row_accuracy,
    cast(round(avg(power(cast(p.prob_up as float) - case when rr.outcome = N'UP' then 1.0 else 0.0 end, 2)), 6) as decimal(12,6)) as brier_score,
    min(p.observed_at) as first_prediction_at,
    max(p.observed_at) as last_prediction_at
from dbo.predictions_v2 p
join dbo.rounds r on r.id = p.round_id
join dbo.round_results rr on rr.round_cutoff = r.round_cutoff
where rr.outcome in (N'UP', N'DOWN')
group by p.model_version, p.model_stage;
go

if object_id(N'dbo.bucket_performance_v2', N'V') is not null drop view dbo.bucket_performance_v2;
go
create view dbo.bucket_performance_v2 as
select
    seconds_bucket,
    cast(floor(((cast(edge as float) + 0.20) / 0.40) * 16) + 1 as int) as edge_bucket,
    model_version,
    strategy_version,
    side,
    count(*) as signals,
    sum(case when result = N'WIN' then 1 else 0 end) as wins,
    sum(case when result = N'LOSS' then 1 else 0 end) as losses,
    cast(round(avg(case when result = N'WIN' then 1.0 when result = N'LOSS' then 0.0 end) * 100, 2) as decimal(8,2)) as win_rate,
    cast(round(sum(coalesce(pnl, 0)), 4) as decimal(19,4)) as total_pnl,
    cast(round(avg(roi), 4) as decimal(19,4)) as avg_roi
from dbo.strategy_performance_v2
where result in (N'WIN', N'LOSS')
group by seconds_bucket, cast(floor(((cast(edge as float) + 0.20) / 0.40) * 16) + 1 as int), model_version, strategy_version, side;
go

if object_id(N'dbo.strategy_backtest_performance_v2', N'V') is not null drop view dbo.strategy_backtest_performance_v2;
go
create view dbo.strategy_backtest_performance_v2 as
select
    bs.id as signal_id,
    br.run_id,
    br.strategy_set_version,
    br.completed_at,
    bs.observed_at,
    bs.round_cutoff,
    bs.seconds_to_cutoff,
    bs.seconds_bucket,
    bs.model_version,
    bs.model_stage,
    bs.strategy_version,
    bs.action,
    bs.side,
    bs.prediction,
    bs.prob_up,
    bs.prob_down,
    bs.confidence,
    bs.entry_price,
    bs.model_prob,
    bs.edge,
    bs.stake,
    bs.alignment,
    bs.result,
    bs.pnl,
    bs.roi,
    bs.actual_close,
    bs.outcome,
    bs.reason
from dbo.strategy_backtest_signals bs
join dbo.strategy_backtest_runs br on br.id = bs.backtest_run_id;
go
