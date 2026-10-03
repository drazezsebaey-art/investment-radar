"""
engine/regime.py - market regime per UTC day (RC-1.2 section 9, version R1).

The label of day D uses daily candles closed up to D-1 only (strictly past):
  TREND_UP   BTC close > SMA200, slope of SMA50 over 20 days > 0, breadth >= 50%
  RISK_OFF   BTC close < SMA200 and (breadth < 35% or 30d realised vol >= 80th
             percentile of the 365 values before)
  RANGE      not the above and BTC ADX(14) < 20
  TRANSITION everything else
  DATA_UNCERTAIN breadth on < 50 symbols, or BTC input missing
Breadth = share of the regime universe (top 100 eligible on D) whose close is
above its own SMA50 (both at D-1).
"""
import numpy as np
import pandas as pd

REGIME_VERSION = "R1"


def wilder_adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    up, dn = h.diff(), -l.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi)
    return dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def btc_features(btc: pd.DataFrame) -> pd.DataFrame:
    c = btc["close"]
    f = pd.DataFrame(index=btc.index)
    f["close"] = c
    f["sma200"] = c.rolling(200).mean()
    f["sma50"] = c.rolling(50).mean()
    f["slope50"] = f["sma50"] - f["sma50"].shift(20)
    f["adx"] = wilder_adx(btc)
    f["vol30"] = np.log(c).diff().rolling(30).std() * np.sqrt(365)
    # percentile of vol30[t] among the 365 values strictly before t
    vals = f["vol30"].to_numpy()
    pct = np.full(len(vals), np.nan)
    for i in range(365, len(vals)):
        past = vals[i - 365:i]
        past = past[~np.isnan(past)]
        if len(past) >= 300 and not np.isnan(vals[i]):
            pct[i] = (past < vals[i]).mean() * 100
    f["vol_pct"] = pct
    return f


def above_sma50(daily: dict) -> dict:
    """{symbol: Series(bool) close > SMA50, NaN-safe (False until 50 bars)}"""
    out = {}
    for s, d in daily.items():
        sma = d["close"].rolling(50).mean()
        out[s] = (d["close"] > sma).where(sma.notna())
    return out


def compute_regimes(daily: dict, universe, start: str, end: str) -> pd.DataFrame:
    btc = daily.get("BTCUSDT")
    feats = btc_features(btc) if btc is not None else None
    above = above_sma50(daily)
    rows = []
    for D in pd.date_range(start, end, freq="D", tz="UTC"):
        prev = D - pd.Timedelta(days=1)
        rec = {"day": D.date().isoformat()}
        f = feats.loc[prev] if feats is not None and prev in feats.index else None
        members = universe.regime_universe(rec["day"])
        flags = [bool(above[s].loc[prev]) for s in members
                 if s in above and prev in above[s].index and pd.notna(above[s].loc[prev])]
        rec["breadth_n"] = len(flags)
        rec["breadth"] = round(100 * sum(flags) / len(flags), 2) if flags else None
        if f is None or f[["close", "sma200", "sma50", "slope50", "adx"]].isna().any() or len(flags) < 50:
            rec["regime"] = "DATA_UNCERTAIN"
        else:
            b = rec["breadth"]
            vp = f["vol_pct"]
            if f["close"] > f["sma200"] and f["slope50"] > 0 and b >= 50:
                rec["regime"] = "TREND_UP"
            elif f["close"] < f["sma200"] and (b < 35 or (pd.notna(vp) and vp >= 80)):
                rec["regime"] = "RISK_OFF"
            elif f["adx"] < 20:
                rec["regime"] = "RANGE"
            else:
                rec["regime"] = "TRANSITION"
            rec.update({k: round(float(f[k]), 4) for k in ("close", "sma200", "slope50", "adx")})
            rec["vol_pct"] = None if pd.isna(vp) else round(float(vp), 1)
        rows.append(rec)
    return pd.DataFrame(rows).set_index("day")
