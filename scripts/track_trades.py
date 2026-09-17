"""
Investment Radar - Trade Tracker
---------------------------------
Runs right after scan.py (same 15-minute cron), reads config/trades.json
(the SCALP fund's real + paper trade log) and data/market-scan.json
(the fresh snapshot scan.py just wrote), and auto-closes any "open" trade
whose stop or target was touched within the last 24h window.

Conservative rule: if a trade's stop AND target both look touched in the
same 24h window, we cannot tell which happened first from this data alone.
We default to STOPPED (the worse outcome) and flag the trade with a
"conflict" note rather than silently assuming success.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
TRADES_PATH = BASE_DIR / "config" / "trades.json"
SCAN_PATH = BASE_DIR / "data" / "market-scan.json"


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def build_price_lookup(scan: dict) -> dict:
    lookup = {}
    for coin in scan.get("coins", []):
        lookup[coin["id"]] = coin
    return lookup


def check_trade(trade: dict, coin: dict) -> dict:
    """Mutates and returns the trade dict if a close condition is met."""
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

    return trade


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
            continue  # asset not in this scan's universe right now; leave open
        before = trade.get("status")
        check_trade(trade, coin)
        if trade.get("status") != before:
            changed += 1

    TRADES_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Checked {len(data.get('trades', []))} trades, {changed} closed this run.")


if __name__ == "__main__":
    main()
