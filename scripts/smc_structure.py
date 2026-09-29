"""
smc_structure.py (v65.2 -> v65.4) - market structure engine aligned line-by-line
with LuxAlgo's "Smart Money Concepts" Pine source (library slug
smart-money-concepts-smc, fetched 28/9/2026 via the LuxAlgo MCP).

LICENSE: the original indicator is (c) LuxAlgo under CC BY-NC-SA 4.0
(https://creativecommons.org/licenses/by-nc-sa/4.0/). This file is an adapted
Python port of its structure / order-block / trailing-extreme logic and is
therefore distributed under the SAME licence: attribution to LuxAlgo,
non-commercial use only, share-alike. Personal research use.

v65.4 alignment with the source (differences found vs v65.2 and fixed):
  1. leg() starts at BEARISH_LEG (0): the first registered pivot is always a LOW.
  2. Order-block search window is [pivot bar, break bar) - the break bar is excluded
     (Pine: parsedLows.slice(pivot.barIndex, bar_index)).
  3. Volatility filter uses Wilder ATR(200) (ta.atr = RMA) and is inactive until
     200 bars exist (na in Pine).
  4. Internal breaks at the swing level are skipped, NOT consumed - the internal
     level stays live (Pine only adds `internalHigh != swingHigh` as a condition).
  5. Evaluation order matches Pine: pivots swing -> internal, breaks internal -> swing.
  6. Trailing extremes + Strong/Weak High/Low labels and the indicator's
     premium/discount geometry (equilibrium = mid of trailing top/bottom) added.
Confluence filter: off (indicator default and Azez's setting).
"""

INTERNAL_LEN = 5
SWING_LEN = 50
OB_ATR_LEN = 200
OB_VOL_MULT = 2.0
MAX_OBS = 5


def _atr(c, n, strict=False):
    """Wilder ATR (ta.atr = RMA of true range). strict=True returns None until n
    bars exist (Pine's na); otherwise a running mean is used before that point."""
    out, trs, rma = [], [], None
    for i, x in enumerate(c):
        tr = (x["h"] - x["l"]) if i == 0 else max(x["h"] - x["l"], abs(x["h"] - c[i - 1]["c"]),
                                                  abs(x["l"] - c[i - 1]["c"]))
        trs.append(tr)
        if len(trs) < n:
            out.append(None if strict else sum(trs) / len(trs))
            continue
        rma = sum(trs[-n:]) / n if rma is None else (rma * (n - 1) + tr) / n
        out.append(rma)
    return out


def _parsed(c):
    atr = _atr(c, OB_ATR_LEN, strict=True)
    ph, pl = [], []
    for i, x in enumerate(c):
        if atr[i] is not None and (x["h"] - x["l"]) >= OB_VOL_MULT * atr[i]:
            ph.append(x["l"]); pl.append(x["h"])
        else:
            ph.append(x["h"]); pl.append(x["l"])
    return ph, pl


def analyse(c, internal_len=INTERNAL_LEN, swing_len=SWING_LEN):
    """c: oldest-first candles {ts,o,h,l,c}. Returns events, trends, levels, live OBs."""
    ph, pl = _parsed(c)
    layers = {}
    for name, ln in (("internal", internal_len), ("swing", swing_len)):
        layers[name] = {"len": ln, "os": 0, "top": None, "top_i": None, "top_crossed": True,
                        "btm": None, "btm_i": None, "btm_crossed": True, "trend": 0}
    events, bull_obs, bear_obs = [], [], []
    trail = {"top": None, "top_ts": None, "bottom": None, "bottom_ts": None}

    for i in range(len(c)):
        # trailing extremes (Pine updateTrailingExtremes runs first each bar)
        if trail["top"] is None or c[i]["h"] >= trail["top"]:
            trail["top"], trail["top_ts"] = c[i]["h"], c[i]["ts"]
        if trail["bottom"] is None or c[i]["l"] <= trail["bottom"]:
            trail["bottom"], trail["bottom_ts"] = c[i]["l"], c[i]["ts"]
        # 1) swing detection (right-side confirmation, alternating)
        for L in (layers["swing"], layers["internal"]):
            n = L["len"]
            if i < n:
                continue
            k = i - n
            upper = max(x["h"] for x in c[k + 1:i + 1])
            lower = min(x["l"] for x in c[k + 1:i + 1])
            prev = L["os"]
            if c[k]["h"] > upper:
                L["os"] = 0
            elif c[k]["l"] < lower:
                L["os"] = 1
            if L["os"] == 0 and prev != 0:
                L.update(top=c[k]["h"], top_i=k, top_crossed=False)
                if L is layers["swing"]:
                    trail["top"], trail["top_ts"] = c[k]["h"], c[k]["ts"]
            if L["os"] == 1 and prev != 1:
                L.update(btm=c[k]["l"], btm_i=k, btm_crossed=False)
                if L is layers["swing"]:
                    trail["bottom"], trail["bottom_ts"] = c[k]["l"], c[k]["ts"]

        # 2) structure breaks (close cross), swing layer first so internal can defer
        prev_close = c[i - 1]["c"] if i else c[i]["c"]
        for name in ("internal", "swing"):
            L = layers[name]
            S = layers["swing"]
            if L["top"] is not None and not L["top_crossed"] and prev_close <= L["top"] < c[i]["c"]:
                if name == "internal" and S["top"] is not None and abs(L["top"] - S["top"]) < 1e-12:
                    pass                      # Pine: condition false this bar, level NOT consumed
                else:
                    kind = "CHoCH" if L["trend"] < 0 else "BOS"
                    events.append({"i": i, "ts": c[i]["ts"], "layer": name, "type": kind, "dir": "bull",
                                   "level": L["top"]})
                    L["trend"], L["top_crossed"] = 1, True
                    seg = range(L["top_i"], max(i, L["top_i"] + 1))   # [pivot, break) like Pine slice
                    j = min(seg, key=lambda t: pl[t])
                    bull_obs.insert(0, {"top": ph[j], "bottom": pl[j], "i": j, "ts": c[j]["ts"], "layer": name})
            if L["btm"] is not None and not L["btm_crossed"] and prev_close >= L["btm"] > c[i]["c"]:
                if name == "internal" and S["btm"] is not None and abs(L["btm"] - S["btm"]) < 1e-12:
                    pass
                else:
                    kind = "CHoCH" if L["trend"] > 0 else "BOS"
                    events.append({"i": i, "ts": c[i]["ts"], "layer": name, "type": kind, "dir": "bear",
                                   "level": L["btm"]})
                    L["trend"], L["btm_crossed"] = -1, True
                    seg = range(L["btm_i"], max(i, L["btm_i"] + 1))
                    j = max(seg, key=lambda t: ph[t])
                    bear_obs.insert(0, {"top": ph[j], "bottom": pl[j], "i": j, "ts": c[j]["ts"], "layer": name})

        # 3) mitigation (High/Low)
        bull_obs = [o for o in bull_obs if not (c[i]["l"] < o["bottom"] and i > o["i"])][:MAX_OBS * 2]
        bear_obs = [o for o in bear_obs if not (c[i]["h"] > o["top"] and i > o["i"])][:MAX_OBS * 2]

    sw = layers["swing"]["trend"]
    eq = (trail["top"] + trail["bottom"]) / 2 if trail["top"] is not None and trail["bottom"] is not None else None
    return {"events": events,
            "trailing": {"top": trail["top"], "top_label": "Strong High" if sw == -1 else "Weak High",
                         "bottom": trail["bottom"], "bottom_label": "Strong Low" if sw == 1 else "Weak Low",
                         "equilibrium": eq},
            "trend": {k: v["trend"] for k, v in layers.items()},
            "levels": {k: {"top": v["top"], "top_broken": v["top_crossed"], "bottom": v["btm"],
                           "bottom_broken": v["btm_crossed"]} for k, v in layers.items()},
            "bull_obs": bull_obs[:MAX_OBS], "bear_obs": bear_obs[:MAX_OBS]}


def post_peak_read(c, peak_ts):
    """What the structure says since the impulse peak (for the correction monitor)."""
    if len(c) < 2 * INTERNAL_LEN + 3:
        return {"choch_up": False, "reason": "too few 4H candles"}
    a = analyse(c)
    after = [e for e in a["events"] if e["ts"] >= peak_ts]
    internal_after = [e for e in after if e["layer"] == "internal"]
    last_int = internal_after[-1] if internal_after else None
    price = c[-1]["c"]
    below = [o for o in a["bull_obs"] if o["top"] <= price * 1.001]
    above = [o for o in a["bear_obs"] if o["bottom"] >= price * 0.999]
    return {
        # RESUMING needs the LATEST internal event after the peak to be a bullish CHoCH
        "choch_up": bool(last_int and last_int["dir"] == "bull" and last_int["type"] == "CHoCH"),
        "last_internal_event": ({k: last_int[k] for k in ("type", "dir", "level", "ts")} if last_int else None),
        "internal_trend": a["trend"]["internal"], "swing_trend": a["trend"]["swing"],
        "nearest_bull_ob_below": ({"top": below[0]["top"], "bottom": below[0]["bottom"], "layer": below[0]["layer"]}
                                  if below else None),
        "nearest_bear_ob_above": ({"top": above[0]["top"], "bottom": above[0]["bottom"], "layer": above[0]["layer"]}
                                  if above else None),
        "trailing_extremes": a["trailing"],
        "premium_discount_luxalgo": (None if a["trailing"]["equilibrium"] is None else
                                     ("PREMIUM" if price > a["trailing"]["equilibrium"] else "DISCOUNT")),
        "structure_engine": "smc_structure-v65.4 (aligned to LuxAlgo SMC Pine source, CC BY-NC-SA 4.0)",
    }


# ============================================================ v65.3 ====
# Break validation, liquidity sweeps, premium/discount, volume profile,
# structure invalidation. Concepts: LuxAlgo Library - "ICT Anchored Market
# Structures with Validation" (close beyond an ATR(17) deviation zone confirms a
# break; a breach that closes back inside is a sweep), "Liquidity Sweep",
# "Premium/Discount", "Structure Invalidation". Independent implementation.

VALIDATION_ATR_LEN = 17
VALIDATION_ATR_MULT = 0.5      # width of the deviation zone in ATR(17) units (not published - logged as a trial)
VALIDATION_WINDOW = 3          # bars allowed for a close to clear the zone before the break is judged
SWEEP_LOOKBACK_BARS = 6        # a sweep within this many bars counts as a fresh "hold signal"
VP_BINS = 48
VP_LOOKBACK = 180              # 4H bars ~ 30 days
VP_VALUE_AREA = 0.70


def _swing_points(c, n):
    """Right-confirmed alternating swing points for one layer: list of (idx, 'top'/'btm', price)."""
    pts, os_ = [], 0          # Pine leg() starts BEARISH_LEG -> first pivot is a low
    for i in range(n, len(c)):
        k = i - n
        upper = max(x["h"] for x in c[k + 1:i + 1])
        lower = min(x["l"] for x in c[k + 1:i + 1])
        prev = os_
        if c[k]["h"] > upper:
            os_ = 0
        elif c[k]["l"] < lower:
            os_ = 1
        if os_ == 0 and prev != 0:
            pts.append((k, "top", c[k]["h"], i))
        if os_ == 1 and prev != 1:
            pts.append((k, "btm", c[k]["l"], i))
    return pts


def validate_breaks(c, n=INTERNAL_LEN):
    """For every swing level of the layer: CONFIRMED break, SWEEP, or PENDING.
    A wick through + close back inside = SWEEP. A close through that also clears
    level +/- mult*ATR(17) within VALIDATION_WINDOW bars = CONFIRMED. A close
    through that falls back inside before clearing = SWEEP (failed break)."""
    atr = _atr(c, VALIDATION_ATR_LEN)
    out = []
    for k, kind, lvl, confirmed_at in _swing_points(c, n):
        bull = kind == "top"
        for i in range(confirmed_at + 1, len(c)):
            x = c[i]
            beyond_wick = x["h"] > lvl if bull else x["l"] < lvl
            if not beyond_wick:
                continue
            zone = lvl + VALIDATION_ATR_MULT * atr[i] if bull else lvl - VALIDATION_ATR_MULT * atr[i]
            closed_through = x["c"] > lvl if bull else x["c"] < lvl
            if not closed_through:
                out.append({"i": i, "ts": x["ts"], "level": lvl, "side": "high" if bull else "low",
                            "result": "SWEEP", "how": "wick through, close back inside"})
                break
            verdict = None
            for j in range(i, min(i + VALIDATION_WINDOW, len(c))):
                cj = c[j]["c"]
                if (cj > zone) if bull else (cj < zone):
                    verdict = ("CONFIRMED", j)
                    break
                if (cj <= lvl) if bull else (cj >= lvl):
                    verdict = ("SWEEP", j)
                    break
            if verdict is None:
                verdict = ("PENDING" if i + VALIDATION_WINDOW > len(c) - 1 else "WEAK", i)
            out.append({"i": verdict[1], "ts": c[verdict[1]]["ts"], "level": lvl, "side": "high" if bull else "low",
                        "result": verdict[0], "zone": round(zone, 10)})
            break
    return out


def hold_signal(c, validations):
    """Bullish hold signal = a recent SWEEP of a swing LOW (price ran the stops under
    support and closed back above) within SWEEP_LOOKBACK_BARS bars."""
    last_i = len(c) - 1
    fresh = [v for v in validations if v["side"] == "low" and v["result"] == "SWEEP"
             and last_i - v["i"] <= SWEEP_LOOKBACK_BARS]
    return {"bullish_hold_signal": bool(fresh),
            "last_low_sweep": ({"level": fresh[-1]["level"], "ts": fresh[-1]["ts"]} if fresh else None)}


def premium_discount(price, low, high):
    """Position of price inside a range: 0 = range low, 1 = range high.
    < 0.5 discount (cheap), > 0.5 premium (expensive); equilibrium = 0.5."""
    if high is None or low is None or high <= low:
        return None
    pos = (price - low) / (high - low)
    zone = "DEEP_DISCOUNT" if pos <= 0.382 else "DISCOUNT" if pos < 0.5 else "PREMIUM" if pos > 0.5 else "EQUILIBRIUM"
    return {"position": round(pos, 3), "zone": zone, "equilibrium": round((low + high) / 2, 10),
            "range_low": low, "range_high": high}


def volume_profile(c, lookback=VP_LOOKBACK, bins=VP_BINS):
    """Quote volume spread uniformly over each candle's high-low range.
    POC = highest-volume price bin; value area = bins holding 70% of volume."""
    seg = c[-lookback:]
    if not seg:
        return None
    lo, hi = min(x["l"] for x in seg), max(x["h"] for x in seg)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for x in seg:
        v = x.get("vq") or 0.0
        a = int((x["l"] - lo) / step)
        b = min(bins - 1, int((x["h"] - lo) / step))
        span = b - a + 1
        for t in range(a, b + 1):
            vol[t] += v / span
    total = sum(vol)
    if total <= 0:
        return None
    poc = max(range(bins), key=lambda t: vol[t])
    inside, acc, left, right = {poc}, vol[poc], poc, poc
    while acc < VP_VALUE_AREA * total and (left > 0 or right < bins - 1):
        nl = vol[left - 1] if left > 0 else -1
        nr = vol[right + 1] if right < bins - 1 else -1
        if nr >= nl:
            right += 1; acc += vol[right]; inside.add(right)
        else:
            left -= 1; acc += vol[left]; inside.add(left)
    mid = lambda t: lo + (t + 0.5) * step
    return {"poc": round(mid(poc), 10), "value_area_high": round(lo + (right + 1) * step, 10),
            "value_area_low": round(lo + left * step, 10), "bars_used": len(seg)}


def structure_invalidation(c, n=INTERNAL_LEN):
    """For a LONG built on this timeframe: the most recent swing LOW that is still
    unbroken (by close). A close below it invalidates the structure."""
    lows = [p for p in _swing_points(c, n) if p[1] == "btm"]
    for k, _, lvl, conf in reversed(lows):
        if all(x["c"] >= lvl for x in c[conf + 1:]):
            return {"long_invalidation_level": lvl, "swing_low_ts": c[k]["ts"], "layer_len": n}
    return {"long_invalidation_level": None}


def levels_report(c, impulse_low=None, impulse_high=None):
    """Everything the v65.3 layer adds, for one coin's 4H candles."""
    if len(c) < 2 * INTERNAL_LEN + 3:
        return {}
    val = validate_breaks(c)
    price = c[-1]["c"]
    recent = [v for v in val if len(c) - 1 - v["i"] <= 30]
    return {
        "recent_break_validations": [{k: v[k] for k in ("side", "level", "result", "ts")} for v in recent[-6:]],
        **hold_signal(c, val),
        "premium_discount_impulse": premium_discount(price, impulse_low, impulse_high),
        "volume_profile_30d": volume_profile(c),
        **structure_invalidation(c),
    }
