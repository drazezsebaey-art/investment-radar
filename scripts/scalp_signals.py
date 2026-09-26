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
ENGINE_VERSION = "scalp_signals-v35"  # v48 (24/9/2026): schema/version tagging per the audit report
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
RADAR_FLAGS_PATH = BASE_DIR / "data" / "radar-flags.json"
REJECTIONS_LOG_PATH = BASE_DIR / "data" / "rejections-log.json"
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

# v20 (22/9/2026): this track was silently reusing the MAIN swing-trade
# track's ATR multiples - either straight from breakout_check.py's
# trend_following_stop/targets (2x ATR stop, 1.5/2.5/4.0x risk targets) or
# recomputed identically in the old fallback branch - despite this file's
# own docstring claiming "narrow timeframe, narrow risk." Confirmed live:
# 0 of 28 open scalp trades had closed after 4-23 hours, with average
# stop/target distances of ~9.7%/~14.7% from entry - move sizes that
# typically take DAYS for an altcoin, not the hours this track is meant to
# operate on. Fixed by deriving stop/targets ONLY from atr_value with
# scalp-specific (tighter) multipliers - trend_following_stop/targets are
# never touched here anymore (that sizing stays correct for the main
# track, it was just wrong reused here).
SCALP_ATR_STOP_MULT = 0.75
# v27 fix (22/9/2026, found via the dashboard - 0 scalp trades opened since
# v20 despite 15+ qualifying-by-score candidates every run): v20 set the
# first target multiple to 1.0x risk, which makes R:R mathematically
# EXACTLY 1.0 for every single candidate, always failing the pre-existing
# SCALP_MIN_RR (1.3) fast-reject gate above - a silent, total deadlock
# between two independently-reasonable-looking fixes. The tighter ABSOLUTE
# price distance v20 wanted already comes entirely from the smaller stop
# multiplier (0.75x ATR vs the old 2.0x) - reverting these ratios to the
# original 1.5/2.5/4.0x restores a passing R:R (1.5 at target 1) while
# keeping every absolute distance far tighter than pre-v20, since the risk
# unit itself (0.75x ATR) is now much smaller than before.
SCALP_TARGET_RISK_MULTS = (1.5, 2.5, 4.0)


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
    type the same way as the other two tracks, without a separate file.

    v35 fix (24/9/2026): same "new flag, old gate" gap fixed in
    auto_paper_trade.py's copy of this function - v25's three chart-pattern
    early signals were never recognized here either."""
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
        flag_pattern = early.get("flag_pattern") or {}
        if flag_pattern.get("direction") == "bullish":
            reasons.append("flag_pattern")
        if early.get("double_bottom"):
            reasons.append("double_bottom")
        if early.get("triangle"):
            reasons.append("triangle")
        triggers.append("priority_review:" + "+".join(reasons) if reasons else "priority_review")
    return ",".join(triggers) if triggers else "unknown"


CONFIRMED_TRIGGER_NAMES = {"breakout_signal", "extension_continuation", "trendline_break_confirmed"}


def classify_entry_archetype(triggered_by: str) -> str:
    """v35: same archetype classifier as auto_paper_trade.py - see that
    file for the full rationale."""
    parts = [p for p in triggered_by.split(",") if p]
    confirmed = [p for p in parts if p in CONFIRMED_TRIGGER_NAMES]
    proactive_part = next((p for p in parts if p.startswith("priority_review")), None)
    proactive_reasons = []
    if proactive_part and ":" in proactive_part:
        proactive_reasons = proactive_part.split(":", 1)[1].split("+")

    if confirmed and proactive_part:
        return "confluence_confirmed_plus_proactive"
    if len(confirmed) >= 2:
        return "confluence_multi_confirmed"
    if confirmed:
        return confirmed[0]
    if proactive_part:
        if len(proactive_reasons) >= 2:
            return "proactive_confluence"
        if not proactive_reasons:
            return "priority_review_unspecified"
        reason = proactive_reasons[0]
        return {
            "squeeze": "squeeze_expansion",
            "choch_bullish": "structure_shift",
            "cluster_rotation_lag": "rotation_lag",
            "rsi_divergence": "rsi_divergence_reversal",
            "relative_strength_consolidation": "hidden_relative_strength",
            "flag_pattern": "chart_pattern_flag",
            "double_bottom": "chart_pattern_double_bottom",
            "triangle": "chart_pattern_triangle",
        }.get(reason, f"proactive_{reason}")
    return "unknown"


def compute_stop_and_targets(coin: dict):
    """v20: always derives stop/targets from atr_value using the
    scalp-specific multiples above - never reuses breakout_check.py's
    trend_following_stop/targets, which are correctly sized for the MAIN
    swing-trade track's multi-day holding period, not this track's
    intended hours-to-a-day horizon. used_trend_following_stop is kept in
    the trade schema for continuity with the other two tracks' records,
    but is always False here now - this track no longer uses that
    (swing-sized) stop at all, regardless of the coin's own trend-
    following eligibility."""
    entry = coin.get("price_usd")
    atr = coin.get("atr_value")
    if entry is None or atr is None:
        return entry, None, None, False
    stop = round(entry - SCALP_ATR_STOP_MULT * atr, 8)
    risk = entry - stop
    targets = [round(entry + risk * m, 8) for m in SCALP_TARGET_RISK_MULTS]
    return entry, stop, targets, False


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


def log_rejection(coin: dict, stage: str, rejection_codes: list, details: dict) -> None:
    """v31 (24/9/2026): Rejection Engine - shared log with auto_paper_trade.py
    (data/rejections-log.json). See that file's log_rejection for the full
    rationale."""
    log = {"rejections": []}
    if REJECTIONS_LOG_PATH.exists():
        try:
            log = json.loads(REJECTIONS_LOG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    log.setdefault("rejections", []).append({
        "asset_id": coin.get("id"), "symbol": coin.get("symbol"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "rejection_codes": rejection_codes,
        "score_at_rejection": coin.get("confidence_score"),
        "details": details,
    })
    REJECTIONS_LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def build_scalp_trade(coin: dict) -> dict:
    entry, stop, targets, used_tf_stop = compute_stop_and_targets(coin)
    now = datetime.now(timezone.utc)
    date_str = now.date().isoformat()
    triggered_by = determine_trigger(coin)
    return {
        "id": f"{coin['symbol'].lower()}-scalp-{now.strftime('%Y%m%dT%H%M%S')}",
        "asset_id": coin["id"],
        "symbol": coin["symbol"],
        "type": "paper",
        "track": "scalp",
        "triggered_by": triggered_by,
        "entry_archetype": classify_entry_archetype(triggered_by),
        "engine_version": ENGINE_VERSION,
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
            log_rejection(coin, "scalp_trade", ["NO_STOP_CANDIDATE"], {"entry": entry, "atr_value": coin.get("atr_value")})
            continue
        reason = fast_reject_reason(coin, entry, stop, targets)
        if reason:
            n_rejected += 1
            code = "RR_BELOW_FLOOR" if "R:R" in reason else ("RSI_OVERBOUGHT" if "RSI" in reason else "OTHER")
            log_rejection(coin, "scalp_trade", [code], {"reason_text": reason, "entry": entry, "stop": stop, "targets": targets})
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
