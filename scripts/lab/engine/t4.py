"""
engine/t4.py - Playbook T4 (Derivatives & Positioning, deleveraging proxy), T4-v1.1.
Variants: A (as written), B (no confirmation), C (T2 fixed at 3R).
OI / long-short samples: the last 5-minute sample in the 15 minutes before a time t;
funding: the last settlement strictly before t.
"""
import numpy as np
import pandas as pd

from .execution import Spec, Candle, simulate_both
from .universe import round_trip_cost, SLIPPAGE_PER_SIDE

H4, D1, M15 = 14_400_000, 86_400_000, 900_000
VERSION = "T4-v1.1"
P = dict(oi_drop=-0.08, px_drop=-0.06, funding_max=0.00005, ls_ratio=0.90, confirm_window=3,
         taker_min=1.0, oi_candle_min=-0.01, stop_atr=0.3, cap_r=4.0, fallback_r=3.0, time_stop=18,
         macro_before_h=4.0, macro_after_h=2.0, max_slots=5)


def sampler(met: pd.DataFrame, col: str):
    ts = met["sample_time"].to_numpy(np.int64)
    v = met[col].to_numpy(float)

    def at(t):
        k = np.searchsorted(ts, t, side="left") - 1          # strictly before t
        if k < 0 or ts[k] < t - M15:
            return None
        return v[k]
    return at


def run(c4: dict, daily: dict, fut: dict, universe, regimes, macro, start, end, variant="A", p=P):
    t0 = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    t1 = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp() * 1000)
    syms = sorted({s for d in pd.date_range(start, end, freq="D") for s in universe.top(d.date().isoformat(), 150)})
    cands = []
    for s in syms:
        if s not in c4 or s not in fut["metrics"] or s not in daily:
            continue
        x = c4[s]
        met = fut["metrics"][s]
        oi_at = sampler(met, "sum_open_interest")
        ls_at = sampler(met, "count_long_short_ratio")
        fd = fut["funding"].get(s)
        f_ts = fd["funding_time"].to_numpy(np.int64) if fd is not None else np.array([], np.int64)
        f_v = fd["funding_rate"].to_numpy(float) if fd is not None else np.array([])
        d1 = daily[s]
        ema200 = d1["close"].ewm(span=200, adjust=False, min_periods=200).mean()
        d_close_t = (d1["open_time"] + D1).to_numpy(np.int64)
        o, h, l, c = (x[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        qv, tbq = x["quote_volume"].to_numpy(float), x["taker_buy_quote"].to_numpy(float)
        ot = x["open_time"].to_numpy(np.int64)
        comp = x["complete"].to_numpy(bool)
        tr = np.maximum(h - l, np.maximum(np.abs(h - np.r_[c[0], c[:-1]]), np.abs(l - np.r_[c[0], c[:-1]])))
        atr = pd.Series(tr).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean().to_numpy()
        n = len(c)
        i = 7
        while i < n:
            t = ot[i] + H4                                       # close time of candle i = setup time
            if t < t0 or t >= t1 or np.isnan(atr[i]):
                i += 1
                continue
            oi_now, oi_prev = oi_at(t), oi_at(t - D1)
            ls_now, ls_prev = ls_at(t), ls_at(t - D1)
            kf = np.searchsorted(f_ts, t, side="left") - 1
            fund_v = f_v[kf] if kf >= 0 else None
            px_prev = c[i - 6]
            if None in (oi_now, oi_prev, ls_now, ls_prev, fund_v) or not comp[i - 6:i + 1].all() \
                    or oi_prev <= 0 or px_prev <= 0:
                if oi_now is not None or oi_prev is not None:
                    pass
                i += 1
                continue
            setup = (oi_now / oi_prev - 1 <= p["oi_drop"] and c[i] / px_prev - 1 <= p["px_drop"]
                     and fund_v <= p["funding_max"] and ls_prev > 0 and ls_now <= p["ls_ratio"] * ls_prev)
            if not setup:
                i += 1
                continue
            kd = np.searchsorted(d_close_t, t, side="right") - 1
            rec = {"symbol": s, "setup_time": int(t), "oi_change": oi_now / oi_prev - 1, "px_change": c[i] / px_prev - 1,
                   "funding": fund_v, "ls_change": ls_now / ls_prev, "flush_low": l[i - 5:i + 1].min(),
                   "ref_close": px_prev, "atr": atr[i],
                   "downtrend": not (kd >= 0 and not np.isnan(ema200.iat[kd]) and d1["close"].iat[kd] > ema200.iat[kd]),
                   "macro": bool(macro and macro.any_between(int(t) - int(p["macro_before_h"] * 3_600_000),
                                                             int(t) + int(p["macro_after_h"] * 3_600_000)))}
            if variant == "B":
                cands.append({**rec, "signal_time": int(t), "sig_i": i, "taker": None})
                i += 1
                continue
            status = None
            for m in range(i + 1, min(i + 1 + p["confirm_window"], n)):
                tm = ot[m] + H4
                if c[m] < rec["flush_low"]:
                    status = "FLUSH_CONTINUES"
                    break
                oi_o, oi_c = oi_at(ot[m]), oi_at(tm)
                taker = tbq[m] / (qv[m] - tbq[m]) if qv[m] > tbq[m] else 0
                if c[m] > o[m] and taker >= p["taker_min"] and oi_o and oi_c and oi_c / oi_o - 1 >= p["oi_candle_min"]:
                    status = "OK"
                    cands.append({**rec, "signal_time": int(tm), "sig_i": m, "taker": taker})
                    break
            if status != "OK":
                cands.append({**rec, "signal_time": int(t), "invalid": status or "NO_CONFIRMATION"})
            i += 1
    cands.sort(key=lambda e: (e["signal_time"], e["symbol"]))
    signals, trades, open_until = [], [], {}
    for e in cands:
        st, s = e["signal_time"], e["symbol"]
        day = pd.Timestamp(e["setup_time"] - 1, unit="ms", tz="UTC").date().isoformat()
        reg = regimes["regime"].get(day, "DATA_UNCERTAIN")
        base = {"trader": "T4", "version": VERSION, "variant": variant, "symbol": s, "signal_time": st, "regime": reg}
        tier = universe.tier(s, day)
        if tier is None or s not in universe.top(day, 150):
            signals.append({**base, "decision": "NOT_TRADEABLE"}); continue
        reason = "REGIME_FORBIDDEN" if reg == "DATA_UNCERTAIN" else "LONG_DOWNTREND" if e["downtrend"] else \
            "MACRO_WINDOW" if e["macro"] else None
        if reason:
            signals.append({**base, "decision": "ABSTAIN", "reason": reason}); continue
        if e.get("invalid"):
            signals.append({**base, "decision": "INVALID", "reason": e["invalid"]}); continue
        c = round_trip_cost(tier)
        stop = e["flush_low"] - p["stop_atr"] * e["atr"]
        rec = {**base, "tier": tier, "cost": c, "stop": stop, "ref_close": e["ref_close"],
               "oi_change": round(e["oi_change"], 4), "px_change": round(e["px_change"], 4)}
        if open_until.get(s, 0) > st:
            continue
        if sum(1 for v in open_until.values() if v > st) >= p["max_slots"]:
            signals.append({**rec, "decision": "ABSTAIN", "reason": "NO_SLOT"}); continue
        x = c4[s]
        k = int(np.searchsorted(x["open_time"].to_numpy(np.int64), st))
        seg = x.iloc[k:k + p["time_stop"] + 2]
        cs = [Candle(int(r.open_time), float(r.open), float(r.high), float(r.low), float(r.close), bool(r.complete))
              for r in seg.itertuples()]
        ref = e["ref_close"]

        def tfn(fill, R, c=c, ref=ref):
            T1 = fill + 1.5 * R + c * fill
            if variant == "C":
                return [T1, fill + p["fallback_r"] * R]
            T2 = min(ref, fill + p["cap_r"] * R)
            return [T1, T2 if T2 > T1 else fill + p["fallback_r"] * R]
        spec = Spec(signal_time=st, stop=stop, targets=[], target_fracs=[0.5, 0.5], time_stop_candles=p["time_stop"],
                    cost=c, slippage=SLIPPAGE_PER_SIDE[tier], atr=e["atr"], targets_fn=tfn)
        res = simulate_both(spec, cs, H4)
        out = {**rec, **{kk: v for kk, v in res.items() if kk != "exits"}, "decision": "LONG"}
        if res["status"] in ("CLOSED", "OPEN_AT_END"):
            out["exit_time"] = res["exits"][-1][3] + H4
            open_until[s] = out["exit_time"]
            trades.append(out)
        signals.append(out)
    return {"trades": trades, "signals": signals}
