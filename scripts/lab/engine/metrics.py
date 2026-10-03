"""
engine/metrics.py - RC-1.3 sections 12 and 13. Input: closed trade records
(dicts from the runner); output: the contract's metrics, nothing judged by an LLM.
"""
import math
import statistics
from collections import defaultdict

DAY_MS = 86_400_000


def max_drawdown_r(rs: list) -> float:
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def independent_clusters(trades: list, btc_24h: dict) -> int:
    """RC 12, with information available at signal time only: a new trade joins
    an open cluster if a trade of that cluster is still open at its signal time
    AND BTC was up > 2% in the 24h before the signal. The sector condition of the
    contract cannot be applied (no point-in-time sector data) -> this count is an
    upper bound and is reported as such."""
    clusters = []                                    # [ [ (fill, exit) ... ] ]
    for t in sorted(trades, key=lambda x: x["signal_time"]):
        joined = False
        if btc_24h.get(t["signal_time"], 0) > 0.02:
            for cl in clusters:
                if any(f <= t["signal_time"] < e for f, e in cl):
                    cl.append((t["fill_time"], t["exit_time"]))
                    joined = True
                    break
        if not joined:
            clusters.append([(t["fill_time"], t["exit_time"])])
    return len(clusters)


def summarize(trades: list, btc_24h: dict = None) -> dict:
    closed = [t for t in trades if t.get("status") == "CLOSED"]
    rs = [t["r_net"] for t in closed]
    if not rs:
        return {"n": 0}
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_profit = sum(t["gross_return"] for t in closed if t["gross_return"] > 0)
    costs = sum(t["cost"] for t in closed)
    exposure_days = sum((t["exit_time"] - t["fill_time"]) / DAY_MS for t in closed)
    months = {datetime_month(t["fill_time"]) for t in closed}
    out = {
        "n": len(rs),
        "independent_opportunities": independent_clusters(closed, btc_24h or {}),
        "independent_note": "upper bound - sector condition unavailable point-in-time",
        "assets": len({t["symbol"] for t in closed}),
        "months": len(months),
        "expectancy_r": round(statistics.mean(rs), 4),
        "median_r": round(statistics.median(rs), 4),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses and sum(losses) else None,
        "win_rate": round(len(wins) / len(rs), 4),
        "avg_win_r": round(statistics.mean(wins), 4) if wins else None,
        "avg_loss_r": round(statistics.mean(losses), 4) if losses else None,
        "max_drawdown_r": round(max_drawdown_r(rs), 3),
        "median_hold_h": round(statistics.median((t["exit_time"] - t["fill_time"]) / 3_600_000 for t in closed), 1),
        "mae_median": round(statistics.median(t["mae"] for t in closed), 4),
        "mfe_median": round(statistics.median(t["mfe"] for t in closed), 4),
        "cost_share_of_gross_profit": round(costs / gross_profit, 3) if gross_profit else None,
        "time_exits_pct": round(sum(1 for t in closed if t["exit_reason"] == "TIME") / len(closed), 3),
        "ambiguous_pct": round(sum(1 for t in closed if "AMBIGUOUS" in t.get("flags", [])) / len(closed), 3),
        "r_per_100_exposure_days": round(100 * sum(rs) / exposure_days, 3) if exposure_days else None,
    }
    opt = [t.get("optimistic_r_net", t["r_net"]) for t in closed]
    out["expectancy_r_optimistic"] = round(statistics.mean(opt), 4)
    return out


def datetime_month(ms: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m")


def breakdown(trades: list, key: str) -> dict:
    groups = defaultdict(list)
    for t in trades:
        groups[t.get(key)].append(t)
    return {k: summarize(v) for k, v in groups.items()}


def decisions_report(signals: list) -> dict:
    """Counts of every non-trade decision (ABSTAIN / WAIT / INVALID / DATA_ERROR...)
    and the DATA_ERROR share (RC 12: > 5% or concentrated -> uninterpretable)."""
    by = defaultdict(int)
    for s in signals:
        by[f"{s['decision']}:{s.get('reason', '')}"] += 1
    total = len(signals) or 1
    de = sum(v for k, v in by.items() if k.startswith("DATA_ERROR"))
    return {"counts": dict(by), "data_error_share": round(de / total, 4),
            "interpretable": de / total <= 0.05}


def gate(summary: dict, baseline: dict = None) -> dict:
    """RC 13 transfer gate - only the parts computable from one summary; the
    stress and parameter-stability conditions are filled by the runner."""
    checks = {
        "n_trades>=40": summary.get("n", 0) >= 40,
        "independent>=20": summary.get("independent_opportunities", 0) >= 20,
        "expectancy>=0.10R": (summary.get("expectancy_r") or -9) >= 0.10,
        "pf>=1.25": (summary.get("profit_factor") or 0) >= 1.25,
        "maxdd<=12R": (summary.get("max_drawdown_r") or 99) <= 12,
    }
    if baseline:
        checks["beats_baseline_expectancy"] = summary.get("expectancy_r", -9) > baseline.get("expectancy_r", 9)
        checks["beats_baseline_r_per_exposure"] = (summary.get("r_per_100_exposure_days") or -9) > \
            (baseline.get("r_per_100_exposure_days") or 9)
    return {"checks": checks, "passed_so_far": all(checks.values()),
            "language": "passing = cleared the gate to forward testing, NOT a proven edge"}
