"""
engine/t3.py - Playbook T3 (Mean Reversion), T3-v1.1.
Variants: A (as written), B (no confirmation candle), C (target = Bollinger mid only).
"""
from pathlib import Path

import numpy as np
import pandas as pd

from .execution import Spec, Candle, simulate_both
from .regime import wilder_adx
from .universe import round_trip_cost, SLIPPAGE_PER_SIDE

H1, H4, D1 = 3_600_000, 14_400_000, 86_400_000
VERSION = "T3-v1.1"
P = dict(bb_n=20, bb_k=2.0, rsi_max=30.0, vwap_atr=2.0, vol_mult=1.5, adx_max=25.0, confirm_window=3,
         knife_atr=0.5, stop_atr=0.3, min_rr=1.5, time_stop=24, macro_before_h=3.0, btc_shock=-0.05, max_slots=5)
ALLOWED = {"RANGE", "TRANSITION"}


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + up / dn)


def features(a: np.ndarray, p=P) -> pd.DataFrame:
    f = pd.DataFrame(a, columns=["t", "o", "h", "l", "c", "qv"])
    f["t"] = f["t"].astype(np.int64)
    c = f["c"]
    mid = c.rolling(p["bb_n"]).mean()
    sd = c.rolling(p["bb_n"]).std(ddof=0)
    f["bb_mid"], f["bb_low"] = mid, mid - p["bb_k"] * sd
    f["rsi"] = rsi(c)
    tr = pd.concat([f["h"] - f["l"], (f["h"] - c.shift()).abs(), (f["l"] - c.shift()).abs()], axis=1).max(axis=1)
    f["atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    tp = (f["h"] + f["l"] + c) / 3
    vol = f["qv"]
    f["vwap"] = (tp * vol).rolling(24).sum() / vol.rolling(24).sum()
    f["vwap_dist_atr"] = (f["vwap"] - c) / f["atr"]
    f["vol_ratio"] = vol / vol.shift(1).rolling(20).median()
    gap = (f["t"].diff() != H1).astype(int)
    f["gap_window"] = gap.rolling(30).max() > 0
    return f


def run(c1_dir, c4: dict, daily: dict, universe, regimes, macro, start, end, variant="A", p=P):
    t0 = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    t1 = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp() * 1000)
    btc = np.load(Path(c1_dir) / "BTCUSDT.npy")
    btc_close = pd.Series(btc[:, 4], index=btc[:, 0].astype(np.int64))
    btc24 = (btc_close / btc_close.shift(24) - 1)              # keyed by candle open; value at close of that candle
    syms = sorted({s for d in pd.date_range(start, end, freq="D") for s in universe.top(d.date().isoformat(), 150)})
    cands = []
    for s in syms:
        f_ = Path(c1_dir) / f"{s}.npy"
        if not f_.exists() or s not in daily:
            continue
        a = np.load(f_)
        f = features(a, p)
        d1 = daily[s]
        low30 = d1["low"].shift(1).rolling(30).min()             # previous 30 completed days, keyed by day open
        low30_map = dict(zip((d1["open_time"] // D1).astype(np.int64), low30))
        adx4 = None
        if s in c4:
            x = c4[s]
            adx4 = pd.Series(wilder_adx(x).to_numpy(), index=(x["open_time"] + H4).to_numpy())  # by 4H close time
        setup = (f["c"] < f["bb_low"]) & (f["rsi"] <= p["rsi_max"]) & (f["vwap_dist_atr"] >= p["vwap_atr"]) & \
                (f["vol_ratio"] >= p["vol_mult"]) & (f["t"] + H1 >= t0) & (f["t"] + H1 < t1)
        idxs = np.nonzero(setup.to_numpy())[0]
        if len(idxs) == 0:
            continue
        o, h, l, c = f["o"].to_numpy(), f["h"].to_numpy(), f["l"].to_numpy(), f["c"].to_numpy()
        bbl = f["bb_low"].to_numpy()
        active_until = -1
        for k in idxs:
            if k <= active_until:                                # a newer setup replaces the older one: handled in order
                pass
            st_setup = int(f["t"].iat[k] + H1)
            lo30 = low30_map.get(int(f["t"].iat[k] // D1))
            rec = {"symbol": s, "setup_time": st_setup, "setup_low": l[k], "atr_setup": f["atr"].iat[k],
                   "vwap_at_setup": f["vwap"].iat[k], "bb_mid_at_setup": f["bb_mid"].iat[k], "rsi": f["rsi"].iat[k],
                   "vwap_dist_atr": f["vwap_dist_atr"].iat[k], "vol_ratio": f["vol_ratio"].iat[k],
                   "gap": bool(f["gap_window"].iat[k]), "new_30d_low": lo30 is not None and not np.isnan(lo30) and l[k] <= lo30}
            if adx4 is not None:
                kk = np.searchsorted(adx4.index.to_numpy(), st_setup, side="right") - 1
                rec["adx4"] = float(adx4.iat[kk]) if kk >= 0 else np.nan
            else:
                rec["adx4"] = np.nan
            b = btc24.get(int(f["t"].iat[k]))
            rec["btc24"] = float(b) if b is not None and not np.isnan(b) else 0.0
            rec["macro"] = bool(macro and macro.any_between(int(f["t"].iat[k]) - 3 * H1, st_setup))
            # confirmation (variant B: none)
            if variant == "B":
                rec.update(signal_time=st_setup, sig_idx=k, stop_base=l[k])
                cands.append(rec)
                continue
            status, low_run = None, l[k]
            for m in range(k + 1, min(k + 1 + p["confirm_window"], len(c))):
                if setup.iat[m]:
                    status = "REPLACED"
                    break
                if l[m] < l[k] - p["knife_atr"] * rec["atr_setup"]:
                    status = "KNIFE"
                    break
                low_run = min(low_run, l[m])
                if c[m] > bbl[m] and c[m] > o[m]:
                    status = "OK"
                    rec.update(signal_time=int(f["t"].iat[m] + H1), sig_idx=m, stop_base=low_run)
                    break
            if status == "OK":
                cands.append(rec)
            elif status in ("KNIFE", None):
                cands.append({**rec, "signal_time": st_setup, "invalid": status or "NO_CONFIRMATION"})
    cands.sort(key=lambda x: (x["signal_time"], x["symbol"]))
    signals, trades, open_until, arrays = [], [], {}, {}
    for e in cands:
        st, s = e["signal_time"], e["symbol"]
        day = pd.Timestamp(e["setup_time"] - 1, unit="ms", tz="UTC").date().isoformat()
        reg = regimes["regime"].get(day, "DATA_UNCERTAIN")
        base = {"trader": "T3", "version": VERSION, "variant": variant, "symbol": s, "signal_time": st, "regime": reg}
        tier = universe.tier(s, day)
        if tier is None or s not in universe.top(day, 150):
            signals.append({**base, "decision": "NOT_TRADEABLE"}); continue
        if e["gap"]:
            signals.append({**base, "decision": "DATA_ERROR"}); continue
        reason = None
        if reg not in ALLOWED:
            reason = "REGIME_FORBIDDEN"
        elif np.isnan(e["adx4"]) or e["adx4"] >= p["adx_max"]:
            reason = "TRENDING"
        elif e["macro"]:
            reason = "MACRO_WINDOW"
        elif e["btc24"] <= p["btc_shock"]:
            reason = "MARKET_SHOCK"
        elif e["new_30d_low"]:
            reason = "NEW_30D_LOW"
        if reason:
            signals.append({**base, "decision": "ABSTAIN", "reason": reason}); continue
        if e.get("invalid"):
            signals.append({**base, "decision": "INVALID", "reason": e["invalid"]}); continue
        c = round_trip_cost(tier)
        target = e["bb_mid_at_setup"] if variant == "C" else min(e["vwap_at_setup"], e["bb_mid_at_setup"])
        stop = e["stop_base"] - p["stop_atr"] * e["atr_setup"]
        rec = {**base, "tier": tier, "cost": c, "stop": stop, "target": target, "rsi": e["rsi"], "vwap_dist_atr": e["vwap_dist_atr"]}
        if open_until.get(s, 0) > st:
            continue
        if sum(1 for v in open_until.values() if v > st) >= p["max_slots"]:
            signals.append({**rec, "decision": "ABSTAIN", "reason": "NO_SLOT"}); continue
        if s not in arrays:
            arrays[s] = np.load(Path(c1_dir) / f"{s}.npy")
        a = arrays[s]
        k = np.searchsorted(a[:, 0], st)
        seg = a[k:k + p["time_stop"] + 2]
        cs = [Candle(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in seg]
        spec = Spec(signal_time=st, stop=stop, targets=[target], target_fracs=[1.0], time_stop_candles=p["time_stop"],
                    cost=c, slippage=SLIPPAGE_PER_SIDE[tier], atr=e["atr_setup"], breakeven_after_t1=False,
                    min_rr_net=p["min_rr"])
        res = simulate_both(spec, cs, H1)
        out = {**rec, **{kk: v for kk, v in res.items() if kk != "exits"}, "decision": "LONG"}
        if res["status"] in ("CLOSED", "OPEN_AT_END"):
            out["exit_time"] = res["exits"][-1][3] + H1
            open_until[s] = out["exit_time"]
            trades.append(out)
        signals.append(out)
    return {"trades": trades, "signals": signals}
