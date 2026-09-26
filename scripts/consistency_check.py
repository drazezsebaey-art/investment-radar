"""
Investment Radar - Consistency & Anomaly Checker (v1)
------------------------------------------------------------
Runs AFTER track_trades.py, reading the same output files everything else
already reads/writes (no new API calls, no new state beyond one small
timestamp file). Purpose: catch structural gaps, contradictions, and
missing data THIS RUN, instead of noticing them two reports later or via a
manual code audit.

Rationale (22/9/2026): three real bugs (v18, v19, v21) all had the exact
same shape - a new flag (priority_review) was introduced in one place, and
an old condition elsewhere in the codebase kept gating on the pre-existing
flags only, silently excluding the new case. That whole CLASS of bug is
what section A below defends against going forward. The rest of the
checks are a broader net: invalid math, cross-file contradictions the code
is SUPPOSED to prevent (verified independently, not just trusted), and
coverage gaps.

Findings are severity-tagged:
  error   - almost certainly a real bug (contradiction, invalid math,
            a field missing that should exist given other fields present)
  warning - probably fine, but worth a human glance (coverage gaps, thin
            history, a monitoring fallback persisting)

Never raises or fails the workflow - this is observability, not a gate.
Writes data/consistency-report.json (full detail, one entry per finding)
and prints every finding to the Action log so it's visible without
opening a file.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"

RADAR_FLAGS_PATH = DATA_DIR / "radar-flags.json"
TRADES_PATH = CONFIG_DIR / "trades.json"
SHADOW_TRADES_PATH = DATA_DIR / "shadow-trades.json"
SCALP_TRADES_PATH = DATA_DIR / "scalp-trades.json"
PRICE_HISTORY_PATH = DATA_DIR / "price-history.json"
INDICATOR_WEIGHTS_PATH = CONFIG_DIR / "indicator-weights.json"
KNOWN_UNLOCKS_PATH = CONFIG_DIR / "known-unlocks.json"
STATE_PATH = DATA_DIR / "consistency-check-state.json"
REPORT_PATH = DATA_DIR / "consistency-report.json"

# Mirrors breakout_check.py's DEFAULT_INDICATOR_WEIGHTS keys - kept in sync
# manually. Yes, this is exactly the kind of two-places-must-agree gap this
# whole script exists to catch, applied reflexively to itself; if a new
# weight key is ever added to the code and forgotten here, this specific
# check just won't fire for it - a known, accepted blind spot of a
# duplicated-constant approach, not worth a shared-module refactor yet.
DEFAULT_INDICATOR_WEIGHTS_KEYS = {
    "breakout_or_trendline_signal", "volume_confirmed", "trend_aligned",
    "idiosyncratic_quality", "oi_price_confirms", "funding_not_crowded",
    "relative_strength_bonus", "deep_drawdown_penalty", "unlock_risk_penalty",
}
AUTO_TRADE_MIN_SCORE = 40
MAX_RUN_GAP_MINUTES = 60   # cron target is 30 min; flag anything more than double that
THIN_HISTORY_MIN_POINTS = 8
THIN_HISTORY_ALERT_COUNT = 20
UNLOCK_UNCOVERED_ALERT_PCT = 50
ATR_PCT_EXTREME = 60.0


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def finding(severity: str, category: str, message: str, **context) -> dict:
    return {"severity": severity, "category": category, "message": message, **context}


# =========================================================================
# A) The "new flag, old gate" class of bug - radar-flags.json internal
#    pipeline-completeness checks
# =========================================================================

def check_priority_review_pipeline(coins: list) -> list:
    out = []
    for c in coins:
        if not c.get("priority_review") or not c.get("binance_listed"):
            continue  # not escalated, or correctly excluded (not tradeable)
        sym = c.get("symbol", c.get("id"))
        if c.get("confidence_score") is None:
            # v51 fix (26/9/2026): the v37 cap (MAX_TOTAL_CANDIDATES_PER_RUN)
            # means most priority_review coins on a busy run legitimately
            # never reach the funnel at all this run - that's the cap doing
            # its job, not a pipeline gap. funnel_stage is only ever set for
            # a coin that DID get processed this run (breakout_check.py's
            # ranking pass) - a coin with no funnel_stage simply never got a
            # turn, which is expected and not worth an error. Only a coin
            # that WAS selected into the funnel (has a funnel_stage) but
            # still ended up without a score represents a genuine gap.
            if c.get("funnel_stage") is None:
                continue
            out.append(finding("error", "priority_review_pipeline",
                                f"{sym}: priority_review+binance_listed reached funnel_stage={c.get('funnel_stage')} but no confidence_score", symbol=sym))
            continue
        if c.get("confidence_breakdown") is None:
            out.append(finding("error", "priority_review_pipeline",
                                f"{sym}: has confidence_score but no confidence_breakdown - unauditable score", symbol=sym))
        if c.get("oi_price_relationship") in (None, "unknown") and c.get("open_interest_now") is None:
            out.append(finding("warning", "priority_review_pipeline",
                                f"{sym}: has a confidence_score but OI/funding data was never fetched for it", symbol=sym))
    return out


def check_trend_following_consistency(coins: list) -> list:
    out = []
    for c in coins:
        sym = c.get("symbol", c.get("id"))
        if c.get("trend_following_eligible"):
            if c.get("trend_following_stop") is None:
                out.append(finding("error", "trend_following", f"{sym}: eligible=true but trend_following_stop missing", symbol=sym))
            if (c.get("score_streak") or 0) < 3:
                out.append(finding("error", "trend_following", f"{sym}: eligible=true but score_streak={c.get('score_streak')}", symbol=sym))
        price, tf_stop = c.get("price_usd"), c.get("trend_following_stop")
        if price is not None and tf_stop is not None and tf_stop >= price:
            out.append(finding("error", "trend_following", f"{sym}: trend_following_stop ({tf_stop}) not below price ({price})", symbol=sym))
    return out


def check_unlock_flag_consistency(coins: list) -> list:
    out = []
    for c in coins:
        if c.get("unlock_risk_flag") and not c.get("unlock_data_checked"):
            sym = c.get("symbol", c.get("id"))
            out.append(finding("error", "unlock_data", f"{sym}: unlock_risk_flag=true but unlock_data_checked=false", symbol=sym))
    return out


def check_stop_target_math(coins: list) -> list:
    out = []
    for c in coins:
        sym = c.get("symbol", c.get("id"))
        price = c.get("price_usd")
        if price is None:
            continue
        for stop_field in ("suggested_stop_resistance_based", "suggested_stop_trendline_based", "trend_following_stop"):
            v = c.get(stop_field)
            if v is not None and v >= price:
                out.append(finding("error", "stop_math", f"{sym}: {stop_field}={v} not below price={price}", symbol=sym))
        targets = c.get("trend_following_targets") or []
        if targets:
            if any(t <= price for t in targets):
                out.append(finding("error", "target_math", f"{sym}: a trend_following_target is not above current price", symbol=sym))
            if targets != sorted(targets):
                out.append(finding("error", "target_math", f"{sym}: trend_following_targets not ascending: {targets}", symbol=sym))
    return out


def check_signal_quality_pairing(coins: list) -> list:
    out = []
    for c in coins:
        has_score = c.get("confidence_score") is not None
        has_quality = c.get("signal_quality") is not None
        if has_score != has_quality:
            sym = c.get("symbol", c.get("id"))
            out.append(finding("error", "signal_quality", f"{sym}: confidence_score present={has_score} but signal_quality present={has_quality}", symbol=sym))
    return out


def check_rsi_bounds(coins: list) -> list:
    out = []
    for c in coins:
        rsi = (c.get("indicators") or {}).get("rsi14")
        if rsi is not None and not (0 <= rsi <= 100):
            out.append(finding("error", "data_sanity", f"{c.get('symbol')}: rsi14={rsi} out of 0-100 range", symbol=c.get("symbol")))
    return out


def check_atr_sanity(coins: list) -> list:
    out = []
    for c in coins:
        sym = c.get("symbol", c.get("id"))
        atr_pct = c.get("atr_pct_of_price")
        if atr_pct is None:
            continue
        if atr_pct <= 0:
            out.append(finding("error", "data_sanity", f"{sym}: atr_pct_of_price={atr_pct} <= 0", symbol=sym))
        elif atr_pct > ATR_PCT_EXTREME:
            out.append(finding("warning", "data_sanity", f"{sym}: atr_pct_of_price={atr_pct}% is extreme - verify candle data", symbol=sym))
    return out


# =========================================================================
# B) Cross-file trade consistency - verifying invariants the code is
#    SUPPOSED to guarantee, independently of trusting that code
# =========================================================================

def check_real_trade_score_gate(trades: list) -> list:
    out = []
    for t in trades:
        if t.get("auto") and (t.get("confidence_score_at_entry") or 0) < AUTO_TRADE_MIN_SCORE:
            out.append(finding("error", "trade_gate",
                                f"{t.get('symbol')} ({t.get('id')}): real auto trade with confidence_score_at_entry="
                                f"{t.get('confidence_score_at_entry')} < {AUTO_TRADE_MIN_SCORE}", trade_id=t.get("id")))
    return out


def check_trade_math(trades: list, label: str) -> list:
    out = []
    for t in trades:
        tid, sym = t.get("id"), t.get("symbol")
        entry = t.get("actual_entry") or t.get("entry")
        stop = t.get("stop")
        targets = t.get("targets") or []
        if entry is not None and stop is not None and stop >= entry:
            out.append(finding("error", "trade_math", f"[{label}] {sym} ({tid}): stop {stop} not below entry {entry}", trade_id=tid))
        if targets and entry is not None:
            if any(target <= entry for target in targets):
                out.append(finding("error", "trade_math", f"[{label}] {sym} ({tid}): a target is not above entry", trade_id=tid))
            if targets != sorted(targets):
                out.append(finding("error", "trade_math", f"[{label}] {sym} ({tid}): targets not ascending: {targets}", trade_id=tid))
        hit_values = {h["target"] for h in t.get("targets_hit", [])}
        if hit_values and not hit_values.issubset(set(targets)):
            out.append(finding("error", "trade_math", f"[{label}] {sym} ({tid}): targets_hit contains a value not in targets", trade_id=tid))
    return out


def check_duplicate_ids(labeled_trade_lists: list) -> list:
    out, seen = [], {}
    for label, trades in labeled_trade_lists:
        for t in trades:
            tid = t.get("id")
            if tid in seen:
                out.append(finding("error", "duplicate_id", f"trade id '{tid}' appears in both {seen[tid]} and {label}", trade_id=tid))
            else:
                seen[tid] = label
    return out


def check_cross_track_double_open(real_trades: list, shadow_trades: list) -> list:
    """Verifies find_open_trade()'s promotion guarantee actually holds for
    AUTOMATED real trades - a manually-added real trade (e.g. a hand-placed
    limit order) coexisting with an open shadow entry is an accepted,
    deliberate pattern (22/9/2026 decision: a specific limit strategy and
    general signal tracking are different things, not a duplicate
    position) and is intentionally NOT flagged here. Only an auto:true real
    trade sharing an open shadow entry is a genuine has_open_position/
    find_open_trade failure."""
    out = []
    real_open_auto_assets = {t["asset_id"] for t in real_trades
                              if t.get("status") in ("open", "pending") and t.get("auto")}
    for t in shadow_trades:
        if t.get("status") == "open" and t.get("asset_id") in real_open_auto_assets:
            out.append(finding("error", "double_position",
                                f"{t.get('symbol')}: OPEN in shadow AND an AUTO real trade simultaneously - should have been superseded",
                                trade_id=t.get("id")))
    return out


def check_orphaned_promotions(shadow_trades: list, real_trades: list) -> list:
    real_assets = {t["asset_id"] for t in real_trades}
    out = []
    for t in shadow_trades:
        if t.get("status") == "superseded" and t.get("asset_id") not in real_assets:
            # v53 fix (26/9/2026): a manually-annotated orphan (its "note"
            # field already explains why - e.g. the real trade it promoted
            # to was voided and deleted) is documented audit history, not a
            # live gap - keep it visible as a warning so it's never
            # silently lost, but stop escalating it as an error every run.
            severity = "warning" if t.get("note") else "error"
            out.append(finding(severity, "orphaned_promotion",
                                f"{t.get('symbol')}: shadow trade marked superseded but no real trade exists for this asset"
                                + (f" - {t['note']}" if t.get("note") else ""),
                                trade_id=t.get("id")))
    return out


def check_closed_trade_completeness(trades: list, label: str) -> list:
    out = []
    for t in trades:
        if t.get("status") in ("stopped", "stopped_after_partial_targets", "closed_targets_complete"):
            if t.get("exit_price") is None or t.get("date_closed") is None:
                out.append(finding("error", "incomplete_close",
                                    f"[{label}] {t.get('symbol')} ({t.get('id')}): status={t['status']} but missing exit_price/date_closed",
                                    trade_id=t.get("id")))
    return out


def check_stale_monitoring(trades: list, label: str) -> list:
    """A trade repeatedly falling back to current-spot-only monitoring isn't
    a bug by itself, but flags exactly the blind spot the v16 OKX fix was
    built to close, if it recurs for a specific symbol."""
    out = []
    for t in trades:
        if t.get("status") == "open" and t.get("last_check_source") == "current_spot_fallback":
            out.append(finding("warning", "monitoring_gap",
                                f"[{label}] {t.get('symbol')} ({t.get('id')}): monitored via current-spot-only fallback - no candle/snapshot data reaching it",
                                trade_id=t.get("id")))
    return out


# =========================================================================
# C) Coverage / missing data
# =========================================================================

def check_unlock_coverage(open_trades_all_tracks: list, unlocks_config: dict) -> list:
    checked_ids = set(unlocks_config.keys()) - {"_readme"}
    open_ids = {t["asset_id"] for t in open_trades_all_tracks}
    if not open_ids:
        return []
    uncovered = open_ids - checked_ids
    pct = len(uncovered) / len(open_ids) * 100
    if pct > UNLOCK_UNCOVERED_ALERT_PCT:
        return [finding("warning", "unlock_coverage",
                         f"{len(uncovered)}/{len(open_ids)} ({pct:.0f}%) coins with an open trade have no researched unlock schedule at all")]
    return []


def check_weight_config_drift(weights_file_keys: set) -> list:
    out = []
    missing = DEFAULT_INDICATOR_WEIGHTS_KEYS - weights_file_keys
    extra = weights_file_keys - DEFAULT_INDICATOR_WEIGHTS_KEYS - {"_readme"}
    if missing:
        out.append(finding("warning", "weight_config", f"indicator-weights.json missing keys the code defines: {sorted(missing)}"))
    if extra:
        out.append(finding("warning", "weight_config", f"indicator-weights.json has keys the code no longer uses: {sorted(extra)}"))
    return out


def check_thin_history(price_history: dict) -> list:
    thin = [cid for cid, points in price_history.items() if 0 < len(points) < THIN_HISTORY_MIN_POINTS]
    if len(thin) > THIN_HISTORY_ALERT_COUNT:
        return [finding("warning", "thin_history",
                         f"{len(thin)} coins have fewer than {THIN_HISTORY_MIN_POINTS} price-history points - Layer 2 structure signals can't evaluate them yet")]
    return []


# =========================================================================
# D) Timing
# =========================================================================

def check_run_gap(current_updated_at, state: dict) -> list:
    last = state.get("last_updated_at")
    if not last or not current_updated_at:
        return []
    try:
        gap_min = (datetime.fromisoformat(current_updated_at) - datetime.fromisoformat(last)).total_seconds() / 60
    except ValueError:
        return []
    if gap_min > MAX_RUN_GAP_MINUTES:
        return [finding("warning", "run_gap", f"{gap_min:.0f} minutes since the previous run (target ~30) - check the external cron")]
    return []


def main():
    radar = load_json(RADAR_FLAGS_PATH, {"coins": []})
    coins = radar.get("coins", [])
    real_trades = load_json(TRADES_PATH, {"trades": []}).get("trades", [])
    shadow_trades = load_json(SHADOW_TRADES_PATH, {"trades": []}).get("trades", [])
    scalp_trades = load_json(SCALP_TRADES_PATH, {"trades": []}).get("trades", [])
    price_history = load_json(PRICE_HISTORY_PATH, {})
    weights_file = load_json(INDICATOR_WEIGHTS_PATH, {})
    unlocks_config = load_json(KNOWN_UNLOCKS_PATH, {})
    state = load_json(STATE_PATH, {})

    findings = []
    findings += check_priority_review_pipeline(coins)
    findings += check_trend_following_consistency(coins)
    findings += check_unlock_flag_consistency(coins)
    findings += check_stop_target_math(coins)
    findings += check_signal_quality_pairing(coins)
    findings += check_rsi_bounds(coins)
    findings += check_atr_sanity(coins)

    findings += check_real_trade_score_gate(real_trades)
    findings += check_trade_math(real_trades, "real")
    findings += check_trade_math(shadow_trades, "shadow")
    findings += check_trade_math(scalp_trades, "scalp")
    findings += check_duplicate_ids([("real", real_trades), ("shadow", shadow_trades), ("scalp", scalp_trades)])
    findings += check_cross_track_double_open(real_trades, shadow_trades)
    findings += check_orphaned_promotions(shadow_trades, real_trades)
    findings += check_closed_trade_completeness(real_trades, "real")
    findings += check_closed_trade_completeness(shadow_trades, "shadow")
    findings += check_closed_trade_completeness(scalp_trades, "scalp")
    findings += check_stale_monitoring(real_trades, "real")
    findings += check_stale_monitoring(shadow_trades, "shadow")
    findings += check_stale_monitoring(scalp_trades, "scalp")

    all_open = [t for t in real_trades + shadow_trades + scalp_trades if t.get("status") in ("open", "pending")]
    findings += check_unlock_coverage(all_open, unlocks_config)
    findings += check_weight_config_drift(set(weights_file.keys()))
    findings += check_thin_history(price_history)

    findings += check_run_gap(radar.get("updated_at"), state)

    errors = [f for f in findings if f["severity"] == "error"]
    warnings = [f for f in findings if f["severity"] == "warning"]

    REPORT_PATH.write_text(json.dumps({
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "n_errors": len(errors),
        "n_warnings": len(warnings),
        "findings": findings,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    STATE_PATH.write_text(json.dumps({"last_updated_at": radar.get("updated_at")}, ensure_ascii=False), encoding="utf-8")

    print(f"Consistency check: {len(errors)} error(s), {len(warnings)} warning(s).")
    for f in errors:
        print(f"  ERROR [{f['category']}] {f['message']}")
    for f in warnings:
        print(f"  WARN  [{f['category']}] {f['message']}")


if __name__ == "__main__":
    main()
