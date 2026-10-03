"""
engine/execution.py - execution simulator (RC-1.3 sections 4, 5, 6, 10 and the
central intrabar rules). Playbooks say WHAT; this module decides IF and HOW a
signal is filled and exited. One trade at a time, candle by candle.

Conventions (all from the contract):
  * fill_price never contains costs; c = (fee + slippage) x 2 is subtracted from
    the return (net R = net return / initial risk fraction)
  * market entry = open of the candle after the signal candle
  * limit entry fills only if low <= limit - slippage x limit (touch is not a
    fill); fill_price = min(limit, open)
  * unknown order inside a candle -> worst case for the strategy (flag AMBIGUOUS),
    and an optimistic replay is kept as the upper bound
  * entry candle: the stop counts, targets do not
  * gap through stop / above a target -> exit at the open
  * close-confirmed exits (failure rules) exit at the next open; time stop exits
    at the close of candle N (entry candle = 1)
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional

STOP_MIN_ATR, STOP_MIN_COST_MULT, STOP_MAX_PCT = 0.5, 3.0, 0.08


@dataclass
class Candle:
    t: int          # open time ms
    o: float
    h: float
    l: float
    c: float
    complete: bool = True


@dataclass
class Spec:
    """What the playbook asks for (frozen at signal time)."""
    signal_time: int
    stop: float
    targets: List[float]                     # absolute prices; may be filled later via targets_fn
    target_fracs: List[float]                # e.g. [0.5, 0.5] or [1.0]
    time_stop_candles: int
    cost: float                              # round-trip c for the tier
    slippage: float                          # per side, for the limit trade-through rule
    atr: float
    entry: str = "market"                    # "market" | "limit"
    limit_price: Optional[float] = None
    limit_valid_candles: int = 6
    breakeven_after_t1: bool = True
    min_rr_net: Optional[float] = None       # structural-target playbooks (T3)
    targets_fn: Optional[Callable] = None    # (fill_price, risk) -> targets, for R-based targets
    failure_fn: Optional[Callable] = None    # (candle, state) -> True to exit next open (close-confirmed)
    cancel_close_below: Optional[float] = None   # pending limit: close below -> cancel
    cancel_if_high_reaches: Optional[float] = None  # pending limit: missed move
    blackout: List[tuple] = field(default_factory=list)  # [(start_ms, end_ms)] no fills inside (macro)


def _in_blackout(c: Candle, blackout, candle_ms) -> bool:
    return any(a < c.t + candle_ms and b > c.t for a, b in blackout)   # any overlap -> may be inside


def check_stop(fill: float, stop: float, atr: float, cost: float):
    dist = fill - stop
    if dist <= 0:
        return "stop_above_entry"
    if dist < STOP_MIN_ATR * atr or dist / fill < STOP_MIN_COST_MULT * cost:
        return "stop_too_tight"
    if dist / fill > STOP_MAX_PCT:
        return "stop_too_wide"
    return None


def simulate(spec: Spec, candles: List[Candle], candle_ms: int, optimistic: bool = False) -> dict:
    """candles: those AFTER the signal candle, in order."""
    res = {"status": None, "flags": [], "fill_price": None, "fill_time": None, "exits": []}
    i, n = 0, len(candles)
    # ---------------------------------------------------------- entry ----
    if spec.entry == "market":
        if n == 0:
            res["status"] = "NO_DATA"
            return res
        c0 = candles[0]
        if spec.blackout and _in_blackout(c0, spec.blackout, candle_ms):
            res["status"], res["reason"] = "INVALID", "MACRO_WINDOW"
            return res
        fill, fill_idx = c0.o, 0
    else:
        fill = fill_idx = None
        for i in range(min(spec.limit_valid_candles, n)):
            c = candles[i]
            trade_through = c.l <= spec.limit_price * (1 - spec.slippage)
            reached_t1 = spec.cancel_if_high_reaches is not None and c.h >= spec.cancel_if_high_reaches
            if trade_through and spec.blackout and _in_blackout(c, spec.blackout, candle_ms):
                if optimistic:
                    pass
                else:
                    res["status"], res["reason"] = "INVALID", "MACRO_WINDOW"
                    res["flags"].append("AMBIGUOUS")
                    return res
            if trade_through and reached_t1:
                res["flags"].append("AMBIGUOUS")
                if not optimistic:                       # worst case: T1 first -> we missed it
                    res["status"], res["reason"] = "INVALID", "MISSED_MOVE"
                    return res
            elif reached_t1:
                res["status"], res["reason"] = "INVALID", "MISSED_MOVE"
                return res
            if trade_through:
                fill, fill_idx = min(spec.limit_price, c.o), i
                break
            if spec.cancel_close_below is not None and c.c < spec.cancel_close_below:
                res["status"], res["reason"] = "INVALID", "SWEEP_BROKEN"
                return res
        if fill is None:
            res["status"] = "EXPIRED_UNFILLED"
            return res
    # ------------------------------------------------- entry re-check ----
    bad = check_stop(fill, spec.stop, spec.atr, spec.cost)
    if bad:
        res.update(status="INVALID", reason="ENTRY_GAP", sub_reason=bad, fill_price=fill)
        return res
    risk = fill - spec.stop
    if spec.targets_fn:
        try:
            targets = spec.targets_fn(fill, risk, candles[fill_idx].t)   # T2: targets frozen at fill time
        except TypeError:
            targets = spec.targets_fn(fill, risk)
    else:
        targets = list(spec.targets)
    if spec.min_rr_net is not None:
        rr = (targets[0] - fill - spec.cost * fill) / risk
        if rr < spec.min_rr_net:
            res.update(status="INVALID", reason="ENTRY_GAP", sub_reason="rr_too_low", fill_price=fill)
            return res
    res.update(fill_price=fill, fill_time=candles[fill_idx].t, targets=targets, stop=spec.stop)
    # ----------------------------------------------------------- manage ----
    stop, remaining, hit = spec.stop, 1.0, 0
    low, high = fill, fill
    pending_exit = False
    for k in range(fill_idx, n):
        c = candles[k]
        num = k - fill_idx + 1                               # entry candle = 1
        if not c.complete:
            res["flags"].append("DATA_GAP")
        if pending_exit:                                     # close-confirmed exit -> this open
            res["exits"].append((remaining, c.o, "FAILURE", c.t))
            remaining = 0
            break
        low, high = min(low, c.l), max(high, c.h)
        entry_candle = k == fill_idx
        # gaps at the open
        if c.o <= stop and not entry_candle:
            res["exits"].append((remaining, c.o, "STOP_GAP", c.t))
            remaining = 0
            break
        while not entry_candle and hit < len(targets) and c.o >= targets[hit] and remaining > 0:
            frac = min(spec.target_fracs[hit], remaining) if hit < len(targets) - 1 else remaining
            res["exits"].append((frac, c.o, f"T{hit + 1}_GAP", c.t))
            remaining -= frac
            hit += 1
            if spec.breakeven_after_t1 and hit == 1:
                stop = max(stop, fill * (1 + spec.cost))
        if remaining <= 1e-12:
            break
        # intrabar
        stop_touch = c.l <= stop
        tgt_touch = (not entry_candle) and hit < len(targets) and c.h >= targets[hit]
        if stop_touch and tgt_touch:
            res["flags"].append("AMBIGUOUS")
            if not optimistic:
                res["exits"].append((remaining, stop, "STOP", c.t))
                remaining = 0
                break
        if stop_touch and not (optimistic and tgt_touch):
            res["exits"].append((remaining, stop, "STOP", c.t))
            remaining = 0
            break
        while tgt_touch and remaining > 0:
            frac = min(spec.target_fracs[hit], remaining) if hit < len(targets) - 1 else remaining
            res["exits"].append((frac, targets[hit], f"T{hit + 1}", c.t))
            remaining -= frac
            hit += 1
            if spec.breakeven_after_t1 and hit == 1:
                stop = max(stop, fill * (1 + spec.cost))
            tgt_touch = hit < len(targets) and c.h >= targets[hit]
        if remaining <= 1e-12:
            break
        # close-confirmed rules
        if spec.failure_fn and hit == 0 and spec.failure_fn(c, {"fill": fill, "hit": hit}):
            pending_exit = True
            continue
        if num >= spec.time_stop_candles:
            res["exits"].append((remaining, c.c, "TIME", c.t))
            remaining = 0
            break
    if remaining > 1e-12:
        res["status"] = "OPEN_AT_END"
        res["exits"].append((remaining, candles[-1].c, "END_OF_DATA", candles[-1].t))
    else:
        res["status"] = "CLOSED"
    gross = sum(f * (p / fill - 1) for f, p, _, _ in res["exits"])
    net = gross - spec.cost
    res.update(gross_return=gross, net_return=net, r_net=net / (risk / fill),
               mae=low / fill - 1, mfe=high / fill - 1, risk_frac=risk / fill,
               exit_reason=res["exits"][-1][2], targets_hit=hit)
    return res


def simulate_both(spec: Spec, candles: List[Candle], candle_ms: int) -> dict:
    """Primary (worst case) result plus the optimistic upper bound when ambiguous."""
    main = simulate(spec, candles, candle_ms)
    if "AMBIGUOUS" in main["flags"]:
        opt = simulate(spec, candles, candle_ms, optimistic=True)
        main["optimistic_r_net"] = opt.get("r_net")
        main["optimistic_status"] = opt.get("status")
    return main
