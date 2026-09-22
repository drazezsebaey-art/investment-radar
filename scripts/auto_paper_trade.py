"""
Investment Radar - Automatic Paper Trades + Shadow Log (v15)
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

v15 fixes (from the 22/9/2026 committee audit, confirmed live on ZAMA):
  1. has_open_position() used to block a real trade FOREVER once a coin had
     ANY open shadow entry, even after its score later cleared the bar -
     ZAMA scored 30 on 20/9 (shadow-logged), then 68 on 21/9, and never got
     promoted because the old check only asked "is anything open", not
     "is anything REAL open". find_open_trade() + the promotion branch in
     main() below fix this: a live real trade still blocks a duplicate, but
     a shadow entry gets marked "superseded" and promoted instead of
     silently blocking forever.
  2. pick_stop() used to ALWAYS use the liquidity-buffered resistance/
     trendline stop, even for trend_following_eligible coins - which for an
     extended trender (no nearby support, by definition) meant a stop 50%+
     below entry instead of the correct ATR-based trend_following_stop
     breakout_check.py already computes for exactly this case. Now checked
     first.

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
    """v18 fix (22/9/2026): a Layer-2 early-signal coin (priority_review)
    that got deep-evaluated this run - and therefore has a confidence_score
    - now enters this list too, regardless of that score. The
    AUTO_TRADE_MIN_SCORE gate below still decides real vs shadow exactly as
    before; this only decides whether a coin is considered AT ALL. Without
    this, a quiet-consolidation/early-CHoCH coin with a genuinely strong
    confidence_score would still never reach shadow-trades.json (let alone
    a real trade) until it ALSO produced an actual price breakout - directly
    defeating the point of catching it before it moves. This does NOT lower
    AUTO_TRADE_MIN_SCORE's bar for real money - a weak-scoring early signal
    still only reaches the shadow log, same as any other weak signal."""
    coins = radar_data.get("coins", [])
    return [c for c in coins if c.get("breakout_signal") or c.get("extension_continuation_signal")
            or c.get("trendline_break_confirmed_signal")
            or (c.get("priority_review") and c.get("confidence_score") is not None)]


def find_open_trade(asset_id: str, trades: list):
    """Returns the open/pending trade for this asset in the given list, or
    None. Deliberately checked against `trades` (real) and `shadow_trades`
    SEPARATELY in main() below now, not merged into one boolean like the
    old has_open_position() - the two cases need different handling."""
    for t in trades:
        if t.get("asset_id") == asset_id and t.get("status") in OPEN_STATUSES:
            return t
    return None


def pick_stop(coin: dict, entry: float):
    """v15 fix: a trend_following_eligible coin (extended move, no nearby
    support by definition) must use the ATR-based trend_following_stop
    breakout_check.py already computes for it - the liquidity-buffered
    resistance/trendline stops below are anchored to a support level the
    price may not have visited in weeks, which for an extended trender
    isn't a "more conservative" stop, it's simply the wrong one (this is
    exactly what happened reviewing ZAMA on 22/9/2026: the old-style stops
    sat 50%+ below entry). Falls back to the pre-v15 logic when trend-
    following mode isn't active for this coin."""
    if coin.get("trend_following_eligible") and coin.get("trend_following_stop") is not None:
        tf_stop = coin["trend_following_stop"]
        if tf_stop < entry:
            return tf_stop, True
    candidates = [
        coin.get("suggested_stop_resistance_based"),
        coin.get("suggested_stop_trendline_based"),
    ]
    valid = [s for s in candidates if s is not None and s < entry]
    if not valid:
        return None, False
    return max(valid), False  # the tighter (higher, closer to entry) of the valid stops


def determine_trigger(coin: dict) -> str:
    """v19 (22/9/2026): records exactly which signal(s) fired for this coin,
    so a future report can compare early-signal (Layer 2/priority_review)
    performance against confirmed-signal (breakout/extension/trendline)
    performance WITHOUT a separate trades file - just filter config/trades.json,
    shadow-trades.json, and scalp-trades.json by this field. A coin can
    satisfy more than one condition at once (e.g. both extension_continuation
    and priority_review); all applicable ones are listed, comma-separated,
    in the same order get_fired_coins() checks them."""
    triggers = []
    if coin.get("breakout_signal"):
        triggers.append("breakout_signal")
    if coin.get("extension_continuation_signal"):
        triggers.append("extension_continuation")
    if coin.get("trendline_break_confirmed_signal"):
        triggers.append("trendline_break_confirmed")
    if coin.get("priority_review") and coin.get("confidence_score") is not None:
        early = coin.get("early_signals") or {}
        reasons = []
        structure = early.get("structure") or {}
        if structure.get("signal") == "CHoCH_bullish":
            reasons.append("choch_bullish")
        if early.get("volatility_squeeze"):
            reasons.append("squeeze")
        if early.get("bullish_rsi_divergence"):
            reasons.append("rsi_divergence")
        if early.get("cluster_rotation_lag"):
            reasons.append("cluster_rotation_lag")
        if early.get("relative_strength_consolidation"):
            reasons.append("relative_strength_consolidation")
        triggers.append("priority_review:" + "+".join(reasons) if reasons else "priority_review")
    return ",".join(triggers) if triggers else "unknown"


def build_trade(coin: dict, entry: float, stop: float, kind: str, used_tf_stop: bool,
                 reason: str = None, now: datetime = None) -> dict:
    risk = entry - stop
    targets = [round(entry + risk * mult, 8) for mult in RISK_REWARD_TIERS]
    now = now or datetime.now(timezone.utc)
    date_str = now.date().isoformat()
    trade = {
        "id": f"{coin['symbol'].lower()}-{kind}-{date_str}",
        "asset_id": coin["id"],
        "symbol": coin["symbol"],
        "type": "paper",
        "auto": True,
        "triggered_by": determine_trigger(coin),
        "date_opened": date_str,
        "status": "open",
        "entry": entry,
        "stop": stop,
        "used_trend_following_stop": used_tf_stop,
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
    now = datetime.now(timezone.utc)

    fired = get_fired_coins(radar_data)
    n_auto_opened = 0
    n_promoted = 0
    n_shadow_logged = 0
    n_skipped_no_stop = 0
    n_skipped_duplicate = 0

    for coin in fired:
        asset_id = coin["id"]
        entry = coin.get("price_usd")
        score = coin.get("confidence_score")
        if entry is None or score is None:
            continue

        # v15 fix: a live REAL trade always blocks a duplicate - never
        # touched automatically.
        if find_open_trade(asset_id, trades) is not None:
            n_skipped_duplicate += 1
            continue

        stop, used_tf_stop = pick_stop(coin, entry)
        if stop is None:
            # No valid computed stop for this coin this run - can't size a
            # trade responsibly, so it's skipped entirely (not even logged
            # as shadow, since there's nothing concrete to compare against).
            n_skipped_no_stop += 1
            continue

        open_shadow = find_open_trade(asset_id, shadow_trades)

        if score >= AUTO_TRADE_MIN_SCORE:
            if open_shadow is not None:
                # v15 fix: promote instead of silently staying blocked -
                # the coin's score has since crossed the real bar, so the
                # earlier shadow entry is superseded, not still "current".
                open_shadow["status"] = "superseded"
                open_shadow["superseded_at"] = now.isoformat()
                open_shadow["superseded_reason"] = (
                    f"promoted to a real auto trade at confidence_score {score}"
                )
                n_promoted += 1
            trades.append(build_trade(coin, entry, stop, kind="auto", used_tf_stop=used_tf_stop, now=now))
            n_auto_opened += 1
        else:
            if open_shadow is not None:
                # Already logged in shadow at an equal-or-lower bar this
                # run's signal doesn't beat - nothing new to record.
                n_skipped_duplicate += 1
                continue
            reason = f"confidence_score {score} < AUTO_TRADE_MIN_SCORE {AUTO_TRADE_MIN_SCORE}"
            shadow_trades.append(
                build_trade(coin, entry, stop, kind="shadow", used_tf_stop=used_tf_stop, reason=reason, now=now)
            )
            n_shadow_logged += 1

    trades_data["trades"] = trades
    shadow_data["trades"] = shadow_trades
    save_json(TRADES_PATH, trades_data)
    save_json(SHADOW_TRADES_PATH, shadow_data)

    print(f"Auto paper trades: {n_auto_opened} opened ({n_promoted} promoted from shadow), "
          f"{n_shadow_logged} logged to shadow, {n_skipped_duplicate} skipped (already open), "
          f"{n_skipped_no_stop} skipped (no valid stop).")


if __name__ == "__main__":
    main()
