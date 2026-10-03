"""
replay_gate.py - lab validation gate (a): replay the real scalp paper trades
through the execution simulator on Binance 1H candles and compare outcomes
(RC-1.3 section 11: outcome match >= 85%, mean net return difference <= 0.3pp).

The scalp tracker: 3 targets, static stop (no breakeven), no time stop, stop
first when ambiguous, exit at the stop after partial targets. The replay uses
the same rules; returns are compared with equal thirds per target for both.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from engine.data import Lake, H1  # noqa: E402
from engine.execution import Spec, Candle, simulate  # noqa: E402

COST = 0.0026


def category(n_hit: int, n_targets: int) -> str:
    return "all_targets" if n_hit >= n_targets else ("partial_then_stop" if n_hit else "stopped")


def tracker_category(t: dict) -> str:
    return {"stopped": "stopped", "stopped_after_partial_targets": "partial_then_stop",
            "closed_targets_complete": "all_targets"}[t["status"]]


def thirds_return(entry, targets, n_hit, exit_price, all_done):
    w = 1 / len(targets)
    r = sum(w * (targets[i] / entry - 1) for i in range(n_hit))
    if not all_done:
        r += (1 - w * n_hit) * (exit_price / entry - 1)
    return r - COST


def main(trades_path: str, years=(2026,), mode: str = "next_hour"):
    """mode next_hour: the fill hour is replaced by a zero-range candle at the fill
    price (we cannot know what happened before vs after the fill inside it);
    mode fill_hour: the whole fill hour counts (biased - includes pre-fill prices)."""
    lake = Lake(download=True)
    trades = [t for t in json.load(open(trades_path))["trades"]
              if t["status"] in ("stopped", "stopped_after_partial_targets", "closed_targets_complete")]
    syms = {t["symbol"] + "USDT" for t in trades}
    h1 = lake.hourly(list(years), syms)
    rows, skipped = [], 0
    for t in trades:
        sym = t["symbol"] + "USDT"
        fill_ms = int(datetime.fromisoformat(t["filled_at"]).timestamp() * 1000)
        if sym not in h1:
            skipped += 1
            continue
        d = h1[sym]
        first = (fill_ms // H1) * H1 if mode == "fill_hour" else (fill_ms // H1 + 1) * H1
        d = d[d["open_time"] >= first]
        if len(d) < 2 or int(d["open_time"].iloc[-1]) < fill_ms + 48 * H1:
            skipped += 1                                    # not enough lake data after the fill
            continue
        cs = [Candle(int(r.open_time), float(r.open), float(r.high), float(r.low), float(r.close))
              for r in d.itertuples()]
        e = float(t["actual_entry"])
        if mode == "fill_hour":
            cs[0].o = e                                     # the tracker filled at its own price
        else:
            cs.insert(0, Candle(first - H1, e, e, e, e))   # entry candle = the fill itself
        targets = [float(x) for x in t["targets"]]
        spec = Spec(signal_time=fill_ms, stop=float(t["stop"]), targets=targets,
                    target_fracs=[1 / len(targets)] * len(targets), time_stop_candles=10 ** 6,
                    cost=COST, slippage=0.0003, atr=0.0, breakeven_after_t1=False)
        r = simulate(spec, cs, H1)
        if r["status"] not in ("CLOSED",):
            skipped += 1
            continue
        mine = category(r["targets_hit"], len(targets))
        theirs = tracker_category(t)
        n_hit_tr = len(t.get("targets_hit") or [])
        rows.append({"id": t["id"], "fill": t["filled_at"], "closed": t.get("date_closed"), "replay": mine, "tracker": theirs, "match": mine == theirs,
                     "replay_ret": thirds_return(t["actual_entry"], targets, r["targets_hit"], r["exits"][-1][1], mine == "all_targets"),
                     "tracker_ret": thirds_return(t["actual_entry"], targets, n_hit_tr, float(t["exit_price"]), theirs == "all_targets")})
    n = len(rows)
    match = sum(r["match"] for r in rows) / n if n else 0
    diff = (sum(r["replay_ret"] for r in rows) - sum(r["tracker_ret"] for r in rows)) / n * 100 if n else 0
    report = {"mode": mode, "compared": n, "skipped": skipped, "outcome_match": round(match, 4),
              "mean_net_return_diff_pp": round(diff, 3),
              "passed": match >= 0.85 and abs(diff) <= 0.3,
              "mismatches": [r for r in rows if not r["match"]][:40], "rows": rows}
    out = Path(__file__).resolve().parents[2] / "data" / "lab" / "gate-replay.json"
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "mismatches"}, indent=1))
    return report


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/scalp-trades.json",
         mode=sys.argv[2] if len(sys.argv) > 2 else "next_hour")
