"""
Investment Radar - Target Attainment Report (v1)
------------------------------------------------------------
Context (2026-09-21, Azez): "if I open 10 trades with 3 targets each, how
many typically reach target 1 only, target 2, all 3, or none at all?" -
this answers exactly that, using data that ALREADY exists in every trade's
targets_hit array (populated by track_trades.py's check_open/check_pending
as it runs) - no change to how trades are opened, tracked, or closed. A
trade doesn't need to be closed yet to count here: an open trade with
targets_hit=[target1] contributes to the "reached >=1" bucket today and
can move to a higher bucket on a later run, same as any other field that
updates over time.

Deliberately read-only and non-invasive: this does NOT split positions or
change what "closing a trade" means anywhere else in the pipeline - see
the discussion note in the docstring below for why that's a SEPARATE
decision (partial profit-taking / scaling out) kept out of this script.

Run manually (not part of the scheduled workflow - this is an on-demand
analysis, not a fee/state update): python scripts/target_attainment_report.py
"""
import json
from pathlib import Path
from collections import Counter

BASE_DIR = Path(__file__).resolve().parent.parent
TRADES_PATH = BASE_DIR / "config" / "trades.json"
SHADOW_TRADES_PATH = BASE_DIR / "data" / "shadow-trades.json"
SCALP_TRADES_PATH = BASE_DIR / "data" / "scalp-trades.json"
REPORT_PATH = BASE_DIR / "data" / "target-attainment-report.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def classify_trade(trade: dict) -> dict:
    """Returns how many of this trade's targets were reached (so far, if
    still open) and its current status bucket. A trade stopped out before
    any target counts as 0/n - that's real information (the R:R math never
    paid off at all), not a trade to exclude."""
    targets = trade.get("targets") or []
    targets_hit = trade.get("targets_hit") or []
    n_targets = len(targets)
    n_hit = len(targets_hit)
    status = trade.get("status")
    return {
        "id": trade.get("id"),
        "symbol": trade.get("symbol"),
        "n_targets": n_targets,
        "n_hit": n_hit,
        "status": status,
        "fully_resolved": status in ("stopped", "stopped_after_partial_targets", "closed_targets_complete"),
    }


def summarize_track(trades: list) -> dict:
    # only trades that actually have a target ladder defined (pending
    # orders that never filled, or malformed entries, contribute nothing
    # meaningful here) and have progressed past "pending" (a never-filled
    # limit order hasn't had a chance to reach anything yet).
    eligible = [t for t in trades if t.get("targets") and t.get("status") != "pending"]
    classified = [classify_trade(t) for t in eligible]

    distribution = Counter(c["n_hit"] for c in classified)
    resolved = [c for c in classified if c["fully_resolved"]]
    resolved_distribution = Counter(c["n_hit"] for c in resolved)

    return {
        "n_trades": len(classified),
        "n_fully_resolved": len(resolved),
        "distribution_all_trades": dict(sorted(distribution.items())),
        "distribution_fully_resolved_only": dict(sorted(resolved_distribution.items())),
        "trades": classified,
        "note": (
            "distribution_all_trades counts every eligible trade at its CURRENT progress "
            "(an open trade with 1/3 targets hit so far still counts as 1 today, and may move "
            "up on a later run). distribution_fully_resolved_only counts only trades whose story "
            "has ended (stopped or fully closed) - that's the more honest number for calibrating "
            "real-money targets, since it isn't inflated by trades that are still open and might "
            "still reach more targets, or might still give them back."
        ),
    }


def main():
    tracks = {
        "real_and_paper": load_json(TRADES_PATH, {"trades": []}).get("trades", []),
        "shadow": load_json(SHADOW_TRADES_PATH, {"trades": []}).get("trades", []),
        "scalp": load_json(SCALP_TRADES_PATH, {"trades": []}).get("trades", []),
    }

    report = {track: summarize_track(trades) for track, trades in tracks.items()}
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    for track, summary in report.items():
        print(f"\n[{track}] {summary['n_trades']} eligible trades, {summary['n_fully_resolved']} fully resolved")
        print(f"  all trades (current progress):   {summary['distribution_all_trades']}")
        print(f"  fully resolved only:              {summary['distribution_fully_resolved_only']}")


if __name__ == "__main__":
    main()
