"""
Investment Radar - Thesis Watchlist Checker (v1)
------------------------------------------------------------
Rationale (22/9/2026): every full Agent Room review that lands on WAIT/HOLD
names specific conditions to watch for ("wait for a daily close above $8",
"or a pullback to $7.00-7.20 with RSI cooling under 55") - but nothing
tracked those conditions automatically. It fell on remembering to check
back manually, the exact "remember to do X" problem already solved for
agent-room-log.json. This closes the same gap for entry conditions.

How it's meant to be used: after any full analysis that ends in WAIT/HOLD
with concrete, checkable conditions, Claude adds an entry to
data/thesis-watchlist.json (same "here's the ready block to paste" pattern
as the agent-room-log.json snippet) with one or more SCENARIOS, each a
list of conditions that must ALL be true together (AND) - an entry
triggers when ANY one scenario fully matches (OR across scenarios), which
is how a real "either this breaks out OR that pulls back" thesis is
actually shaped.

Only two condition types are supported on purpose:
  price_above / price_below - checked against radar-flags.json's
    price_usd, which is real, live, and exact.
  rsi_above / rsi_below - checked against indicators.rsi14, which is
    APPROXIMATE (computed from ~15-min snapshots over a short window, not
    true candle-close RSI - the exact gap found reviewing ZAMA). Every
    finding involving an RSI condition is labeled accordingly so it's
    never mistaken for a confirmed trigger - it's a prompt to go check the
    real chart, not a final answer.

Never fails the workflow - purely observational, same as consistency_check.py.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

RADAR_FLAGS_PATH = DATA_DIR / "radar-flags.json"
WATCHLIST_PATH = DATA_DIR / "thesis-watchlist.json"
REPORT_PATH = DATA_DIR / "thesis-check-report.json"

STALE_WARNING_DAYS = 21  # a pending thesis this old without triggering is worth a human glance


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def check_condition(cond: dict, coin: dict) -> bool | None:
    """Returns True/False if the condition could be evaluated, or None if
    the needed field isn't available for this coin this run (never treated
    as a false negative - just skipped, entry stays pending)."""
    ctype = cond.get("type")
    if ctype == "price_above":
        price = coin.get("price_usd")
        return price is not None and price >= cond["level"]
    if ctype == "price_below":
        price = coin.get("price_usd")
        return price is not None and price <= cond["level"]
    if ctype == "rsi_above":
        rsi = (coin.get("indicators") or {}).get("rsi14")
        return rsi is not None and rsi >= cond["level"]
    if ctype == "rsi_below":
        rsi = (coin.get("indicators") or {}).get("rsi14")
        return rsi is not None and rsi <= cond["level"]
    return None


def scenario_matches(scenario: dict, coin: dict) -> bool:
    results = [check_condition(c, coin) for c in scenario.get("conditions", [])]
    if not results or any(r is None for r in results):
        return False  # can't confirm every leg yet - not a match
    return all(results)


def format_condition(cond: dict) -> str:
    ctype = cond.get("type", "?")
    level = cond.get("level")
    label = {
        "price_above": f"السعر >= {level}",
        "price_below": f"السعر <= {level}",
        "rsi_above": f"RSI >= {level} (تقريبي - راجع الشارت الحقيقي)",
        "rsi_below": f"RSI <= {level} (تقريبي - راجع الشارت الحقيقي)",
    }.get(ctype, f"{ctype} {level}")
    return label


def main():
    radar = load_json(RADAR_FLAGS_PATH, {"coins": []})
    coin_lookup = {c["id"]: c for c in radar.get("coins", [])}

    watchlist = load_json(WATCHLIST_PATH, {"watchlist": []})
    entries = watchlist.get("watchlist", [])

    now = datetime.now(timezone.utc)
    newly_triggered, still_pending, stale = [], [], []

    for entry in entries:
        if entry.get("status") != "pending":
            continue  # already triggered-and-reviewed, or manually resolved - leave alone

        coin = coin_lookup.get(entry["coin_id"])
        if coin is None:
            still_pending.append(entry)  # coin not in this run's flagged set - can't check, not an error
            continue

        matched_scenario = next((s for s in entry.get("scenarios", []) if scenario_matches(s, coin)), None)
        if matched_scenario:
            entry["status"] = "triggered"
            entry["triggered_at"] = now.isoformat()
            entry["triggered_scenario"] = matched_scenario.get("label")
            entry["price_at_trigger"] = coin.get("price_usd")
            newly_triggered.append(entry)
        else:
            still_pending.append(entry)
            try:
                added = datetime.fromisoformat(entry["date_added"])
                if added.tzinfo is None:
                    added = added.replace(tzinfo=timezone.utc)
                age_days = (now - added).days
                if age_days >= STALE_WARNING_DAYS:
                    stale.append(entry)
            except (KeyError, ValueError):
                pass

    save_json(WATCHLIST_PATH, watchlist)

    report = {
        "checked_at": now.isoformat(),
        "n_triggered_this_run": len(newly_triggered),
        "n_pending": len(still_pending),
        "n_stale": len(stale),
        "triggered": [
            {
                "symbol": e.get("symbol"), "coin_id": e.get("coin_id"),
                "matched_scenario": e.get("triggered_scenario"),
                "verdict_at_creation": e.get("verdict_at_creation"),
                "date_added": e.get("date_added"), "price_at_trigger": e.get("price_at_trigger"),
                "note": e.get("note"),
            }
            for e in newly_triggered
        ],
        "stale": [
            {"symbol": e.get("symbol"), "coin_id": e.get("coin_id"), "date_added": e.get("date_added")}
            for e in stale
        ],
    }
    save_json(REPORT_PATH, report)

    print(f"Thesis watchlist: {len(newly_triggered)} newly triggered, {len(still_pending)} still pending, "
          f"{len(stale)} pending >{STALE_WARNING_DAYS}d without a trigger.")
    for e in newly_triggered:
        conds = "; ".join(format_condition(c) for c in
                           next(s for s in e["scenarios"] if s.get("label") == e["triggered_scenario"]).get("conditions", []))
        print(f"  TRIGGERED [{e.get('symbol')}] scenario '{e.get('triggered_scenario')}' ({conds}) - verdict was {e.get('verdict_at_creation')}")
    for e in stale:
        print(f"  STALE [{e.get('symbol')}] added {e.get('date_added')}, still pending - worth a fresh look")


if __name__ == "__main__":
    main()
