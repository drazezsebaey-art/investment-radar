"""
Investment Radar - Agent Room KPI Aggregator (new, v6)
------------------------------------------------------------
Context: signal-log.json + signal-performance.json already answer "does
the TECHNICAL signal itself beat doing nothing" (see evaluate_signals.py).
They can't answer a different, equally important question: "of the radar
signals that got a FULL Agent Room review, what fraction actually turned
into an actionable call (BUY NOW / ACCUMULATE) vs a pass (WAIT / HOLD /
REDUCE / EXIT)?" That number lives in data/agent-room-log.json - a small,
manually-appended record of every full Agent Room verdict - because Agent
Room reviews happen in conversation, not in a script.

This script is manual-run (not part of the GitHub Actions schedule - there's
nothing to automate here, it's just arithmetic over a hand-kept log). Run it
locally with: python scripts/agent_room_stats.py

Why this matters concretely: it's the direct empirical check on whether the
v6 signal_quality field (idiosyncratic vs beta_driven_or_cluster, added to
breakout_check.py after the 2026-09-19 review) is actually predictive - if
"idiosyncratic" signals pass Agent Room noticeably more often than
"beta_driven_or_cluster" ones over enough samples, that confirms the filter
is worth keeping as a priority signal rather than just a label.

Writes: data/agent-room-stats.json
"""
import json
from pathlib import Path
from collections import Counter, defaultdict

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
LOG_PATH = DATA_DIR / "agent-room-log.json"
STATS_PATH = DATA_DIR / "agent-room-stats.json"

ACTIONABLE_VERDICTS = {"BUY NOW", "ACCUMULATE"}
NON_ACTIONABLE_VERDICTS = {"WAIT", "HOLD", "REDUCE", "EXIT"}


def load_log() -> list:
    if not LOG_PATH.exists():
        return []
    try:
        return json.loads(LOG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        print(f"Warning: {LOG_PATH} is not valid JSON, treating as empty.")
        return []


def pct(n_actionable: int, n_total: int):
    return round(n_actionable / n_total * 100, 1) if n_total else None


def summarize(entries: list) -> dict:
    n = len(entries)
    verdict_counts = Counter(e.get("verdict") for e in entries)
    unrecognized = [v for v in verdict_counts if v not in ACTIONABLE_VERDICTS | NON_ACTIONABLE_VERDICTS]
    if unrecognized:
        print(f"Warning: unrecognized verdict values in log (check for typos): {unrecognized}")

    n_actionable = sum(c for v, c in verdict_counts.items() if v in ACTIONABLE_VERDICTS)

    # breakdown by signal_quality_at_trigger - the key diagnostic this script exists for
    by_quality = defaultdict(lambda: {"n": 0, "n_actionable": 0})
    for e in entries:
        q = e.get("signal_quality_at_trigger") or "unknown"
        by_quality[q]["n"] += 1
        if e.get("verdict") in ACTIONABLE_VERDICTS:
            by_quality[q]["n_actionable"] += 1

    by_quality_out = {
        q: {
            "n": d["n"],
            "n_actionable": d["n_actionable"],
            "actionable_rate_pct": pct(d["n_actionable"], d["n"]),
        }
        for q, d in by_quality.items()
    }

    avg_confidence = round(
        sum(e.get("confidence", 0) for e in entries) / n, 1
    ) if n else None

    return {
        "n_reviews": n,
        "verdict_counts": dict(verdict_counts),
        "actionable_rate_pct": pct(n_actionable, n),
        "avg_confidence": avg_confidence,
        "by_signal_quality_at_trigger": by_quality_out,
        "note": (
            "n under ~15-20 is directional only, not conclusive - this is a hand-kept "
            "log so it grows slowly by design. actionable_rate_pct = BUY NOW + ACCUMULATE "
            "as a share of all full Agent Room reviews logged. A low rate is not "
            "necessarily a problem: it may mean the pre-filters (cluster/beta check, "
            "ATH drawdown, unlock risk) are correctly doing their job upstream, or that "
            "the market regime right now genuinely offers few clean entries - "
            "by_signal_quality_at_trigger is what tells the two apart over time."
        ),
    }


def main():
    entries = load_log()
    if not entries:
        print(f"No entries found in {LOG_PATH} - nothing to summarize yet.")
        return
    stats = summarize(entries)
    STATS_PATH.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Summarized {stats['n_reviews']} Agent Room reviews -> {STATS_PATH}")
    print(f"  Actionable rate: {stats['actionable_rate_pct']}%")
    for q, d in stats["by_signal_quality_at_trigger"].items():
        print(f"  {q}: {d['n_actionable']}/{d['n']} actionable ({d['actionable_rate_pct']}%)")


if __name__ == "__main__":
    main()
