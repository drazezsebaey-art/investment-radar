"""
Investment Radar - Automatic Paper Trades + Shadow Log (v9)
------------------------------------------------------------
Context (2026-09-20 discussion): manually reviewing a chart before opening
each paper trade was the actual bottleneck limiting how much data
accumulates in trades.json / performance-summary.json - not the entry
criteria being too strict. This removes the human step from TRADE CREATION
while keeping the exact same bar: a coin only gets an auto-trade if its v8
confidence_score (computed in breakout_check.py, same weights as every
manual review) clears AUTO_TRADE_MIN_SCORE.

Signals that fire but DON'T clear the bar are not simply discarded - they're
logged to data/shadow-trades.json with the same entry/stop/target math,
tagged with why they were rejected. Shadow trades are evaluated the same
way as real ones (see track_trades.py's shadow pass) but never count toward
performance-summary.json - they exist purely to answer "what would have
happened if we'd said yes anyway", which is exactly the buy-and-hold-vs-
signal question this whole line of discussion started from.

Run order: after breakout_check.py (needs its confidence_score output),
before track_trades.py (so a same-run price-history snapshot doesn't
predate a trade opened this run - track_trades.py's own created_at/
filled_at guards already handle that either way, but this ordering is
the more natural fit and mirrors how manual trades were added).
"""
import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
RADAR_FLAGS_PATH = BASE_DIR / "data" / "radar-flags.json"
TRADES_PATH = BASE_DIR / "config" / "trades.json"
SHADOW_TRADES_PATH = BASE_DIR / "data" / "shadow-trades.json"

AUTO_TRADE_MIN_SCORE = 40          # agreed starting threshold - same bar as a manual "worth reviewing" call
RISK_REWARD_TIERS = [1.5, 2.5, 4.0]  # tiered targets as multiples of (entry - stop), matching the manual AAVE/SKY trades' shape
OPEN_STATUSES = {"open", "pending"}


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_fired_coins(radar_data: dict) -> list:
    coins = radar_data.get("coins", [])
    return [c for c in coins if c.get("breakout_signal") or c.get("extension_continuation_signal")
            or c.get("trendline_break_confirmed_signal")]


def has_open_position(asset_id: str, trades: list, shadow_trades: list) -> bool:
    """Never open a second auto-trade (real or shadow) for a coin that
    already has one live - avoids the log filling with duplicate entries
    every single run a coin keeps firing the same ongoing signal."""
    for t in trades + shadow_trades:
        if t.get("asset_id") == asset_id and t.get("status") in OPEN_STATUSES:
            return True
    return False


def pick_stop(coin: dict, entry: float):
    """Prefer whichever liquidity-buffered stop (v8.1) is present and valid
    (below entry, for a long) - resistance-based and trendline-based can
    both exist; the tighter one is used since it's the more conservative
    risk figure for sizing the trade off of."""
    candidates = [
        coin.get("suggested_stop_resistance_based"),
        coin.get("suggested_stop_trendline_based"),
    ]
    valid = [s for s in candidates if s is not None and s < entry]
    if not valid:
        return None
    return max(valid)  # the tighter (higher, closer to entry) of the valid stops


def build_trade(coin: dict, entry: float, stop: float, kind: str, reason: str = None) -> dict:
    risk = entry - stop
    targets = [round(entry + risk * mult, 8) for mult in RISK_REWARD_TIERS]
    now = datetime.now(timezone.utc)
    date_str = now.date().isoformat()
    trade = {
        "id": f"{coin['symbol'].lower()}-{kind}-{date_str}",
        "asset_id": coin["id"],
        "symbol": coin["symbol"],
        "type": "paper",
        "auto": True,
        "date_opened": date_str,
        "status": "open",
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "created_at": now.isoformat(),
        "filled_at": now.isoformat(),
        "actual_entry": entry,
        "targets_hit": [],
        "confidence_score_at_entry": coin.get("confidence_score"),
        "signal_quality_at_entry": coin.get("signal_quality"),
    }
    if reason:
        trade["rejected_reason"] = reason
    return trade


def main():
    radar_data = load_json(RADAR_FLAGS_PATH, {"coins": []})
    trades_data = load_json(TRADES_PATH, {"trades": []})
    shadow_data = load_json(SHADOW_TRADES_PATH, {"trades": []})

    trades = trades_data.get("trades", [])
    shadow_trades = shadow_data.get("trades", [])

    fired = get_fired_coins(radar_data)
    n_auto_opened = 0
    n_shadow_logged = 0
    n_skipped_no_stop = 0
    n_skipped_duplicate = 0

    for coin in fired:
        asset_id = coin["id"]
        entry = coin.get("price_usd")
        score = coin.get("confidence_score")
        if entry is None or score is None:
            continue

        if has_open_position(asset_id, trades, shadow_trades):
            n_skipped_duplicate += 1
            continue

        stop = pick_stop(coin, entry)
        if stop is None:
            # No valid computed stop for this coin this run - can't size a
            # trade responsibly, so it's skipped entirely (not even logged
            # as shadow, since there's nothing concrete to compare against).
            n_skipped_no_stop += 1
            continue

        if score >= AUTO_TRADE_MIN_SCORE:
            trades.append(build_trade(coin, entry, stop, kind="auto"))
            n_auto_opened += 1
        else:
            reason = f"confidence_score {score} < AUTO_TRADE_MIN_SCORE {AUTO_TRADE_MIN_SCORE}"
            shadow_trades.append(build_trade(coin, entry, stop, kind="shadow", reason=reason))
            n_shadow_logged += 1

    trades_data["trades"] = trades
    shadow_data["trades"] = shadow_trades
    save_json(TRADES_PATH, trades_data)
    save_json(SHADOW_TRADES_PATH, shadow_data)

    print(f"Auto paper trades: {n_auto_opened} opened, {n_shadow_logged} logged to shadow, "
          f"{n_skipped_duplicate} skipped (already open), {n_skipped_no_stop} skipped (no valid stop).")


if __name__ == "__main__":
    main()
