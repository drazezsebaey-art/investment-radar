"""
Investment Radar - Scalp/Momentum Signals (v2)
------------------------------------------------------------
v2 fix (2026-09-21, caught by Azez): v1 only wrote a live SNAPSHOT to
data/scalp-signals.json, overwritten every run - there was no persistent
trade record, so nothing could ever be tracked to a win/loss outcome. That
defeated the entire point of this track (accumulate labeled data faster).
v2 actually opens lightweight paper trades into data/scalp-trades.json,
tracked the same way as auto_paper_trade.py's real trades (see
track_trades.py's process_trades, reused for this file too) - separate
file, separate performance summary, never mixed with config/trades.json,
data/shadow-trades.json, or SCALP's own real $100 fund.

Reuses breakout_check.py's OUTPUT (data/radar-flags.json) instead of
re-fetching or re-running select_rotating_candidates - that data-fetching
is the expensive, rate-limited part, and it already ran once this cycle.
This script adds NO new API calls.

Stop is ALWAYS the ATR-based trend_following_stop when available (tight,
volatility-sized, from the current price), never the wider
liquidity-buffered support/trendline stop the main radar uses - matching
the "narrow timeframe, narrow risk" intent for this track. Falls back to a
plain 2xATR stop when the coin hasn't hit the 3-run streak yet.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
RADAR_FLAGS_PATH = BASE_DIR / "data" / "radar-flags.json"
SCALP_SIGNALS_PATH = BASE_DIR / "data" / "scalp-signals.json"   # live snapshot, kept for a quick glance
SCALP_TRADES_PATH = BASE_DIR / "data" / "scalp-trades.json"     # v2: the actual persistent trade record

SCALP_MIN_SCORE = 30  # deliberately lower than AUTO_TRADE_MIN_SCORE (40) - this track's whole
                        # point is to log more candidates for learning, not to gate tightly

# v3 (2026-09-21, Azez): fast-reject gates added AFTER seeing this track had
# neither an R:R floor nor an overbought check - unlike SCALP's own fast-
# reject gate (min 1.5:1 R:R, RSI>75 rejected). This track uses a slightly
# looser R:R floor (it's a data-collection track, not real capital), but
# the SAME RSI ceiling as SCALP for consistency. Deliberately NOT adding a
# concurrent-open-trades cap - Azez wants this track to open as many
# trades as qualify so there's more outcome data to analyze later.
SCALP_MIN_RR = 1.3
SCALP_RSI_OVERBOUGHT = 75.0
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
    """v19 (22/9/2026): extended to match the v18 fix already applied to
    auto_paper_trade.py's get_fired_coins() - without this, a priority_review
    (Layer 2 early-signal) coin would never reach the scalp track at all,
    even though this track's whole purpose is accumulating outcome data
    faster than the main radar. AUTO_TRADE... (SCALP_MIN_SCORE below) still
    gates which of these actually get a trade opened, exactly as before."""
    coins = radar_data.get("coins", [])
    return [c for c in coins if c.get("breakout_signal") or c.get("extension_continuation_signal")
            or c.get("trendline_break_confirmed_signal")
            or (c.get("priority_review") and c.get("confidence_score") is not None)]


def determine_trigger(coin: dict) -> str:
    """v19: same helper as auto_paper_trade.py - records which signal(s)
    fired for this coin so scalp-trades.json can be filtered by trigger
    type the same way as the other two tracks, without a separate file."""
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


def compute_stop_and_targets(coin: dict):
    entry = coin.get("price_usd")
    stop = coin.get("trend_following_stop")
    targets = coin.get("trend_following_targets")
    used_tf_stop = stop is not None

    if stop is None:
        # No trend-following eligibility yet (streak < 3) - fall back to a
        # plain ATR stop from THIS run alone, still never the wide
        # liquidity-buffered support stop the main radar uses, since this
        # track is deliberately tight-risk regardless of streak status.
        atr = coin.get("atr_value")
        if entry is not None and atr is not None:
            stop = round(entry - 2.0 * atr, 8)
            risk = entry - stop
            targets = [round(entry + risk * m, 8) for m in (1.5, 2.5, 4.0)]

    return entry, stop, targets, used_tf_stop


def build_scalp_signal(coin: dict) -> dict:
    entry, stop, targets, used_tf_stop = compute_stop_and_targets(coin)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "asset_id": coin["id"],
        "symbol": coin["symbol"],
        "price_usd": entry,
        "stop": stop,
        "targets": targets,
        "used_trend_following_stop": used_tf_stop,
        "confidence_score": coin.get("confidence_score"),
        "score_streak": coin.get("score_streak"),
        "trend_following_eligible": coin.get("trend_following_eligible", False),
        "signal_quality": coin.get("signal_quality"),
        "relative_strength_pct": coin.get("relative_strength_pct"),
        "oi_price_relationship": coin.get("oi_price_relationship"),
    }


def has_open_scalp_trade(asset_id: str, trades: list) -> bool:
    return any(t.get("asset_id") == asset_id and t.get("status") in OPEN_STATUSES for t in trades)


def fast_reject_reason(coin: dict, entry, stop, targets):
    """v3: mirrors SCALP's own fast-reject gate (min R:R, RSI overbought
    ceiling) for this track - catches exactly the two gaps Azez flagged:
    no R:R floor and no overbought check. Returns None if the candidate
    passes, or a short string reason if it should be rejected."""
    if entry is None or stop is None or not targets:
        return "missing entry/stop/targets"

    risk = entry - stop
    if risk <= 0:
        return "non-positive risk (stop not below entry)"
    reward = targets[0] - entry
    rr = reward / risk
    if rr < SCALP_MIN_RR:
        return f"R:R {rr:.2f} below floor {SCALP_MIN_RR}"

    rsi = (coin.get("indicators") or {}).get("rsi14")
    if rsi is not None and rsi > SCALP_RSI_OVERBOUGHT:
        return f"RSI {rsi:.1f} above overbought ceiling {SCALP_RSI_OVERBOUGHT}"

    return None


def build_scalp_trade(coin: dict) -> dict:
    entry, stop, targets, used_tf_stop = compute_stop_and_targets(coin)
    now = datetime.now(timezone.utc)
    date_str = now.date().isoformat()
    return {
        "id": f"{coin['symbol'].lower()}-scalp-{now.strftime('%Y%m%dT%H%M%S')}",
        "asset_id": coin["id"],
        "symbol": coin["symbol"],
        "type": "paper",
        "track": "scalp",
        "triggered_by": determine_trigger(coin),
        "date_opened": date_str,
        "status": "open",
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "used_trend_following_stop": used_tf_stop,
        "created_at": now.isoformat(),
        "filled_at": now.isoformat(),
        "actual_entry": entry,
        "targets_hit": [],
        "confidence_score_at_entry": coin.get("confidence_score"),
        "score_streak_at_entry": coin.get("score_streak"),
        "signal_quality_at_entry": coin.get("signal_quality"),
    }


def main():
    radar_data = load_json(RADAR_FLAGS_PATH, {"coins": []})
    fired = get_fired_coins(radar_data)
    qualifying = [c for c in fired if (c.get("confidence_score") or 0) >= SCALP_MIN_SCORE]

    # Live snapshot - kept for a quick "who qualifies right now" glance,
    # but this is NOT the trade record (that's scalp-trades.json below).
    signals = sorted((build_scalp_signal(c) for c in qualifying), key=lambda s: -(s.get("confidence_score") or 0))
    save_json(SCALP_SIGNALS_PATH, {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "Live snapshot only - see data/scalp-trades.json for the actual tracked paper trades.",
        "signals": signals,
    })

    # v2: the actual persistent, trackable paper trades.
    trades_data = load_json(SCALP_TRADES_PATH, {"trades": []})
    trades = trades_data.get("trades", [])

    n_opened, n_skipped_dup, n_skipped_no_stop, n_rejected = 0, 0, 0, 0
    for coin in qualifying:
        if has_open_scalp_trade(coin["id"], trades):
            n_skipped_dup += 1
            continue
        entry, stop, targets, _ = compute_stop_and_targets(coin)
        if entry is None or stop is None:
            n_skipped_no_stop += 1
            continue
        reason = fast_reject_reason(coin, entry, stop, targets)
        if reason:
            n_rejected += 1
            print(f"  Scalp fast-reject {coin['symbol']}: {reason}")
            continue
        trades.append(build_scalp_trade(coin))
        n_opened += 1

    trades_data["trades"] = trades
    save_json(SCALP_TRADES_PATH, trades_data)

    n_trend_following = sum(1 for s in signals if s["trend_following_eligible"])
    print(f"Scalp signals: {len(signals)} qualifying (score >= {SCALP_MIN_SCORE}), "
          f"{n_trend_following} trend-following-eligible. "
          f"Trades: {n_opened} opened, {n_skipped_dup} skipped (already open), "
          f"{n_skipped_no_stop} skipped (no valid stop), {n_rejected} fast-rejected (R:R/RSI).")


if __name__ == "__main__":
    main()
