"""
Investment Radar - Scalp/Momentum Signals (v1, part of v14)
------------------------------------------------------------
Context (2026-09-21 discussion): a "third track" for quick momentum entries
that would never survive the full Agent Room process SCALP requires - but
per Azez's own framing, the honest reason to build this FIRST is to
accumulate data at a faster rate, not to risk real capital on an unproven
edge. Nothing here is real money and nothing here bypasses SCALP's own
discipline - SCALP still requires the full 5-role Agent Room debate before
any real trade, unchanged.

Deliberately reuses breakout_check.py's OUTPUT (data/radar-flags.json)
instead of re-fetching or re-running select_rotating_candidates - that
data-fetching is the expensive, rate-limited part, and it already ran once
this cycle. This script adds NO new API calls; it just applies a second,
independent scoring lens to data that already exists.

What makes this track different from the main radar:
  - Prioritizes coins already flagged trend_following_eligible (v14 score
    streak - sustained strength, not a single snapshot) alongside fresh
    breakout_signal/trendline_break_confirmed_signal hits.
  - Stop is ALWAYS the ATR-based trend_following_stop when available (tight,
    volatility-sized), never the wider liquidity-buffered support/trendline
    stop the main radar uses - matching the "narrow timeframe, narrow risk"
    intent for this track.
  - Output goes to its own file (data/scalp-signals.json), never touches
    config/trades.json or data/shadow-trades.json, so it can never be
    confused with a SCALP-approved real trade or the main radar's paper
    trades.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
RADAR_FLAGS_PATH = BASE_DIR / "data" / "radar-flags.json"
SCALP_SIGNALS_PATH = BASE_DIR / "data" / "scalp-signals.json"

SCALP_MIN_SCORE = 30  # deliberately lower than AUTO_TRADE_MIN_SCORE (40) - this track's whole
                        # point is to log more candidates for learning, not to gate tightly


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


def build_scalp_signal(coin: dict) -> dict:
    entry = coin.get("price_usd")
    stop = coin.get("trend_following_stop")
    targets = coin.get("trend_following_targets")
    used_atr_stop = stop is not None

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

    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "asset_id": coin["id"],
        "symbol": coin["symbol"],
        "price_usd": entry,
        "stop": stop,
        "targets": targets,
        "used_trend_following_stop": used_atr_stop,
        "confidence_score": coin.get("confidence_score"),
        "score_streak": coin.get("score_streak"),
        "trend_following_eligible": coin.get("trend_following_eligible", False),
        "signal_quality": coin.get("signal_quality"),
        "relative_strength_pct": coin.get("relative_strength_pct"),
        "oi_price_relationship": coin.get("oi_price_relationship"),
    }


def main():
    radar_data = load_json(RADAR_FLAGS_PATH, {"coins": []})
    fired = get_fired_coins(radar_data)

    qualifying = [c for c in fired if (c.get("confidence_score") or 0) >= SCALP_MIN_SCORE]
    signals = [build_scalp_signal(c) for c in qualifying]
    signals.sort(key=lambda s: -(s.get("confidence_score") or 0))

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": (
            "Data-collection track only - nothing here is a real trade or a SCALP-approved "
            "signal. SCALP still requires the full Agent Room debate before any real capital "
            "moves. This exists to build a labeled dataset faster than the main radar's paper "
            "trades alone can."
        ),
        "signals": signals,
    }
    save_json(SCALP_SIGNALS_PATH, output)
    n_trend_following = sum(1 for s in signals if s["trend_following_eligible"])
    print(f"Scalp signals: {len(signals)} qualifying (score >= {SCALP_MIN_SCORE}), "
          f"{n_trend_following} using the trend-following ATR stop.")


if __name__ == "__main__":
    main()
