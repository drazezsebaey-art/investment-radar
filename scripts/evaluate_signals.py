"""
Investment Radar - Signal Evaluator (new in v4)
------------------------------------------------------------
Runs after breakout_check.py. Looks at data/signal-log.json (every check
breakout_check.py has ever logged, fired or not) and, for any entry at
least EVALUATION_DELAY_HOURS old that hasn't been evaluated yet, looks up
that coin's CURRENT price from data/market-scan.json and records what
happened since the signal was logged.

This is the calibration mechanism: it lets us eventually answer, with real
outcomes instead of assumptions, questions like "does breakout_signal=true
actually outperform doing nothing?" and "is the 1.3x volume threshold the
right number, or should it be 1.5x?" - the same evidence-based-calibration
principle used elsewhere (e.g. adjusting probability estimates as new
evidence arrives).

Writes:
  - Updates data/signal-log.json in place (adds evaluated=true and outcome
    fields to entries that just got evaluated)
  - data/signal-performance.json - an aggregate summary split by signal
    type (breakout_signal vs extension_continuation_signal vs neither/
    baseline), so the numbers are comparable at a glance
"""
import json
from pathlib import Path
from datetime import datetime, timezone
from statistics import mean

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SIGNAL_LOG_PATH = DATA_DIR / "signal-log.json"
SCAN_PATH = DATA_DIR / "market-scan.json"
PERFORMANCE_PATH = DATA_DIR / "signal-performance.json"

EVALUATION_DELAY_HOURS = 24  # how long after a signal to check the outcome


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def build_price_lookup(scan: dict) -> dict:
    return {c["id"]: c.get("price_usd") for c in scan.get("coins", [])}


def evaluate_entry(entry: dict, price_lookup: dict, now: datetime) -> bool:
    """Returns True if this entry was evaluated this run (age reached, price found)."""
    ts = datetime.fromisoformat(entry["timestamp"])
    age_hours = (now - ts).total_seconds() / 3600
    if age_hours < EVALUATION_DELAY_HOURS:
        return False

    current_price = price_lookup.get(entry["coin_id"])
    price_then = entry.get("price_at_check")
    if current_price is None or price_then in (None, 0):
        entry["evaluated"] = True
        entry["outcome_note"] = "price unavailable at evaluation time"
        return True

    pct_change = round((current_price - price_then) / price_then * 100, 2)
    entry["evaluated"] = True
    entry["price_after_24h"] = current_price
    entry["pct_change_after_signal"] = pct_change
    return True


def classify(entry: dict) -> str:
    if entry.get("breakout_signal_high_confidence"):
        return "breakout_signal_high_confidence"
    if entry.get("breakout_signal"):
        return "breakout_signal"
    if entry.get("extension_continuation_signal"):
        return "extension_continuation_signal"
    if entry.get("pullback_entry_signal"):
        return "pullback_entry_signal"
    return "no_signal_baseline"


def summarize(log: list) -> dict:
    evaluated = [e for e in log if e.get("evaluated") and "pct_change_after_signal" in e]

    def stats_for(group: list) -> dict:
        n = len(group)
        changes = [e["pct_change_after_signal"] for e in group]
        positive = [c for c in changes if c > 0]
        return {
            "n": n,
            "pct_positive_after_24h": round(len(positive) / n * 100, 1) if n else None,
            "avg_change_pct": round(mean(changes), 2) if changes else None,
        }

    groups = {
        "breakout_signal": [], "breakout_signal_high_confidence": [],
        "extension_continuation_signal": [], "pullback_entry_signal": [],
        "no_signal_baseline": [],
    }
    for e in evaluated:
        groups[classify(e)].append(e)

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "n under ~30 per group is not statistically meaningful yet - directional only. "
            "no_signal_baseline is what an average logged coin does with no signal at all, "
            "for comparison against the signal types."
        ),
        "breakout_signal": stats_for(groups["breakout_signal"]),
        "breakout_signal_high_confidence": stats_for(groups["breakout_signal_high_confidence"]),
        "extension_continuation_signal": stats_for(groups["extension_continuation_signal"]),
        "pullback_entry_signal": stats_for(groups["pullback_entry_signal"]),
        "no_signal_baseline": stats_for(groups["no_signal_baseline"]),
    }


def main():
    log = load_json(SIGNAL_LOG_PATH, [])
    if not log:
        print("No signal log found, nothing to evaluate.")
        return

    scan = load_json(SCAN_PATH, {"coins": []})
    price_lookup = build_price_lookup(scan)
    now = datetime.now(timezone.utc)

    newly_evaluated = 0
    for entry in log:
        if entry.get("evaluated"):
            continue
        if evaluate_entry(entry, price_lookup, now):
            newly_evaluated += 1

    SIGNAL_LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = summarize(log)
    PERFORMANCE_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Evaluated {newly_evaluated} newly-due signal(s). "
          f"breakout_signal n={summary['breakout_signal']['n']}, "
          f"extension n={summary['extension_continuation_signal']['n']}, "
          f"baseline n={summary['no_signal_baseline']['n']}.")


if __name__ == "__main__":
    main()
