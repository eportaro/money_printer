"""
Feature Engineering Engine — Technical Indicators + Pro Tricks
==============================================================
All indicators implemented from scratch using numpy/pandas only.
No external TA library needed.
"""

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────
# Core Technical Indicators
# ─────────────────────────────────────────────────────────────

def sma(series, period):
    return series.rolling(window=period, min_periods=period).mean()


def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))


def macd(series, fast=12, slow=26, signal=9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(series, period=20, std_dev=2):
    mid = sma(series, period)
    std = series.rolling(window=period, min_periods=period).std()
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    width = (upper - lower) / (mid + 1e-10)
    pct_b = (series - lower) / (upper - lower + 1e-10)
    return upper, mid, lower, width, pct_b


def atr(high, low, close, period=14):
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=period).mean()


def stochastic(high, low, close, k_period=14, d_period=3):
    lowest_low = low.rolling(window=k_period, min_periods=k_period).min()
    highest_high = high.rolling(window=k_period, min_periods=k_period).max()
    k = 100 * (close - lowest_low) / (highest_high - lowest_low + 1e-10)
    d = k.rolling(window=d_period, min_periods=d_period).mean()
    return k, d


def williams_r(high, low, close, period=14):
    highest_high = high.rolling(window=period, min_periods=period).max()
    lowest_low = low.rolling(window=period, min_periods=period).min()
    return -100 * (highest_high - close) / (highest_high - lowest_low + 1e-10)


def cci(high, low, close, period=14):
    tp = (high + low + close) / 3
    tp_sma = sma(tp, period)
    mean_dev = tp.rolling(window=period, min_periods=period).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    )
    return (tp - tp_sma) / (0.015 * mean_dev + 1e-10)


def obv(close, volume):
    direction = np.sign(close.diff())
    direction.iloc[0] = 0
    return (direction * volume).cumsum()


def mfi(high, low, close, volume, period=14):
    tp = (high + low + close) / 3
    raw_mf = tp * volume
    delta = tp.diff()
    pos_mf = raw_mf.where(delta > 0, 0.0).rolling(window=period, min_periods=period).sum()
    neg_mf = raw_mf.where(delta <= 0, 0.0).rolling(window=period, min_periods=period).sum()
    mf_ratio = pos_mf / (neg_mf + 1e-10)
    return 100 - (100 / (1 + mf_ratio))


def vwap(high, low, close, volume):
    tp = (high + low + close) / 3
    cum_tp_vol = (tp * volume).cumsum()
    cum_vol = volume.cumsum()
    return cum_tp_vol / (cum_vol + 1e-10)


# ─────────────────────────────────────────────────────────────
# Feature Columns (used by model for training/prediction)
# ─────────────────────────────────────────────────────────────

FEATURE_COLUMNS = [
    # Polymarket Context (CRITICAL)
    'dist_to_window_open', 'minutes_into_window',
    # Trend
    'sma_5', 'sma_10', 'sma_20', 'sma_50',
    'ema_5', 'ema_10', 'ema_20', 'ema_50',
    'price_vs_sma20', 'price_vs_sma50',
    'ema_cross_5_20', 'sma_cross_10_50',
    # Momentum
    'rsi_14', 'rsi_slope',
    'macd_line', 'macd_signal', 'macd_hist', 'macd_hist_slope',
    'stoch_k', 'stoch_d', 'stoch_cross',
    'willr_14', 'cci_14', 'roc_5', 'roc_10',
    # Volatility
    'bb_upper', 'bb_lower', 'bb_width', 'bb_pct_b',
    'atr_14', 'atr_pct',
    'rolling_vol_5', 'rolling_vol_15', 'rolling_vol_30',
    'bb_squeeze',
    # Volume
    'obv', 'obv_slope',
    'mfi_14', 'vwap_val', 'price_vs_vwap',
    'volume_ratio_20', 'taker_buy_ratio',
    # Price Action
    'returns_1', 'returns_5', 'returns_10', 'returns_15', 'returns_30',
    'candle_body_pct', 'upper_shadow_pct', 'lower_shadow_pct',
    'hl_range_pct',
    'consecutive_direction',
    # Time
    'hour_sin', 'hour_cos', 'dow_sin', 'dow_cos',
]


def compute_all_features(df):
    """
    Compute all technical indicators and engineered features.
    """
    df = df.copy()
    c = df['close']
    h = df['high']
    l = df['low']
    o = df['open']
    v = df['volume_btc']

    # ── Polymarket Context ──
    # Calculate window open for each bar
    window_id = df['timestamp'] // 300_000
    df['w_open'] = df.groupby(window_id)['open'].transform('first')
    df['dist_to_window_open'] = (c - df['w_open']) / (df['w_open'] + 1e-10) * 100
    df['minutes_into_window'] = (df['timestamp'] // 60_000) % 5

    # ── Trend ──
    df['sma_5'] = sma(c, 5)
    df['sma_10'] = sma(c, 10)
    df['sma_20'] = sma(c, 20)
    df['sma_50'] = sma(c, 50)
    df['ema_5'] = ema(c, 5)
    df['ema_10'] = ema(c, 10)
    df['ema_20'] = ema(c, 20)
    df['ema_50'] = ema(c, 50)
    df['price_vs_sma20'] = (c - df['sma_20']) / (df['sma_20'] + 1e-10) * 100
    df['price_vs_sma50'] = (c - df['sma_50']) / (df['sma_50'] + 1e-10) * 100
    df['ema_cross_5_20'] = (df['ema_5'] - df['ema_20']) / (df['ema_20'] + 1e-10) * 100
    df['sma_cross_10_50'] = (df['sma_10'] - df['sma_50']) / (df['sma_50'] + 1e-10) * 100

    # ── Momentum ──
    df['rsi_14'] = rsi(c, 14)
    df['rsi_slope'] = df['rsi_14'].diff(3) / 3
    ml, ms, mh = macd(c)
    df['macd_line'] = ml
    df['macd_signal'] = ms
    df['macd_hist'] = mh
    df['macd_hist_slope'] = mh.diff(3) / 3
    df['stoch_k'], df['stoch_d'] = stochastic(h, l, c)
    df['stoch_cross'] = df['stoch_k'] - df['stoch_d']
    df['willr_14'] = williams_r(h, l, c, 14)
    df['cci_14'] = cci(h, l, c, 14)
    df['roc_5'] = c.pct_change(5) * 100
    df['roc_10'] = c.pct_change(10) * 100

    # ── Volatility ──
    df['bb_upper'], _, df['bb_lower'], df['bb_width'], df['bb_pct_b'] = bollinger_bands(c)
    df['atr_14'] = atr(h, l, c, 14)
    df['atr_pct'] = df['atr_14'] / (c + 1e-10) * 100
    returns = c.pct_change()
    df['rolling_vol_5'] = returns.rolling(5).std() * 100
    df['rolling_vol_15'] = returns.rolling(15).std() * 100
    df['rolling_vol_30'] = returns.rolling(30).std() * 100
    bb_width_sma = sma(df['bb_width'], 50)
    df['bb_squeeze'] = (df['bb_width'] < bb_width_sma * 0.75).astype(float)

    # ── Volume ──
    df['obv'] = obv(c, v)
    df['obv_slope'] = df['obv'].diff(5)
    df['mfi_14'] = mfi(h, l, c, v, 14)
    df['vwap_val'] = vwap(h, l, c, v)
    df['price_vs_vwap'] = (c - df['vwap_val']) / (df['vwap_val'] + 1e-10) * 100
    vol_sma_20 = sma(v, 20)
    df['volume_ratio_20'] = v / (vol_sma_20 + 1e-10)
    df['taker_buy_ratio'] = df['taker_buy_base'] / (v + 1e-10)

    # ── Price Action ──
    df['returns_1'] = c.pct_change(1) * 100
    df['returns_5'] = c.pct_change(5) * 100
    df['returns_10'] = c.pct_change(10) * 100
    df['returns_15'] = c.pct_change(15) * 100
    df['returns_30'] = c.pct_change(30) * 100
    body = (c - o).abs()
    full_range = h - l + 1e-10
    df['candle_body_pct'] = body / full_range
    df['upper_shadow_pct'] = (h - pd.concat([c, o], axis=1).max(axis=1)) / full_range
    df['lower_shadow_pct'] = (pd.concat([c, o], axis=1).min(axis=1) - l) / full_range
    df['hl_range_pct'] = (h - l) / (c + 1e-10) * 100

    # Consecutive direction
    direction = np.sign(c.diff())
    groups = (direction != direction.shift()).cumsum()
    df['consecutive_direction'] = direction.groupby(groups).cumcount() + 1
    df['consecutive_direction'] *= direction

    # ── Time Features ──
    dt = pd.to_datetime(df['timestamp'], unit='ms', utc=True)
    hour = dt.dt.hour + dt.dt.minute / 60
    dow = dt.dt.dayofweek
    df['hour_sin'] = np.sin(2 * np.pi * hour / 24)
    df['hour_cos'] = np.cos(2 * np.pi * hour / 24)
    df['dow_sin'] = np.sin(2 * np.pi * dow / 7)
    df['dow_cos'] = np.cos(2 * np.pi * dow / 7)

    # Normalization
    for col in ['sma_5', 'sma_10', 'sma_20', 'sma_50', 'ema_5', 'ema_10', 'ema_20', 'ema_50', 'bb_upper', 'bb_lower', 'vwap_val']:
        df[col] = (df[col] - c) / (c + 1e-10) * 100
    
    obv_mean = df['obv'].rolling(50).mean()
    obv_std = df['obv'].rolling(50).std()
    df['obv'] = (df['obv'] - obv_mean) / (obv_std + 1e-10)

    return df


def prepare_dataset(df_1min):
    """
    Prepare feature matrix aligned to Polymarket rounds.
    Now trains on EVERY MINUTE of the window.
    """
    df = compute_all_features(df_1min)
    df['window_id'] = df['timestamp'] // 300_000

    # Get window results
    window_stats = df.groupby('window_id').agg(
        w_open=('open', 'first'),
        w_close=('close', 'last')
    )
    window_stats['target'] = (window_stats['w_close'] > window_stats['w_open']).astype(int)

    # Join target back to every minute bar
    df = df.merge(window_stats[['target']], left_on='window_id', right_index=True)

    # Drop NaNs and select features
    df = df.dropna(subset=FEATURE_COLUMNS + ['target'])
    
    X = df[FEATURE_COLUMNS].copy()
    y = df['target'].copy()
    meta = df[['timestamp', 'window_id']].copy()

    return X, y, meta

