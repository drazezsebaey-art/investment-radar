"""
engine/t2.py - Playbook T2 (Liquidity & Structure / SMC-ICT inspired), T2-v1.1.
Variants: A (as written), B (entry at FVG top), C (no killzone filter).

Point-in-time rules: a swing at candle j (L=3) is usable only from candle j+4 on
(confirmed after the close of j+3); the pool is frozen when the sweep candle opens;
every FVG candle lies between the sweep candle and the CHoCH candle.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .execution import Spec, Candle, simulate_both, check_stop
from .universe import round_trip_cost, SLIPPAGE_PER_SIDE

H1, H4 = 3_600_000, 14_400_000
VERSION = "T2-v1.1"
NY = ZoneInfo("America/New_York")
P = dict(L=3, pool_lookback=48, eq_atr=0.1, sweep_atr=0.1, disp_body_atr=1.0, disp_body_range=0.6,
         disp_window=6, fvg_atr=0.25, stop_atr=0.2, limit_valid=6, time_stop=48, bias_window=30,
         fail_atr=0.25, max_slots=5, macro_before_h=1.0, macro_after_h=1.0, cap_r=5.0, fallback_r=3.0)
ALLOWED = {"TREND_UP", "TRANSITION", "RANGE"}


def atr_np(h, l, c, n=14):
    tr = np.maximum(h - l, np.maximum(np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1))))
    tr[0] = h[0] - l[0]
    out = np.full(len(tr), np.nan)
    a = 1 / n
    acc = np.nan
    for i, x in enumerate(tr):
        acc = x if np.isnan(acc) else acc + a * (x - acc)
        if i >= n - 1:
            out[i] = acc
    return out


def swings(h, l, L=3):
    """-> (is_high, is_low) boolean arrays at the swing candle itself."""
    n = len(h)
    sh = np.zeros(n, bool)
    sl = np.zeros(n, bool)
    for i in range(L, n - L):
        if h[i] > h[i - L:i].max() and h[i] > h[i + 1:i + L + 1].max():
            sh[i] = True
        if l[i] < l[i - L:i].min() and l[i] < l[i + 1:i + L + 1].min():
            sl[i] = True
    return sh, sl


def in_killzone(t_ms: int) -> str:
    local = datetime.fromtimestamp(t_ms / 1000, timezone.utc).astimezone(NY)
    hm = local.hour + local.minute / 60
    if 2 <= hm < 5:
        return "LONDON"
    if 7 <= hm < 10:
        return "NEW_YORK"
    return ""


def bias_4h(c4: pd.DataFrame, L=3, window=30):
    """-> (signal_time_ms array, bullish bool array): bias known at each 4H close.
    Event = close above the last confirmed swing high (bull) / below the last
    confirmed swing low (bear). Bullish bias if the last event (within `window`
    candles) is bullish."""
    h, l, c = c4["high"].to_numpy(), c4["low"].to_numpy(), c4["close"].to_numpy()
    sh, sl = swings(h, l, L)
    n = len(c)
    last_hi = last_lo = None
    last_event, last_event_i = None, -10 ** 9
    bull = np.zeros(n, bool)
    for i in range(n):
        j = i - (L + 1)                      # swing at j confirmed after close of j+3 -> usable at i = j+4
        if j >= 0:
            if sh[j]:
                last_hi = h[j]
            if sl[j]:
                last_lo = l[j]
        if last_hi is not None and c[i] > last_hi:
            last_event, last_event_i = "bull", i
            last_hi = None                   # a level is broken once
        elif last_lo is not None and c[i] < last_lo:
            last_event, last_event_i = "bear", i
            last_lo = None
        bull[i] = last_event == "bull" and i - last_event_i < window
    return (c4["open_time"].to_numpy() + H4), bull


def score(pool_n, sweep_depth, disp, gap, fresh):
    def b(x, edges):
        pts = 0
        for k, e in enumerate(edges):
            if x >= e:
                pts = 5 * (k + 1)
        return pts
    s = 5 if pool_n <= 1 else (15 if pool_n == 2 else 20)
    s += b(sweep_depth, [0.1, 0.3, 0.6, 1.0])
    s += b(disp, [1.0, 1.5, 2.0, 3.0])
    s += b(gap, [0.25, 0.5, 1.0, 1.5])
    s += 20 if fresh < 5 else 15 if fresh < 10 else 10 if fresh <= 20 else 5
    return s


def find_signals(sym, a, c4, t0, t1, variant, p=P):
    """a: 1H array [open_time, o, h, l, c, qv]. Returns candidate signal dicts."""
    t = a[:, 0].astype(np.int64)
    o, h, l, c = a[:, 1], a[:, 2], a[:, 3], a[:, 4]
    n = len(t)
    if n < 200:
        return []
    at = atr_np(h, l, c)
    sh, sl = swings(h, l, p["L"])
    gap_prev = np.r_[False, np.diff(t) != H1]               # a missing hour before candle i
    btimes, bbull = bias_4h(c4, p["L"], p["bias_window"]) if c4 is not None else (np.array([]), np.array([]))
    b4h = c4["high"].to_numpy() if c4 is not None else None
    sh4, _ = swings(c4["high"].to_numpy(), c4["low"].to_numpy(), p["L"]) if c4 is not None else (None, None)
    t4 = c4["open_time"].to_numpy() if c4 is not None else None
    sh4_idx = np.nonzero(sh4)[0] if c4 is not None else []
    L = p["L"]
    out = []
    conf_hi, conf_lo = [], []                                # (index, price) confirmed swings, in order
    pending = None                                           # active sweep waiting for displacement
    for i in range(n):
        j = i - (L + 1)
        if j >= 0:
            if sh[j]:
                conf_hi.append((j, h[j]))
            if sl[j]:
                conf_lo.append((j, l[j]))
        if t[i] + H1 <= t0 - 30 * 86_400_000 or t[i] >= t1:
            continue
        if np.isnan(at[i - 1] if i else np.nan):
            continue
        A = at[i - 1]                                        # ATR known at the open of candle i
        # -- displacement / CHoCH for an active sweep
        if pending is not None:
            k0 = pending["i"]
            if i - k0 >= p["disp_window"]:
                pending = None
            else:
                body = abs(c[i] - o[i])
                rng = h[i] - l[i]
                if c[i] > o[i] and body >= p["disp_body_atr"] * pending["atr"] and rng > 0 and body / rng >= p["disp_body_range"] \
                        and pending["choch_level"] is not None and c[i] > pending["choch_level"]:
                    fv = None
                    for m in range(k0 + 2, i + 1):                # n-2 >= sweep candle, n <= CHoCH candle
                        g = l[m] - h[m - 2]
                        if g >= p["fvg_atr"] * pending["atr"]:
                            if fv is None or l[m] > fv[0]:
                                fv = (l[m], h[m - 2], g)          # (top, bottom, size)
                    sig_t = int(t[i] + H1)
                    if t0 <= sig_t < t1:
                        rec = {"symbol": sym, "signal_time": sig_t, "sweep_time": int(t[k0]), **pending, "choch_i": i}
                        if fv is None:
                            rec["decision"], rec["reason"] = "ABSTAIN", "NO_FVG"
                        else:
                            rec.update(fvg_top=fv[0], fvg_bottom=fv[1], fvg_size_atr=fv[2] / pending["atr"],
                                       disp_body_atr=body / pending["atr"])
                        # 4H bias known at the signal time
                        kk = np.searchsorted(btimes, sig_t, side="right") - 1
                        rec["bias_bull"] = bool(kk >= 0 and bbull[kk])
                        rec["bias_fresh"] = 99
                        if kk >= 0 and bbull[kk]:
                            back = kk
                            while back > 0 and bbull[back - 1]:
                                back -= 1
                            rec["bias_fresh"] = kk - back
                        # 4H confirmed swing highs as of signal time (for T2 frozen later at fill)
                        rec["swing4h_highs"] = [(int(t4[q] + (L + 1) * H4), float(b4h[q]))           # usable from candle q+4
                                                for q in sh4_idx if t4[q] + (L + 1) * H4 <= sig_t + 7 * H1]
                        rec["gap_in_window"] = bool(gap_prev[max(0, k0 - p["pool_lookback"]):i + 1].any())
                        out.append(rec)
                    pending = None
                    continue
        # -- sweep detection on candle i (pool frozen at its open)
        recent_lo = [(jj, v) for jj, v in conf_lo if jj >= i - p["pool_lookback"]]
        recent_hi = [(jj, v) for jj, v in conf_hi if jj >= i - p["pool_lookback"]]
        if not recent_lo or i == 0:
            continue
        below = [(jj, v) for jj, v in recent_lo if v < c[i - 1]]
        if not below:
            continue
        anchor = max(below, key=lambda x: x[1])[1]          # nearest confirmed low below the previous close
        group = [v for _, v in recent_lo if abs(v - anchor) <= p["eq_atr"] * A]
        level = min(group)
        if l[i] < level - p["sweep_atr"] * A and c[i] > level:
            kz = in_killzone(int(t[i]))
            if variant != "C" and not kz:
                continue                                    # counted as OUTSIDE_KILLZONE below
            choch = [v for jj, v in conf_hi]                # last confirmed 1H swing high before the sweep
            pending = {"i": i, "pool_level": level, "pool_n": len(group), "sweep_low": l[i],
                       "sweep_depth_atr": (level - l[i]) / A, "atr": A, "killzone": kz or "NONE",
                       "choch_level": choch[-1] if choch else None,
                       "ambiguous_structure": len(recent_hi) < 2 or len(recent_lo) < 2}
    return out


def run(c1_dir, c4: dict, universe, regimes, macro, start, end, variant="A", p=P):
    from pathlib import Path
    t0 = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    t1 = int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp() * 1000)
    blackout = macro.windows(p["macro_before_h"], p["macro_after_h"]) if macro else []
    cands = []
    syms = sorted({s for d in pd.date_range(start, end, freq="D") for s in universe.top(d.date().isoformat(), 150)})
    for s in syms:
        f = Path(c1_dir) / f"{s}.npy"
        if not f.exists():
            continue
        a = np.load(f)
        cands += find_signals(s, a, c4.get(s), t0, t1, variant, p)
    cands.sort(key=lambda x: (x["signal_time"], x["symbol"]))
    signals, trades, open_until = [], [], {}
    arrays = {}
    for e in cands:
        st, s = e["signal_time"], e["symbol"]
        day = pd.Timestamp(st - 1, unit="ms", tz="UTC").date().isoformat()
        reg = regimes["regime"].get(day, "DATA_UNCERTAIN")
        base = {"trader": "T2", "version": VERSION, "variant": variant, "symbol": s, "signal_time": st, "regime": reg,
                "killzone": e.get("killzone")}
        tier = universe.tier(s, day)
        if tier is None or s not in universe.top(day, 150):
            signals.append({**base, "decision": "NOT_TRADEABLE"}); continue
        if e.get("decision") == "ABSTAIN":
            signals.append({**base, "decision": "ABSTAIN", "reason": e["reason"]}); continue
        if e["gap_in_window"]:
            signals.append({**base, "decision": "DATA_ERROR"}); continue
        reason = None
        if reg not in ALLOWED:
            reason = "REGIME_FORBIDDEN"
        elif not e["bias_bull"]:
            reason = "NO_4H_BIAS"
        elif e["ambiguous_structure"]:
            reason = "STRUCTURE_AMBIGUOUS"
        c = round_trip_cost(tier)
        A = e["atr"]
        limit = e["fvg_top"] if variant == "B" else (e["fvg_top"] + e["fvg_bottom"]) / 2
        stop = e["sweep_low"] - p["stop_atr"] * A
        if reason is None and check_stop(limit, stop, A, c):
            reason = "STOP_" + check_stop(limit, stop, A, c).split("_")[-1].upper()
        sc = score(e["pool_n"], e["sweep_depth_atr"], e["disp_body_atr"], e["fvg_size_atr"], e["bias_fresh"])
        rec = {**base, "tier": tier, "cost": c, "atr": A, "limit": limit, "stop": stop, "score": sc,
               "pool_level": e["pool_level"], "pool_n": e["pool_n"], "sweep_low": e["sweep_low"],
               "fvg_top": e["fvg_top"], "fvg_bottom": e["fvg_bottom"]}
        if reason:
            signals.append({**rec, "decision": "ABSTAIN", "reason": reason}); continue
        if open_until.get(s, 0) > st:
            continue
        if sum(1 for v in open_until.values() if v > st) >= p["max_slots"]:
            signals.append({**rec, "decision": "ABSTAIN", "reason": "NO_SLOT"}); continue
        if s not in arrays:
            arrays[s] = np.load(Path(c1_dir) / f"{s}.npy")
        a = arrays[s]
        k = np.searchsorted(a[:, 0], st)                        # first candle opening at/after the signal
        seg = a[k:k + p["limit_valid"] + p["time_stop"] + 2]
        cs = [Candle(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in seg]
        R0 = limit - stop
        t1_est = limit + 1.5 * R0 + c * limit
        sw = e["swing4h_highs"]

        def tfn(fill, R, fill_t, c=c, sw=sw):
            T1 = fill + 1.5 * R + c * fill
            highs = [v for (known_t, v) in sw if known_t <= fill_t and v > T1]
            cap = fill + p["cap_r"] * R
            T2 = min(highs) if highs and min(highs) <= cap else fill + p["fallback_r"] * R
            return [T1, T2]
        spec = Spec(signal_time=st, stop=stop, targets=[], target_fracs=[0.5, 0.5], time_stop_candles=p["time_stop"],
                    cost=c, slippage=SLIPPAGE_PER_SIDE[tier], atr=A, entry="limit", limit_price=limit,
                    limit_valid_candles=p["limit_valid"], targets_fn=tfn,
                    failure_fn=lambda cd, stt, lvl=e["fvg_bottom"] - p["fail_atr"] * A: cd.c < lvl,
                    cancel_close_below=e["sweep_low"], cancel_if_high_reaches=t1_est, blackout=blackout)
        res = simulate_both(spec, cs, H1)
        out = {**rec, **{kk: v for kk, v in res.items() if kk != "exits"}, "decision": "LONG"}
        if res["status"] in ("CLOSED", "OPEN_AT_END"):
            out["exit_time"] = res["exits"][-1][3] + H1
            open_until[s] = out["exit_time"]
            trades.append(out)
        signals.append(out)
    return {"trades": trades, "signals": signals}
