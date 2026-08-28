"""Hand-rolled technical indicators — pure numpy/pandas, no TA-Lib.

Conventions:
- All functions return a pd.Series aligned to the input index; warm-up values are NaN.
- Wilder smoothing (RSI, ATR): seed = simple mean of the first `n` samples,
  then avg_t = (avg_{t-1}*(n-1) + x_t) / n.
- EMA seeds with SMA(n) at index n-1, alpha = 2/(n+1).
- Window policy: n < 1 or n > len(s) raises ValueError.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _as_series(s) -> pd.Series:
    if isinstance(s, pd.Series):
        return s.astype(float)
    return pd.Series(s, dtype=float)


def _check_window(s: pd.Series, n: int) -> None:
    if not isinstance(n, (int, np.integer)) or n < 1:
        raise ValueError(f"window must be a positive int, got {n!r}")
    if n > len(s):
        raise ValueError(f"window n={n} exceeds series length {len(s)}")


def sma(s, n: int) -> pd.Series:
    s = _as_series(s)
    _check_window(s, n)
    return s.rolling(n).mean()


def ema(s, n: int) -> pd.Series:
    s = _as_series(s)
    _check_window(s, n)
    vals = s.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    out[n - 1] = vals[:n].mean()
    alpha = 2.0 / (n + 1.0)
    for i in range(n, len(vals)):
        out[i] = out[i - 1] + alpha * (vals[i] - out[i - 1])
    return pd.Series(out, index=s.index)


def rsi(close, n: int = 14) -> pd.Series:
    """RSI with Wilder smoothing; first defined value at index n."""
    close = _as_series(close)
    _check_window(close, n)
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    out = np.full(len(close), np.nan)
    if len(close) <= n:
        return pd.Series(out, index=close.index)

    avg_gain = float(gain.iloc[1 : n + 1].mean())
    avg_loss = float(loss.iloc[1 : n + 1].mean())
    out[n] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(n + 1, len(close)):
        avg_gain = (avg_gain * (n - 1) + float(gain.iloc[i])) / n
        avg_loss = (avg_loss * (n - 1) + float(loss.iloc[i])) / n
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return pd.Series(out, index=close.index)


def atr(high, low, close, n: int = 14) -> pd.Series:
    """Average True Range, Wilder smoothing; first value at index n-1."""
    high, low, close = _as_series(high), _as_series(low), _as_series(close)
    if not (len(high) == len(low) == len(close)):
        raise ValueError("high/low/close must have equal length")
    _check_window(high, n)
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)  # tr[0] degenerates to high-low

    out = np.full(len(high), np.nan)
    if len(high) < n:
        return pd.Series(out, index=high.index)

    atr_val = float(tr.iloc[:n].mean())  # seed = SMA of first n TRs (TR0 = high-low)
    out[n - 1] = atr_val
    for i in range(n, len(high)):
        atr_val = (atr_val * (n - 1) + float(tr.iloc[i])) / n
        out[i] = atr_val
    return pd.Series(out, index=high.index)


def roc(s, n: int) -> pd.Series:
    """Rate of change in percent over n bars."""
    s = _as_series(s)
    _check_window(s, n)
    return s.pct_change(n) * 100.0


def donchian_high(high, n: int) -> pd.Series:
    """Rolling max of highs over the last n bars (inclusive of current bar)."""
    high = _as_series(high)
    _check_window(high, n)
    return high.rolling(n).max()


def realized_vol_pct(ret, n: int, periods_per_year: int = 252) -> pd.Series:
    """Annualized realized volatility in percent from a returns series.

    rv = rolling_sample_std(ret, n) * sqrt(periods_per_year) * 100
    """
    ret = _as_series(ret)
    _check_window(ret, n)
    return ret.rolling(n).std(ddof=1) * np.sqrt(float(periods_per_year)) * 100.0


def slope(s, n: int) -> pd.Series:
    """Least-squares linear-regression slope of the last n values, per bar."""
    s = _as_series(s)
    if n < 2:
        raise ValueError(f"slope needs a window >= 2, got {n}")
    _check_window(s, n)
    x_mean = (n - 1) / 2.0
    x_centered_sq = ((np.arange(n) - x_mean) ** 2).sum()

    def _slope(window: np.ndarray) -> float:
        return float(((np.arange(n) - x_mean) * (window - window.mean())).sum() / x_centered_sq)

    return s.rolling(n).apply(_slope, raw=True)
