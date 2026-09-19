"""
Investment Radar - Trade Tracker (v2)
---------------------------------------
Same auto-close logic as v1 (checks open trades against the latest 24h
high/low, closes on stop or target touch), plus: after every run, computes
a performance summary from all CLOSED trades (real + paper combined, and
separately) and writes data/performance-summary.json — a running, always-
current win-rate instead of a number someone has to calculate by hand.

Conservative rule unchanged: if both stop and target look touched in the same
24h window, default to STOPPED and flag the conflict rather than assume success.
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


def check_trade(trade: dict, coin: dict) -> None:
    low = coin.get("low_24h_usd")
    high = coin.get("high_24h_usd")
    stop = trade.get("stop")
    target_low = trade.get("target_low")

    stop_hit = stop is not None and low is not None and low <= stop
    target_hit = target_low is not None and high is not None and high >= target_low
    now = datetime.now(timezone.utc).isoformat()

    if stop_hit and target_hit:
        trade["status"] = "stopped"
        trade["exit_price"] = stop
        trade["date_closed"] = now
        trade["note"] = (
            "⚠️ تعارض: الستوب والهدف الاتنين ظهروا متلمسين في نفس نافذة الـ24 ساعة — "
            "معتبرينها ستوب كافتراض متحفظ، الترتيب الفعلي مش مؤكد من البيانات دي."
        )
    elif stop_hit:
        trade["status"] = "stopped"
        trade["exit_price"] = stop
        trade["date_closed"] = now
    elif target_hit:
        trade["status"] = "target_hit"
        trade["exit_price"] = target_low
        trade["date_closed"] = now


def pct_return(trade: dict) -> float:
    entry = trade.get("entry")
    exit_price = trade.get("exit_price")
    if entry is None or exit_price is None or entry == 0:
        return None
    return round((exit_price - entry) / entry * 100, 2)


def summarize(trades: list) -> dict:
    closed = [t for t in trades if t.get("status") in ("target_hit", "stopped")]

    def stats_for(subset: list) -> dict:
        n = len(subset)
        wins = [t for t in subset if t.get("status") == "target_hit"]
        losses = [t for t in subset if t.get("status") == "stopped"]
        returns = [pct_return(t) for t in subset if pct_return(t) is not None]
        win_returns = [pct_return(t) for t in wins if pct_return(t) is not None]
        loss_returns = [pct_return(t) for t in losses if pct_return(t) is not None]
        return {
            "n_closed": n,
            "wins": len(wins),
            "losses": len(losses),
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

    changed = 0
    for trade in data.get("trades", []):
        if trade.get("status") != "open":
            continue
        coin = lookup.get(trade.get("asset_id"))
        if coin is None:
            continue
        before = trade.get("status")
        check_trade(trade, coin)
        if trade.get("status") != before:
            changed += 1

    TRADES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = summarize(data.get("trades", []))
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Checked {len(data.get('trades', []))} trades, {changed} closed this run. "
          f"Overall win rate so far: {summary['overall']['win_rate_pct']}")


if __name__ == "__main__":
    main()
