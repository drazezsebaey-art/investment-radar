"""
Investment Radar - Trade Tracker (v4)
---------------------------------------
v4 additions: Profit Factor (gross profit / gross loss, using summed % return
per trade as a proxy for dollar P&L since position sizing isn't tracked) and
Recovery Factor (net profit / max drawdown, both computed from a compounded
equity curve built by sorting closed trades chronologically) - standard
metrics from live-trading-performance literature, added alongside win rate
for a more complete picture of risk-adjusted performance.

CRITICAL FIX from v3: pending-order fills were checked against the coin's
rolling 24h low (low_24h_usd from market-scan.json), which looks backward
24 hours from THE MOMENT OF THE CHECK - not from when the order was placed.
A price dip that happened BEFORE the pending order even existed could
therefore be wrongly counted as "the market came down and filled my order
after I placed it." This is a real, serious bug: it can report a fill that
never actually happened in the order's real lifetime.

Fix: pending fills are now checked against data/price-history.json (our own
timestamped 15-minute snapshots), filtered to ONLY points recorded strictly
AFTER the order's created_at timestamp. The first run that sees a new
pending trade (no created_at yet) just stamps created_at = now and does NOT
fill it that same run - fill detection only begins from snapshots taken
after that stamp, so no pre-existing price action can count.

Trade-off: precision is limited to the ~15-minute snapshot interval (the
same approximation already disclosed everywhere else in this system), and
a coin needs to already be accumulating history (flagged before, or in the
watchlist) for this to work - if data/price-history.json has no entries yet
for that coin, the pending order simply won't fill until history starts
accumulating for it (which happens automatically the moment it's flagged).

v3 features preserved: multiple targets with targets_hit accumulation,
stopped_after_partial_targets classification, old single target_low trades
still supported.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
TRADES_PATH = BASE_DIR / "config" / "trades.json"
SCAN_PATH = BASE_DIR / "data" / "market-scan.json"
HISTORY_PATH = BASE_DIR / "data" / "price-history.json"
SUMMARY_PATH = BASE_DIR / "data" / "performance-summary.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def build_price_lookup(scan: dict) -> dict:
    return {coin["id"]: coin for coin in scan.get("coins", [])}


def get_targets(trade: dict) -> list:
    targets = trade.get("targets")
    if targets:
        return sorted(targets)
    if trade.get("target_low") is not None:
        return [trade["target_low"]]
    return []


def check_pending(trade: dict, price_history: dict) -> bool:
    """FIXED: only fills against price snapshots recorded strictly after the
    order's created_at timestamp - never against a rolling 24h low that can
    include price action from before the order existed.

    Returns True if the trade was filled this run.
    """
    entry = trade.get("entry")
    if entry is None:
        return False

    now_iso = datetime.now(timezone.utc).isoformat()

    if "created_at" not in trade:
        # First time we've seen this pending order - stamp it now and stop.
        # We deliberately do NOT check for a fill on this same run: doing so
        # would risk using a snapshot from the very same 15-min bucket that
        # predates our own knowledge of the order, recreating the same class
        # of bug this fix exists to close. Fill-checking starts next run.
        trade["created_at"] = now_iso
        return False

    points = price_history.get(trade["asset_id"], [])
    post_order_points = [
        p for p in points
        if p.get("t") and p.get("t") > trade["created_at"] and p.get("price") is not None
    ]
    hit = next((p for p in post_order_points if p["price"] <= entry), None)
    if hit:
        trade["status"] = "open"
        trade["filled_at"] = hit["t"]
        trade["actual_entry"] = entry
        return True
    return False


def check_open(trade: dict, coin: dict, price_history: dict) -> None:
    """v5 FIX (caught by Azez, 2026-09-20): this was still using the coin's
    raw rolling low_24h_usd/high_24h_usd to test stop/target hits - the
    EXACT same bug class the v4 fix closed for pending-order fills, just
    left open here. A price touch from BEFORE the trade was even filled
    could sit inside that 24h window and get wrongly counted as "the stop/
    target got hit after I opened this position." Fixed the same way: only
    price-history.json snapshots recorded strictly after the trade's own
    fill time count. If no such snapshot exists yet (this run is the first
    one after the fill), falls back to the coin's single current spot price
    only - never the 24h window, which is exactly what caused the bug."""
    stop = trade.get("stop")
    targets = get_targets(trade)
    now = datetime.now(timezone.utc).isoformat()

    since = trade.get("filled_at") or trade.get("created_at") or trade.get("date_opened")
    points = price_history.get(trade["asset_id"], [])
    post_fill_points = [
        p for p in points
        if p.get("t") and since and p.get("t") > since and p.get("price") is not None
    ]
    if post_fill_points:
        low = min(p["price"] for p in post_fill_points)
        high = max(p["price"] for p in post_fill_points)
    else:
        # No snapshot recorded yet strictly after the fill - use only the
        # coin's current spot price, not the rolling 24h window.
        current = coin.get("price_usd")
        low = high = current

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
        trade["status"] = "stopped_after_partial_targets" if trade["targets_hit"] else "stopped"
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


def pct_return(trade: dict) -> float:
    entry = trade.get("actual_entry", trade.get("entry"))
    exit_price = trade.get("exit_price")
    if entry is None or exit_price is None or entry == 0:
        return None
    return round((exit_price - entry) / entry * 100, 2)


CLOSED_STATUSES = ("closed_targets_complete", "stopped", "stopped_after_partial_targets", "target_hit")


def build_equity_curve(subset: list) -> list:
    """Sort by close date and compound returns into an equity curve starting
    at 100, for max-drawdown and recovery-factor calculation. Uses % return
    per trade (no position-sizing data available), so this is a proxy for a
    real equity curve, not a dollar-accurate one."""
    dated = [t for t in subset if t.get("date_closed") and pct_return(t) is not None]
    dated.sort(key=lambda t: t["date_closed"])
    equity = 100.0
    curve = [equity]
    for t in dated:
        equity *= (1 + pct_return(t) / 100)
        curve.append(equity)
    return curve


def compute_max_drawdown_pct(curve: list) -> float:
    if len(curve) < 2:
        return None
    peak = curve[0]
    max_dd = 0.0
    for v in curve[1:]:
        peak = max(peak, v)
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return round(max_dd, 2)


def stats_for(subset: list) -> dict:
    n = len(subset)
    wins = [t for t in subset if t.get("status") in ("closed_targets_complete", "target_hit")]
    partial = [t for t in subset if t.get("status") == "stopped_after_partial_targets"]
    losses = [t for t in subset if t.get("status") == "stopped"]
    returns = [pct_return(t) for t in subset if pct_return(t) is not None]
    win_returns = [pct_return(t) for t in wins if pct_return(t) is not None]
    loss_returns = [pct_return(t) for t in (losses + partial) if pct_return(t) is not None]

    # Profit Factor = gross profit / gross loss, using summed % returns per
    # trade as a proxy for dollar P&L (no position-sizing data tracked)
    gross_profit = sum(r for r in returns if r > 0)
    gross_loss = abs(sum(r for r in returns if r < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

    # Recovery Factor = net profit / max drawdown, both from a compounded
    # equity curve built by sorting closed trades chronologically
    curve = build_equity_curve(subset)
    max_dd = compute_max_drawdown_pct(curve)
    net_profit_pct = round(curve[-1] - 100, 2) if curve else None
    recovery_factor = round(net_profit_pct / max_dd, 2) if (max_dd and max_dd > 0) else None

    return {
        "n_closed": n,
        "wins": len(wins),
        "losses": len(losses),
        "stopped_after_partial_targets": len(partial),
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
        "avg_return_pct": round(sum(returns) / len(returns), 2) if returns else None,
        "avg_win_pct": round(sum(win_returns) / len(win_returns), 2) if win_returns else None,
        "avg_loss_pct": round(sum(loss_returns) / len(loss_returns), 2) if loss_returns else None,
        "profit_factor": profit_factor,
        "net_profit_pct_compounded": net_profit_pct,
        "max_drawdown_pct": max_dd,
        "recovery_factor": recovery_factor,
    }


def summarize(trades: list) -> dict:
    closed = [t for t in trades if t.get("status") in CLOSED_STATUSES]


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
    price_history = load_json(HISTORY_PATH, {})
    lookup = build_price_lookup(scan)

    filled = 0
    changed = 0
    for trade in data.get("trades", []):
        if trade.get("status") == "pending":
            if check_pending(trade, price_history):
                filled += 1
            continue

        if trade.get("status") != "open":
            continue

        coin = lookup.get(trade.get("asset_id"))
        if coin is None:
            continue

        before = trade.get("status")
        n_targets_before = len(trade.get("targets_hit", []))
        check_open(trade, coin, price_history)
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
