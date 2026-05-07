create or replace view modeling_snapshots as
select
    p.id as prediction_id,
    p.observed_at,
    p.round_cutoff,
    p.model_version,
    s.window_start,
    s.seconds_to_cutoff,
    s.btc_price,
    s.baseline,
    s.dist_to_baseline,
    s.dist_to_baseline_pct,
    p.prediction,
    p.prob_up,
    p.prob_down,
    p.confidence,
    p.edge_up,
    p.edge_down,
    p.recommended_action,
    p.feature_values,
    p.raw as prediction_raw,
    r.actual_close,
    r.outcome,
    case
        when r.outcome = 'UP' then 1
        when r.outcome = 'DOWN' then 0
        else null
    end as target_up
from model_predictions p
left join round_snapshots s on s.id = p.source_snapshot_id
left join round_results r on r.round_cutoff = p.round_cutoff;

create or replace view simulated_bet_performance as
select
    id,
    observed_at,
    round_cutoff,
    side,
    entry_price,
    stake,
    model_prob,
    edge,
    result,
    pnl,
    nullif(raw ->> 'seconds_to_cutoff', '')::numeric as seconds_to_cutoff,
    nullif(raw ->> 'baseline', '')::numeric as baseline,
    nullif(raw ->> 'btc_price', '')::numeric as btc_price,
    nullif(raw #>> '{resolved,actual_close}', '')::numeric as actual_close,
    raw #>> '{resolved,outcome}' as outcome
from simulated_bets;

create or replace view model_bucket_performance as
select
    width_bucket(seconds_to_cutoff, 0, 300, 10) as time_bucket,
    width_bucket(edge, -0.20, 0.20, 16) as edge_bucket,
    side,
    count(*) as bets,
    count(*) filter (where result = 'WIN') as wins,
    count(*) filter (where result = 'LOSS') as losses,
    round(avg(case when result = 'WIN' then 1.0 when result = 'LOSS' then 0.0 end) * 100, 2) as win_rate,
    round(sum(coalesce(pnl, 0)), 4) as total_pnl
from simulated_bet_performance
where result in ('WIN', 'LOSS')
group by 1, 2, 3;
