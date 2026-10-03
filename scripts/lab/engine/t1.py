"""
engine/t1.py - Playbook T1 (Trend & Momentum), version T1-v1.2, as frozen in the
T1 Claude Doc. Variants for validation: A (as written), B (targets 2R/4R), C (no ADX).

Every feature at 4H candle i uses candles <= i only; daily features use the last
daily candle that CLOSED at or before the signal time.
"""
import numpy as np
import pandas as pd

from .execution import Spec, Candle, simulate_both
from .universe import round_trip_cost, SLIPPAGE_PER_SIDE

H4, D1 = 14_400_000, 86_400_000
VERSION = "T1-v1.3"
P = dict(donchian=20, base_len=12, base_atr=2.5, vol_mult=1.5, rs_top=0.30, adx_min=20.0,
         close_strength=0.70, max_ext_atr=3.0, stop_atr=0.2, stop_atr_breakout=1.0, time_stop=30, max_slots=5,
         macro_before_h=6.0, btc_shock=-0.03)
ALLOWED = {"TREND_UP", "TRANSITION", "RANGE"}


def atr(df, n=14):
    tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def features_4h(c4: pd.DataFrame, p=P) -> pd.DataFrame:
    f = pd.DataFrame(index=c4.index)
    f["open_time"] = c4["open_time"]
    f["signal_time"] = c4["open_time"] + H4
    f["close"], f["high"], f["low"] = c4["close"], c4["high"], c4["low"]
    f["atr"] = atr(c4)
    f["ema20"] = c4["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    prev_hi = c4["high"].shift(1).rolling(p["donchian"]).max()
    f["donchian_high"] = prev_hi
    base_hi = c4["high"].shift(1).rolling(p["base_len"]).max()
    base_lo = c4["low"].shift(1).rolling(p["base_len"]).min()
    f["base_high"], f["base_low"] = base_hi, base_lo
    f["base_range_atr"] = (base_hi - base_lo) / f["atr"]
    rng = (c4["high"] - c4["low"]).replace(0, np.nan)
    f["close_strength"] = (c4["close"] - c4["low"]) / rng
    f["vol_ratio"] = c4["quote_volume"] / c4["quote_volume"].shift(1).rolling(20).median()
    f["ext_atr"] = (c4["close"] - f["ema20"]) / f["atr"]
    # data completeness over every window the signal reads (max(20 donchian, 20 volume, 12 base) + current)
    f["complete_window"] = c4["complete"].astype(int).rolling(21).min() == 1
    return f


def features_1d(d1: pd.DataFrame) -> pd.DataFrame:
    from .regime import wilder_adx
    f = pd.DataFrame(index=d1.index)
    f["close"] = d1["close"]
    f["ema20"] = d1["close"].ewm(span=20, adjust=False, min_periods=20).mean()
    f["ema50"] = d1["close"].ewm(span=50, adjust=False, min_periods=50).mean()
    f["adx"] = wilder_adx(d1)
    f["close_time"] = d1["open_time"] + D1          # usable from this moment on
    return f


def score(row) -> int:
    def b(x, edges, rev=False):
        pts = 0
        for k, e in enumerate(edges):
            if (x >= e) if not rev else (x < e):
                pts = 5 * (k + 1)
        return pts
    s = b(row["vol_ratio"], [1.5, 2, 3, 5])
    s += b(row["rs_pct"] * 100, [70, 80, 90, 95])
    s += (20 if row["base_range_atr"] < 1 else 15 if row["base_range_atr"] < 1.5
                                    else 10 if row["base_range_atr"] < 2 else 5)
    s += b(row["adx_d"], [20, 25, 30, 40])
    s += b(row["close_strength"], [0.70, 0.80, 0.90, 0.95])
    return s


def rs_table(daily: dict, universe, days: list) -> dict:
    """{day: {symbol: rs_percentile}} on the broad universe (RC-1.3); RS on the last
    completed day D-1 vs D-8 (7 days), minus BTC."""
    closes = {s: d["close"] for s, d in daily.items()}
    btc = closes["BTCUSDT"]
    out = {}
    for day in days:
        D = pd.Timestamp(day, tz="UTC") - pd.Timedelta(days=1)
        D7 = D - pd.Timedelta(days=7)
        if D not in btc.index or D7 not in btc.index:
            continue
        b = btc[D] / btc[D7] - 1
        members = universe.regime_universe(day)
        vals = {}
        for s in members:
            c = closes.get(s)
            if c is not None and D in c.index and D7 in c.index:
                vals[s] = (c[D] / c[D7] - 1) - b
        if len(vals) < 50:
            out[day] = None                      # SMALL_UNIVERSE
            continue
        ser = pd.Series(vals).rank(pct=True)
        out[day] = ser.to_dict()
    return out


def run(c4: dict, daily: dict, universe, regimes: pd.DataFrame, macro, start: str, end: str,
        variant: str = "A", p=P) -> dict:
    t0 = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    t1 = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp() * 1000)
    days = [d.date().isoformat() for d in pd.date_range(start, end, freq="D")]
    rs = rs_table(daily, universe, days)
    btc4 = c4["BTCUSDT"]
    btc_ret = (btc4["close"] / btc4["open"] - 1)
    btc_ret.index = (btc4["open_time"] + H4).values       # keyed by signal time
    d1f = {s: features_1d(d) for s, d in daily.items() if s in c4}
    cands, signals = [], []
    for s, d in c4.items():
        if s not in d1f or len(d) < 60:
            continue
        f = features_4h(d, p)
        trig = (f["close"] > f["donchian_high"]) & (f["signal_time"] >= t0) & (f["signal_time"] < t1)
        base = f["base_range_atr"] <= p["base_atr"]
        rows = f[trig & base]
        if rows.empty:
            continue
        df = d1f[s]
        ct = df["close_time"].to_numpy()
        for idx, r in rows.iterrows():
            st = int(r["signal_time"])
            k = np.searchsorted(ct, st, side="right") - 1          # last daily close <= signal time
            if k < 0:
                continue
            dr = df.iloc[k]
            cands.append((st, s, idx, r, dr))
    cands.sort(key=lambda x: (x[0], x[1]))
    open_until = {}                                              # symbol -> exit time
    trades = []
    i = 0
    while i < len(cands):
        st = cands[i][0]
        batch = []
        while i < len(cands) and cands[i][0] == st:
            batch.append(cands[i])
            i += 1
        day = pd.Timestamp(st - 1, unit="ms", tz="UTC").date().isoformat()   # day of the signal candle
        reg = regimes["regime"].get(day, "DATA_UNCERTAIN")
        accepted = []
        for st_, s, idx, r, dr in batch:
            sig = {"trader": "T1", "version": VERSION, "variant": variant, "symbol": s, "signal_time": st, "regime": reg}
            tier = universe.tier(s, day)
            if tier is None or s not in universe.top(day, 150):
                signals.append({**sig, "decision": "NOT_TRADEABLE"}); continue
            if pd.isna(r["atr"]) or pd.isna(dr["ema50"]) or pd.isna(dr["adx"]):
                continue                                          # warm-up (INSUFFICIENT_HISTORY, not a signal)
            if not (dr["close"] > dr["ema50"] and dr["ema20"] > dr["ema50"]):
                continue                                          # not in setup (no log: not a signal)
            if variant != "C" and dr["adx"] < p["adx_min"]:
                continue
            rsd = rs.get(day)
            if rsd is None:
                signals.append({**sig, "decision": "ABSTAIN", "reason": "SMALL_UNIVERSE"}); continue
            pct = rsd.get(s)
            if pct is None or pct < 1 - p["rs_top"]:
                continue
            # DATA_ERROR only for candidates that are otherwise a signal (RC 10: a SIGNAL whose window has a gap)
            if not bool(r["complete_window"]):
                signals.append({**sig, "decision": "DATA_ERROR"}); continue
            if r["close_strength"] < p["close_strength"] or r["vol_ratio"] < p["vol_mult"]:
                signals.append({**sig, "decision": "WAIT",
                                "reason": "WEAK_CLOSE" if r["close_strength"] < p["close_strength"] else "LOW_VOLUME"}); continue
            reason = None
            if reg not in ALLOWED:
                reason = "REGIME_FORBIDDEN"
            elif macro is not None and macro.any_between(st, st + int(p["macro_before_h"] * 3_600_000)):
                reason = "MACRO_WINDOW"
            elif btc_ret.get(st, 0) <= p["btc_shock"]:
                reason = "BTC_SHOCK"
            elif r["ext_atr"] > p["max_ext_atr"]:
                reason = "OVEREXTENDED"
            stop = r["donchian_high"] - p["stop_atr_breakout"] * r["atr"]     # T1-v1.3 (trial T023)
            c = round_trip_cost(tier)
            row = {"vol_ratio": r["vol_ratio"], "rs_pct": pct, "base_range_atr": r["base_range_atr"],
                   "adx_d": dr["adx"], "close_strength": r["close_strength"]}
            entry = {**sig, "tier": tier, "stop": stop, "cost": c, "atr": r["atr"], "score": score(row),
                     "donchian_high": r["donchian_high"], "base_high": r["base_high"], "base_low": r["base_low"],
                     "rs_pct": round(pct, 4), "idx": idx}
            if reason:
                signals.append({**entry, "decision": "ABSTAIN", "reason": reason, "shadow": True}); continue
            accepted.append(entry)
        accepted.sort(key=lambda e: (-e["score"], -(universe.median_volume(e["symbol"], day) or 0), e["symbol"]))
        for e in accepted:
            s = e["symbol"]
            if open_until.get(s, 0) > st:
                continue                                          # one position per symbol
            if sum(1 for v in open_until.values() if v > st) >= p["max_slots"]:
                signals.append({**e, "decision": "ABSTAIN", "reason": "NO_SLOT"}); continue
            d = c4[s]
            pos = d.index.get_loc(e["idx"])
            nxt = d.iloc[pos + 1: pos + 1 + p["time_stop"] + 2]
            cs = [Candle(int(x.open_time), float(x.open), float(x.high), float(x.low), float(x.close), bool(x.complete))
                  for x in nxt.itertuples()]
            mult = (2, 4) if variant == "B" else (1.5, 3)
            dh, a = e["donchian_high"], e["atr"]
            spec = Spec(signal_time=st, stop=e["stop"], targets=[], target_fracs=[0.5, 0.5],
                        time_stop_candles=p["time_stop"], cost=e["cost"], slippage=SLIPPAGE_PER_SIDE[e["tier"]],
                        atr=a, targets_fn=lambda f, R, c=e["cost"], m=mult: [f + m[0] * R + c * f, f + m[1] * R + c * f],
                        failure_fn=lambda cd, stt, lvl=dh - 0.5 * a: cd.c < lvl)
            res = simulate_both(spec, cs, H4)
            rec = {**e, **{k: v for k, v in res.items() if k != "exits"}, "decision": "LONG"}
            rec.pop("idx", None)
            if res["status"] in ("CLOSED", "OPEN_AT_END"):
                rec["exit_time"] = res["exits"][-1][3] + H4
                rec["fill_time"] = res["fill_time"]
                open_until[s] = rec["exit_time"]
                trades.append(rec)
            signals.append(rec)
    return {"trades": trades, "signals": signals}
