"""
Investment Radar - Trade Tracker (v3)
---------------------------------------
v3 additions on top of v2:
  - PENDING LIMIT ENTRIES: a trade can now start as status="pending" with
    an entry price BELOW the current market price (a "buy the pullback"
    setup, exactly what a limit order does). Each run, if the coin's 24h
    low touches or goes below that entry price, the trade is marked
    "filled" and moves to status="open" - from that point it's tracked
    exactly like any other open trade. No trade sits "pending" forever
    without you being told: filled_at/actual_entry get recorded the moment
    it happens, and the next scheduled-task check will report it.
  - MULTIPLE TARGETS: a trade can list several targets in ascending order
    (targets: [t1, t2, t3]) instead of a single target_low. Each run checks
    which NEW targets the 24h high has cleared and appends them to
    targets_hit (with a timestamp) - the position is only considered fully
    closed once the stop is hit OR the highest target is reached, so a
    trade can accumulate multiple targets_hit before finally closing.
    Old-style trades with only "target_low" still work unchanged (treated
    as a single-target list).
  - PARTIAL-THEN-STOPPED tracking: if the stop is hit after one or more
    targets were already recorded, the trade closes as
    "stopped_after_partial_targets" instead of a plain "stopped" - so a
    trade that reached target 1 before reversing isn't scored identically
    to one that went straight to the stop.

Conservative rule unchanged: if the stop and a target both look touched in
the same 24h window, the stop wins by default (flagged in a note) rather
than assuming the better outcome - same discipline as v2.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
TRADES_PATH = BASE_DIR / "config" / "trades.json"
SCAN_PATH = BASE_DIR / "data" / "market-scan.json"
SUMMARY_PATH = BASE_DIR / "data" / "performance-summary.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def build_price_lookup(scan: dict) -> dict:
    return {coin["id"]: coin for coin in scan.get("coins", [])}


def get_targets(trade: dict) -> list:
    """Supports both the new 'targets' list and the old single 'target_low'
    field, always returned sorted ascending."""
    targets = trade.get("targets")
    if targets:
        return sorted(targets)
    if trade.get("target_low") is not None:
        return [trade["target_low"]]
    return []


def check_pending(trade: dict, coin: dict) -> bool:
    """Returns True if the trade was filled this run."""
    entry = trade.get("entry")
    low = coin.get("low_24h_usd")
    if entry is None or low is None:
        return False
    if low <= entry:
        now = datetime.now(timezone.utc).isoformat()
        trade["status"] = "open"
        trade["filled_at"] = now
        trade["actual_entry"] = entry  # paper-trade simplifying assumption: filled exactly at the limit price
        return True
    return False


def check_open(trade: dict, coin: dict) -> None:
    low = coin.get("low_24h_usd")
    high = coin.get("high_24h_usd")
    stop = trade.get("stop")
    targets = get_targets(trade)
    now = datetime.now(timezone.utc).isoformat()

    trade.setdefault("targets_hit", [])
    already_hit = {t["target"] for t in trade["targets_hit"]}

    newly_hit = []
    if high is not None:
        for t in targets:
            if t not in already_hit and high >= t:
                newly_hit.append(t)

    for t in newly_hit:
        trade["targets_hit"].append({"target": t, "hit_at": now})

    stop_hit = stop is not None and low is not None and low <= stop
    final_target = targets[-1] if targets else None
    final_target_hit = final_target is not None and (
        final_target in already_hit or final_target in newly_hit
    )

    if stop_hit and newly_hit:
        trade["status"] = "stopped_after_partial_targets" if len(trade["targets_hit"]) > len(newly_hit) or trade["targets_hit"] else "stopped"
        trade["exit_price"] = stop
        trade["date_closed"] = now
        trade["note"] = (
            "⚠️ تعارض: الستوب وهدف جديد الاتنين ظهروا متلمسين في نفس نافذة الـ24 ساعة — "
            "معتبرينها ستوب كافتراض متحفظ، الترتيب الفعلي مش مؤكد من البيانات دي."
        )
    elif stop_hit:
        trade["status"] = "stopped_after_partial_targets" if trade["targets_hit"] else "stopped"
        trade["exit_price"] = stop
        trade["date_closed"] = now
    elif final_target_hit:
        trade["status"] = "closed_targets_complete"
        trade["exit_price"] = final_target
        trade["date_closed"] = now
    # else: still open, possibly with newly_hit targets recorded but not fully closed


def pct_return(trade: dict) -> float:
    entry = trade.get("actual_entry", trade.get("entry"))
    exit_price = trade.get("exit_price")
    if entry is None or exit_price is None or entry == 0:
        return None
    return round((exit_price - entry) / entry * 100, 2)


CLOSED_STATUSES = ("closed_targets_complete", "stopped", "stopped_after_partial_targets",
                   "target_hit")  # target_hit kept for backward compatibility with old records


def summarize(trades: list) -> dict:
    closed = [t for t in trades if t.get("status") in CLOSED_STATUSES]

    def stats_for(subset: list) -> dict:
        n = len(subset)
        wins = [t for t in subset if t.get("status") in ("closed_targets_complete", "target_hit")]
        partial = [t for t in subset if t.get("status") == "stopped_after_partial_targets"]
        losses = [t for t in subset if t.get("status") == "stopped"]
        returns = [pct_return(t) for t in subset if pct_return(t) is not None]
        win_returns = [pct_return(t) for t in wins if pct_return(t) is not None]
        loss_returns = [pct_return(t) for t in (losses + partial) if pct_return(t) is not None]
        return {
            "n_closed": n,
            "wins": len(wins),
            "losses": len(losses),
            "stopped_after_partial_targets": len(partial),
            "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
            "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
            "avg_win_pct": round(sum(win_returns) / len(win_returns), 2) if win_returns else None,
            "avg_loss_pct": round(sum(loss_returns) / len(loss_returns), 2) if loss_returns else None,
        }

    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "overall": stats_for(closed),
        "real_only": stats_for([t for t in closed if t.get("type") == "real"]),
        "paper_only": stats_for([t for t in closed if t.get("type") == "paper"]),
        "note": "n_closed under ~20-30 is not statistically meaningful yet — treat as directional only.",
    }


def main():
    data = load_json(TRADES_PATH, {"trades": []})
    scan = load_json(SCAN_PATH, {"coins": []})
    lookup = build_price_lookup(scan)

    filled = 0
    changed = 0
    for trade in data.get("trades", []):
        coin = lookup.get(trade.get("asset_id"))
        if coin is None:
            continue

        if trade.get("status") == "pending":
            if check_pending(trade, coin):
                filled += 1
            continue  # don't also run open-trade checks the same run it filled

        if trade.get("status") != "open":
            continue

        before = trade.get("status")
        n_targets_before = len(trade.get("targets_hit", []))
        check_open(trade, coin)
        if trade.get("status") != before or len(trade.get("targets_hit", [])) != n_targets_before:
            changed += 1

    TRADES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = summarize(data.get("trades", []))
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Checked {len(data.get('trades', []))} trades: {filled} pending order(s) filled, "
          f"{changed} status/target change(s) this run. "
          f"Overall win rate so far: {summary['overall']['win_rate_pct']}")


if __name__ == "__main__":
    main()
