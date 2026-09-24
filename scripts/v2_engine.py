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

TOP_FUNNEL_TIERS = {"agent_room_priority", "shortlist_10"}

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


def main():
    radar = load_json(RADAR_FLAGS_PATH, {"coins": []})
    coins = radar.get("coins", [])
    market_regime = radar.get("market_regime")

    top_tier_coins = [c for c in coins if c.get("funnel_stage") in TOP_FUNNEL_TIERS and c.get("real_candles")]

    scored = [score_v2_candidate(c, market_regime) for c in top_tier_coins]
    scored.sort(key=lambda r: -r["v2_score"])

    n_high_priority = sum(1 for r in scored if r["v2_decision_state"] == "HIGH_PRIORITY_SETUP")
    n_watch = sum(1 for r in scored if r["v2_decision_state"] == "WATCH")

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market_regime": market_regime,
        "n_candidates_evaluated": len(scored),
        "n_high_priority": n_high_priority,
        "n_watch": n_watch,
        "candidates": scored,
    }
    V2_CANDIDATES_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"V2 engine: {len(scored)} candidates evaluated (from {len(coins)} total this run), "
          f"{n_high_priority} HIGH_PRIORITY_SETUP, {n_watch} WATCH.")
    for r in scored[:5]:
        print(f"  {r['symbol']:8s} score={r['v2_score']:3d} {r['v2_decision_state']:20s} {r['v2_archetype']}")


if __name__ == "__main__":
    main()
