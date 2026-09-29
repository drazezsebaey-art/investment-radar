"""
hypothesis_check.py (v65.4) - forward test of pattern hypotheses found in-sample.

28/9/2026 in-sample mining of 220 closed paper trades (one bullish regime,
correlated trades, ~40 buckets tried) produced two hypotheses. They are NOT
rules. They are judged ONLY on trades opened on/after FORWARD_START (data the
hypotheses never saw), against a control arm, once both arms reach MIN_N.

  H1  entry_archetype == "extension_continuation" beats every other archetype
  H2  confidence_score_at_entry in [30, 50) beats confidence >= 50

Verdict per hypothesis: PENDING (n too small) / SUPPORTED (test arm win rate
higher with a two-proportion z-test p < 0.05 AND higher average net return) /
NOT_SUPPORTED. Writes data/hypotheses-report.json. Never fails the workflow.
"""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRADE_FILES = {"auto": ROOT / "config" / "trades.json", "shadow": ROOT / "data" / "shadow-trades.json",
               "scalp": ROOT / "data" / "scalp-trades.json"}
OUT = ROOT / "data" / "hypotheses-report.json"
FORWARD_START = "2026-10-01"
MIN_N = 30
COST_PCT = 0.3
CLOSED = ("closed_targets_complete", "stopped", "stopped_after_partial_targets")

HYPOTHESES = {
    "H1": {"statement": "extension_continuation archetype outperforms all other archetypes",
           "test": lambda t: t.get("entry_archetype") == "extension_continuation",
           "control": lambda t: t.get("entry_archetype") not in (None, "extension_continuation"),
           "in_sample": {"n": 21, "win_rate_pct": 47.6, "avg_net_pct": 6.05, "control_win_rate_pct": 30.9}},
    "H2": {"statement": "confidence 30-49 at entry outperforms confidence >= 50",
           "test": lambda t: t.get("confidence_score_at_entry") is not None and 30 <= t["confidence_score_at_entry"] < 50,
           "control": lambda t: t.get("confidence_score_at_entry") is not None and t["confidence_score_at_entry"] >= 50,
           "in_sample": {"n": 96, "win_rate_pct": 41.7, "avg_net_pct": 2.92, "control_n": 47, "control_win_rate_pct": 27.7}},
}


def load(path):
    try:
        t = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(t, dict):
        t = t.get("trades") or next((v for v in t.values() if isinstance(v, list)), [])
    return [x for x in t if isinstance(x, dict)]


def opened_on(t):
    return str(t.get("created_at") or t.get("date_opened") or "")[:10]


def stats(trades):
    n = len(trades)
    if not n:
        return {"n": 0}
    w = sum(1 for t in trades if t["status"] == "closed_targets_complete")
    rets = []
    for t in trades:
        e, x = t.get("actual_entry") or t.get("entry"), t.get("exit_price")
        if e and x is not None:
            rets.append((x - e) / e * 100 - COST_PCT)
    return {"n": n, "wins": w, "win_rate_pct": round(w / n * 100, 1),
            "avg_net_pct": round(sum(rets) / len(rets), 2) if rets else None}


def z_test(a, b):
    """one-sided two-proportion z-test p-value for a.win_rate > b.win_rate"""
    if not a.get("n") or not b.get("n"):
        return None
    p1, p2 = a["wins"] / a["n"], b["wins"] / b["n"]
    p = (a["wins"] + b["wins"]) / (a["n"] + b["n"])
    se = math.sqrt(p * (1 - p) * (1 / a["n"] + 1 / b["n"])) or 1e-9
    z = (p1 - p2) / se
    return round(0.5 * math.erfc(z / math.sqrt(2)), 4)


def run(files=TRADE_FILES, out=OUT, forward_start=FORWARD_START):
    closed = []
    for track, f in files.items():
        for t in load(f):
            if t.get("status") in CLOSED and opened_on(t) >= forward_start:
                closed.append({**t, "_track": track})
    report = {"updated_at": datetime.now(timezone.utc).isoformat(), "forward_start": forward_start,
              "min_n_per_arm": MIN_N, "forward_closed_trades": len(closed), "hypotheses": {}}
    for hid, h in HYPOTHESES.items():
        a = stats([t for t in closed if h["test"](t)])
        b = stats([t for t in closed if h["control"](t)])
        p = z_test(a, b)
        if a.get("n", 0) < MIN_N or b.get("n", 0) < MIN_N:
            verdict = "PENDING"
        elif p is not None and p < 0.05 and (a.get("avg_net_pct") or -1e9) > (b.get("avg_net_pct") or -1e9):
            verdict = "SUPPORTED"
        else:
            verdict = "NOT_SUPPORTED"
        report["hypotheses"][hid] = {"statement": h["statement"], "in_sample_reference": h["in_sample"],
                                     "forward_test_arm": a, "forward_control_arm": b,
                                     "p_value_one_sided": p, "verdict": verdict}
    Path(out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Hypotheses: " + ", ".join(f"{k}={v['verdict']} (test n={v['forward_test_arm'].get('n', 0)}, "
                                      f"control n={v['forward_control_arm'].get('n', 0)})"
                                      for k, v in report["hypotheses"].items()))
    return report


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # never fail the workflow
        print(f"hypothesis_check failed (non-fatal): {e}", file=sys.stderr)
    sys.exit(0)
