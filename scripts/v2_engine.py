"""
V2 Engine - independent short-swing discovery/scoring engine
------------------------------------------------------------
Step 15 of the V2-merge plan (24/9/2026). This is the FIRST piece of the
genuinely separate, parallel V2 track - it does NOT touch config/trades.json,
data/shadow-trades.json, or data/scalp-trades.json, and it never will. Per
the agreed design: the existing system's ATR-relative target logic and
decision-making are completely untouched; V2 runs as its own independent
evaluation on the same underlying data.

Reads radar-flags.json's coins that already reached the top two funnel
tiers (funnel_stage: agent_room_priority / shortlist_10, from breakout_check.py
steps 8-9) - these carry real 4h-candle SMC fields already computed at zero
extra API cost:
  fair_value_gaps    (step 10, quality-graded, never boolean)
  liquidity_sweep    (step 11, confidence-graded: detected/confirmed/high_quality)
  inducement         (step 12, same confidence grading, stricter geometry)
  order_block        (step 13, quality-graded: weak/moderate/strong)
  real_structure     (step 14, BOS/CHoCH on real candles)
plus market_regime (step 4) and the existing confidence_score/evidence_clusters.

Score components (max 100), deliberately built as CLUSTERS (no double-
counting correlated evidence - inducement and liquidity_sweep describe
overlapping/nested evidence, so only the stronger of the two is credited,
never both):
  liquidity_sequence   25  - inducement (preferred, stricter) else liquidity_sweep
  displacement_fvg      20  - best unmitigated FVG quality (halved if only a mitigated one exists)
  order_block           15  - freshness + displacement quality
  structure             20  - CHoCH_bullish (reversal) weighted above BOS_bullish (continuation)
  market_regime         10  - context only, matching V2's own principle that
                               regime is never a standalone trade signal
  relative_strength     10  - reuses the EXISTING system's own idiosyncratic/
                               relative-strength read, not a new computation

Writes data/v2-candidates.json every run - a live snapshot only. Trade
opening (step 18) and its own target framework (step 17) are separate,
later pieces - this step only discovers and scores.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RADAR_FLAGS_PATH = DATA_DIR / "radar-flags.json"
V2_CANDIDATES_PATH = DATA_DIR / "v2-candidates.json"
V2_SHADOW_TRADES_PATH = DATA_DIR / "v2-shadow-trades.json"

TOP_FUNNEL_TIERS = {"agent_room_priority", "shortlist_10"}
V2_TRADEABLE_STATES = {"HIGH_PRIORITY_SETUP", "WATCH"}

FVG_QUALITY_POINTS = {"exceptional": 20, "strong": 15, "moderate": 8, "weak": 3}
LIQUIDITY_CONFIDENCE_POINTS_INDUCEMENT = {"high_quality": 25, "confirmed": 18, "detected": 8}
LIQUIDITY_CONFIDENCE_POINTS_SWEEP = {"high_quality": 20, "confirmed": 12, "detected": 5}
OB_QUALITY_POINTS = {"strong": 15, "moderate": 8, "weak": 2}
STRUCTURE_POINTS = {"CHoCH_bullish": 20, "BOS_bullish": 15, "CHoCH_bearish": 0, "BOS_bearish": 0}
REGIME_POINTS = {"risk_on": 10, "neutral": 0, "risk_off": -10, "unavailable": 0}

# V2 section 66's decision-state vocabulary - deliberately NOT buy/sell terms,
# this is a research radar producing candidates, not orders (order-opening
# logic and its own thresholds come in step 18).
SCORE_HIGH_PRIORITY = 70
SCORE_WATCH = 55
SCORE_WAIT = 40


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def score_liquidity_sequence(coin: dict) -> tuple:
    """Inducement and liquidity_sweep describe overlapping evidence (a sweep
    is often PART of what makes an inducement sequence) - crediting both in
    full would double-count the same underlying event. Inducement, being the
    stricter/rarer pattern, is preferred when both are present."""
    inducement = coin.get("inducement")
    if inducement is not None:
        points = LIQUIDITY_CONFIDENCE_POINTS_INDUCEMENT.get(inducement.get("confidence"), 0)
        return points, f"inducement:{inducement.get('confidence')}"
    sweep = coin.get("liquidity_sweep")
    if sweep is not None:
        points = LIQUIDITY_CONFIDENCE_POINTS_SWEEP.get(sweep.get("confidence"), 0)
        return points, f"liquidity_sweep:{sweep.get('confidence')}"
    return 0, "none"


def score_displacement_fvg(coin: dict) -> tuple:
    fvgs = coin.get("fair_value_gaps") or []
    if not fvgs:
        return 0, "none"
    unmitigated = [g for g in fvgs if not g.get("mitigated")]
    best = unmitigated[0] if unmitigated else fvgs[0]
    points = FVG_QUALITY_POINTS.get(best.get("quality"), 0)
    if best.get("mitigated"):
        points = points // 2
    return points, f"fvg:{best.get('quality')}{'_mitigated' if best.get('mitigated') else ''}"


def score_order_block(coin: dict) -> tuple:
    ob = coin.get("order_block")
    if ob is None:
        return 0, "none"
    return OB_QUALITY_POINTS.get(ob.get("quality"), 0), f"order_block:{ob.get('quality')}"


def score_structure(coin: dict) -> tuple:
    structure = coin.get("real_structure")
    if structure is None:
        return 0, "none"
    signal = structure.get("signal")
    return STRUCTURE_POINTS.get(signal, 0), f"structure:{signal}"


def score_regime(market_regime: dict) -> tuple:
    state = (market_regime or {}).get("risk_state", "unavailable")
    return REGIME_POINTS.get(state, 0), f"regime:{state}"


def score_relative_strength(coin: dict) -> tuple:
    """Reuses the EXISTING system's own read (signal_quality /
    relative_strength_pct) rather than recomputing anything - the whole
    point of V2 being a parallel evaluation, not a parallel data pipeline."""
    if coin.get("signal_quality") == "idiosyncratic":
        return 10, "idiosyncratic"
    rs = coin.get("relative_strength_pct")
    if rs is not None and rs >= 15.0:
        return 5, f"relative_strength:{rs}%"
    return 0, "beta_driven_or_weak"


def classify_decision_state(score: int) -> str:
    if score >= SCORE_HIGH_PRIORITY:
        return "HIGH_PRIORITY_SETUP"
    if score >= SCORE_WATCH:
        return "WATCH"
    if score >= SCORE_WAIT:
        return "WAIT_FOR_CONFIRMATION"
    return "NO_TRADE"


def classify_archetype(coin: dict) -> str:
    """V2 section 43's named entry archetypes, matched to whichever
    evidence actually fired for this candidate - not a claim that a full
    textbook sequence played out end to end."""
    has_inducement = coin.get("inducement") is not None
    has_sweep = coin.get("liquidity_sweep") is not None
    has_fvg = bool(coin.get("fair_value_gaps"))
    has_ob = coin.get("order_block") is not None
    structure_signal = (coin.get("real_structure") or {}).get("signal")
    is_choch = structure_signal == "CHoCH_bullish"
    is_bos = structure_signal == "BOS_bullish"

    if has_inducement and has_fvg and is_choch:
        return "B_inducement_sweep_choch_fvg"
    if has_sweep and has_fvg and is_bos:
        return "A_liquidity_sweep_displacement_fvg_bos"
    if has_fvg and is_bos:
        return "D_breakout_retest_continuation"
    if has_ob and not (has_sweep or has_inducement):
        return "order_block_retest_only"
    if has_sweep or has_inducement:
        return "liquidity_event_only"
    if has_fvg:
        return "fvg_only"
    return "structure_only"


def score_v2_candidate(coin: dict, market_regime: dict) -> dict:
    liq_points, liq_note = score_liquidity_sequence(coin)
    fvg_points, fvg_note = score_displacement_fvg(coin)
    ob_points, ob_note = score_order_block(coin)
    struct_points, struct_note = score_structure(coin)
    regime_points, regime_note = score_regime(market_regime)
    rs_points, rs_note = score_relative_strength(coin)

    breakdown = {
        "liquidity_sequence": {"points": liq_points, "max": 25, "detail": liq_note},
        "displacement_fvg": {"points": fvg_points, "max": 20, "detail": fvg_note},
        "order_block": {"points": ob_points, "max": 15, "detail": ob_note},
        "structure": {"points": struct_points, "max": 20, "detail": struct_note},
        "market_regime": {"points": regime_points, "max": 10, "detail": regime_note},
        "relative_strength": {"points": rs_points, "max": 10, "detail": rs_note},
    }
    total = sum(c["points"] for c in breakdown.values())
    total = max(0, min(100, total))

    return {
        "asset_id": coin.get("id"), "symbol": coin.get("symbol"),
        "v2_score": total,
        "v2_score_breakdown": breakdown,
        "v2_decision_state": classify_decision_state(total),
        "v2_archetype": classify_archetype(coin),
        "funnel_stage": coin.get("funnel_stage"),
        "price_usd": coin.get("price_usd"),
    }


REJECTIONS_LOG_PATH = DATA_DIR / "rejections-log.json"

# --- Entry Quality Gate - step 16 of the V2-merge plan --------------------
# V2 section 59, scoped to data actually available. Deliberately does NOT
# include R:R or "target blocked by resistance" here - those need the
# target framework itself (step 17), not yet built; adding them here would
# mean guessing at numbers step 17 is specifically responsible for. Also
# deliberately does NOT hard-reject on market regime alone - V2 itself
# states regime is context, not a standalone signal (already applied as a
# score component in step 15); a genuinely hostile regime shows up as a
# lower score, not a second penalty here.
V2_MIN_VOLUME_24H_USD = 1_000_000
V2_EXTREME_FUNDING_ABS = 0.0005
V2_MIN_REAL_CANDLES = 15


def log_v2_rejection(coin: dict, v2_result: dict, reasons: list) -> None:
    log = {"rejections": []}
    if REJECTIONS_LOG_PATH.exists():
        try:
            log = json.loads(REJECTIONS_LOG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    log.setdefault("rejections", []).append({
        "asset_id": coin.get("id"), "symbol": coin.get("symbol"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stage": "v2_engine", "rejection_codes": reasons,
        "score_at_rejection": v2_result.get("v2_score"),
        "details": {"v2_decision_state_before_gate": v2_result.get("v2_decision_state")},
    })
    REJECTIONS_LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


# --- Target Framework - step 17 of the V2-merge plan (v2, corrected) ------
# Azez's correction (24/9/2026) to the first version of this step: a strict
# 72-hour cutoff was throwing away genuinely good opportunities - a coin
# that can realistically deliver +9% over 5 days, or +12% over 10 days,
# should be ACCEPTED, not rejected for being "too slow" against a fixed
# short target. Redesigned around three TIME HORIZONS instead of three
# fixed percentages: at each horizon, the target size is whatever this
# coin's own ATR pace projects (floor = not worth entering for that
# horizon if the pace can't even clear it; no ceiling on the upside per
# Azez's explicit follow-up correction - the number stands as-is, never
# artificially capped downward). A setup is only rejected if NONE of the
# three horizons - up to two full weeks - clear their own floor.
V2_TIME_HORIZONS = [
    {"label": "fast", "max_hours": 48, "floor_pct": 3.0},
    {"label": "medium", "max_hours": 168, "floor_pct": 5.0},     # up to 1 week
    {"label": "extended", "max_hours": 336, "floor_pct": 8.0},   # up to 2 weeks
]
V2_CANDLE_HOURS = 4          # real_candles granularity (breakout_check.py's OHLC_DAYS window)
V2_STOP_ATR_MULT = 1.0       # ATR-relative stop, NOT a fixed percentage - keeps risk sizing consistent across coins of different volatility


def project_move_pct(atr_value, entry_price, hours: float):
    """Linear projection of this coin's OWN historical 4h-ATR pace forward
    to `hours` - not a guarantee, a pace-based estimate of what's plausible
    given how this specific coin has actually been moving."""
    if not atr_value or not entry_price or entry_price <= 0:
        return None
    n_candles = hours / V2_CANDLE_HOURS
    return atr_value * n_candles / entry_price * 100


def compute_v2_target_framework(coin: dict) -> dict:
    entry = coin.get("price_usd")
    atr = coin.get("atr_value")
    resistance_levels = coin.get("all_resistance_levels") or []
    stop = round(entry - V2_STOP_ATR_MULT * atr, 8) if entry and atr else None
    risk = (entry - stop) if (entry and stop) else None

    targets = []
    for horizon in V2_TIME_HORIZONS:
        projected = project_move_pct(atr, entry, horizon["max_hours"])
        if projected is None or projected < horizon["floor_pct"]:
            # this coin's own pace can't even clear the floor for this
            # horizon - not a target worth proposing here, not forced
            targets.append({
                "horizon_label": horizon["label"], "max_hours": horizon["max_hours"],
                "projected_pct_at_horizon": round(projected, 2) if projected is not None else None,
                "target_pct": None, "target_price": None, "feasible": False,
                "rr": None, "obstacles_in_path": [], "path_quality": None,
            })
            continue
        pct = projected  # no ceiling per Azez's explicit request - the number stands as the pace-based estimate, not artificially capped
        target_price = round(entry * (1 + pct / 100), 8)
        rr = round((target_price - entry) / risk, 2) if (risk and risk > 0) else None
        obstacles = [lvl for lvl in resistance_levels if entry < lvl < target_price]
        targets.append({
            "horizon_label": horizon["label"], "max_hours": horizon["max_hours"],
            "projected_pct_at_horizon": round(projected, 2),
            "target_pct": round(pct, 2), "target_price": target_price, "feasible": True,
            "rr": rr, "obstacles_in_path": obstacles,
            "path_quality": "clear" if not obstacles else ("moderate" if len(obstacles) == 1 else "crowded"),
        })

    return {"entry": entry, "stop": stop, "targets": targets,
            "any_target_feasible": any(t["feasible"] for t in targets)}


def apply_v2_entry_quality_gate(coin: dict, target_framework: dict = None) -> list:
    """Returns a list of rejection codes; empty list = passes the gate."""
    reasons = []
    real_candles = coin.get("real_candles") or []
    if len(real_candles) < V2_MIN_REAL_CANDLES:
        reasons.append("DATA_QUALITY_INSUFFICIENT")
    if (coin.get("volume_24h_usd") or 0) < V2_MIN_VOLUME_24H_USD:
        reasons.append("POOR_LIQUIDITY")
    funding = coin.get("latest_funding_rate")
    if funding is not None and abs(funding) >= V2_EXTREME_FUNDING_ABS:
        reasons.append("EXTREME_FUNDING")
    if coin.get("oi_price_relationship") == "diverges":
        reasons.append("OI_CROWDING_RISK")
    if coin.get("volume_confirmed") is False:
        reasons.append("NO_VOLUME_CONFIRMATION")
    lifecycle = coin.get("opportunity_lifecycle") or {}
    if lifecycle.get("decay_state") == "decayed":
        reasons.append("OVEREXTENDED_OPPORTUNITY_DECAYED")
    structure_signal = (coin.get("real_structure") or {}).get("signal")
    liquidity_present = coin.get("inducement") is not None or coin.get("liquidity_sweep") is not None
    if structure_signal in ("BOS_bearish", "CHoCH_bearish") and liquidity_present:
        reasons.append("CONTRADICTORY_EVIDENCE")
    if target_framework is not None and not target_framework.get("any_target_feasible", True):
        reasons.append("TARGET_TIMEFRAME_TOO_SLOW")
    return reasons


# --- V2 shadow trade opening - step 18 of the V2-merge plan ---------------
# The requirement Azez set explicitly at the very start of the V2-merge
# plan: every candidate the V2 engine actually qualifies (passes the gate,
# reaches WATCH or HIGH_PRIORITY_SETUP) gets a REAL trade record here, not
# just a score - otherwise there is no way to ever know if V2 is actually
# better than the existing system, only whether its scoring LOOKS
# reasonable. Writes to its own file, data/v2-shadow-trades.json - never
# touches config/trades.json, data/shadow-trades.json, or
# data/scalp-trades.json, matching the "fully separate, parallel track"
# decision from the original merge-plan discussion.
def has_open_v2_trade(asset_id, trades: list) -> bool:
    return any(t.get("asset_id") == asset_id and t.get("status") == "open" for t in trades)


def build_v2_trade(coin: dict, result: dict, market_regime: dict, now: datetime) -> dict:
    tf = result["v2_target_framework"]
    feasible_targets = [t for t in tf["targets"] if t["feasible"]]
    date_str = now.date().isoformat()
    return {
        "id": f"{result['symbol'].lower()}-v2-{now.strftime('%Y%m%dT%H%M%S')}",
        "asset_id": result["asset_id"], "symbol": result["symbol"],
        "type": "paper", "track": "v2_shortswing",
        "status": "open",
        "entry": tf["entry"], "actual_entry": tf["entry"], "stop": tf["stop"],
        "targets": feasible_targets,
        "targets_hit": [],
        "v2_score": result["v2_score"], "v2_score_breakdown": result["v2_score_breakdown"],
        "v2_decision_state": result["v2_decision_state"], "v2_final_status": result["v2_final_status"],
        "v2_archetype": result["v2_archetype"], "funnel_stage": result["funnel_stage"],
        "market_regime_at_entry": market_regime,
        "date_opened": date_str, "created_at": now.isoformat(), "filled_at": now.isoformat(),
    }


def open_v2_trades(scored: list, coin_by_id: dict, market_regime: dict) -> int:
    trades_data = load_json(V2_SHADOW_TRADES_PATH, {"trades": []})
    trades = trades_data.get("trades", [])
    now = datetime.now(timezone.utc)
    n_opened = 0
    for result in scored:
        if result["v2_final_status"] not in V2_TRADEABLE_STATES:
            continue
        asset_id = result["asset_id"]
        if has_open_v2_trade(asset_id, trades):
            continue
        coin = coin_by_id.get(asset_id, {})
        trades.append(build_v2_trade(coin, result, market_regime, now))
        n_opened += 1
    trades_data["trades"] = trades
    V2_SHADOW_TRADES_PATH.write_text(json.dumps(trades_data, ensure_ascii=False, indent=2), encoding="utf-8")
    return n_opened


def main():
    radar = load_json(RADAR_FLAGS_PATH, {"coins": []})
    coins = radar.get("coins", [])
    market_regime = radar.get("market_regime")

    top_tier_coins = [c for c in coins if c.get("funnel_stage") in TOP_FUNNEL_TIERS and c.get("real_candles")]

    scored = [score_v2_candidate(c, market_regime) for c in top_tier_coins]
    coin_by_id = {c.get("id"): c for c in top_tier_coins}

    for result in scored:
        coin = coin_by_id.get(result["asset_id"], {})
        target_framework = compute_v2_target_framework(coin)
        result["v2_target_framework"] = target_framework
        gate_reasons = apply_v2_entry_quality_gate(coin, target_framework)
        result["v2_gate_passed"] = not gate_reasons
        result["v2_gate_reject_reasons"] = gate_reasons
        if gate_reasons and result["v2_decision_state"] in ("HIGH_PRIORITY_SETUP", "WATCH"):
            log_v2_rejection(coin, result, gate_reasons)
            result["v2_final_status"] = "REJECTED_BY_GATE"
        else:
            result["v2_final_status"] = result["v2_decision_state"]

    scored.sort(key=lambda r: -r["v2_score"])

    n_high_priority = sum(1 for r in scored if r["v2_final_status"] == "HIGH_PRIORITY_SETUP")
    n_watch = sum(1 for r in scored if r["v2_final_status"] == "WATCH")
    n_gate_rejected = sum(1 for r in scored if r["v2_final_status"] == "REJECTED_BY_GATE")
    n_trades_opened = open_v2_trades(scored, coin_by_id, market_regime)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market_regime": market_regime,
        "n_candidates_evaluated": len(scored),
        "n_high_priority": n_high_priority,
        "n_watch": n_watch,
        "n_gate_rejected": n_gate_rejected,
        "n_trades_opened_this_run": n_trades_opened,
        "candidates": scored,
    }
    V2_CANDIDATES_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"V2 engine: {len(scored)} candidates evaluated (from {len(coins)} total this run), "
          f"{n_high_priority} HIGH_PRIORITY_SETUP, {n_watch} WATCH, {n_gate_rejected} rejected by gate, "
          f"{n_trades_opened} new v2-shadow-trades.json entries opened.")
    for r in scored[:5]:
        print(f"  {r['symbol']:8s} score={r['v2_score']:3d} {r['v2_final_status']:20s} {r['v2_archetype']}")


if __name__ == "__main__":
    main()
