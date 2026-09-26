"""
Counterfactual Rejection Tracker
------------------------------------------------------------
Per the 24/9/2026 audit report (item 24): a rejection rule (STOP_TOO_WIDE,
RR_BELOW_FLOOR, POOR_LIQUIDITY, ...) is only trustworthy once we know
whether the coins it rejected actually went on to win or lose. This
revisits entries in data/rejections-log.json once they are old enough to
judge (48-96 hours), checks what price actually did since the rejection,
and tags each with a one-time counterfactual outcome - never rechecked
after that, and never influences any live decision, purely descriptive
data for a future calibration pass (per the "Level 1 Observation -> Level
4 Adaptation" learning ladder the audit report itself recommends - this is
Level 1/2 only, no rule changes automatically).

Capped at COUNTERFACTUAL_BATCH_SIZE lookups per run - this runs on TOP of
the pipeline's existing CoinGecko usage, so it stays a small, bounded add.
"""
import json
import os
import time
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
REJECTIONS_LOG_PATH = DATA_DIR / "rejections-log.json"
COUNTERFACTUAL_SUMMARY_PATH = DATA_DIR / "counterfactual-summary.json"

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY")

EVAL_WINDOW_MIN_HOURS = 48    # don't judge a rejection until at least this much time has passed
EVAL_WINDOW_MAX_HOURS = 96    # if a run was missed and this window passed too, still catch it up to here
COUNTERFACTUAL_BATCH_SIZE = 20
FAVORABLE_MOVE_PCT = 4.0      # matches the system's own smallest typical target size (V2's "fast" floor)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_current_price(asset_id: str):
    headers = {"User-Agent": "investment-radar/1.0"}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
    params = {"ids": asset_id, "vs_currencies": "usd"}
    url = f"{COINGECKO_BASE}/simple/price?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return data.get(asset_id, {}).get("usd")
    except Exception:  # noqa: BLE001
        return None


def get_reference_price(rejection: dict):
    """Pulls whatever price the rejection's own details happened to record
    (different rejection stages use different field names) - returns None
    if nothing usable is there, which simply excludes that entry rather
    than guessing a price."""
    details = rejection.get("details") or {}
    return details.get("entry") or details.get("price_at_rejection")


def find_evaluable(rejections: list, now: datetime) -> list:
    candidates = []
    for r in rejections:
        if r.get("counterfactual_outcome") is not None:
            continue
        if get_reference_price(r) is None or not r.get("asset_id"):
            continue
        try:
            ts = datetime.fromisoformat(r["timestamp"])
        except (KeyError, ValueError):
            continue
        age_hours = (now - ts).total_seconds() / 3600
        if EVAL_WINDOW_MIN_HOURS <= age_hours <= EVAL_WINDOW_MAX_HOURS:
            candidates.append(r)
    return candidates


def classify_outcome(move_pct: float) -> str:
    if move_pct >= FAVORABLE_MOVE_PCT:
        return "would_have_won"
    if move_pct <= -FAVORABLE_MOVE_PCT:
        return "would_have_lost"
    return "flat"


def build_summary(rejections: list) -> dict:
    summary = {}
    for r in rejections:
        outcome = r.get("counterfactual_outcome")
        if outcome is None or outcome == "price_unavailable":
            continue
        for code in r.get("rejection_codes", []):
            bucket = summary.setdefault(code, {"would_have_won": 0, "would_have_lost": 0, "flat": 0})
            bucket[outcome] = bucket.get(outcome, 0) + 1
    return summary


def main():
    log = load_json(REJECTIONS_LOG_PATH, {"rejections": []})
    rejections = log.get("rejections", [])
    now = datetime.now(timezone.utc)

    candidates = find_evaluable(rejections, now)
    batch = candidates[:COUNTERFACTUAL_BATCH_SIZE]

    n_evaluated = 0
    for r in batch:
        entry = get_reference_price(r)
        current = fetch_current_price(r["asset_id"])
        time.sleep(0.5)
        if current is None:
            r["counterfactual_outcome"] = "price_unavailable"
            continue
        move_pct = round((current - entry) / entry * 100, 2)
        r["counterfactual_move_pct"] = move_pct
        r["counterfactual_outcome"] = classify_outcome(move_pct)
        r["counterfactual_checked_at"] = now.isoformat()
        n_evaluated += 1

    save_json(REJECTIONS_LOG_PATH, log)

    summary = build_summary(rejections)
    save_json(COUNTERFACTUAL_SUMMARY_PATH, {
        "generated_at": now.isoformat(),
        "by_rejection_code": summary,
        "n_evaluated_this_run": n_evaluated,
        "n_pending_evaluation": max(0, len(candidates) - n_evaluated),
        "favorable_move_threshold_pct": FAVORABLE_MOVE_PCT,
    })

    print(f"Counterfactual check: evaluated {n_evaluated} rejections this run "
          f"({len(candidates)} were eligible, capped at {COUNTERFACTUAL_BATCH_SIZE}).")
    for code, counts in summary.items():
        total = sum(counts.values())
        won_pct = round(counts["would_have_won"] / total * 100, 1) if total else None
        print(f"  {code}: {counts} ({won_pct}% would have won)")


if __name__ == "__main__":
    main()
