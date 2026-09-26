"""
Investment Radar - CoinGecko Market Scanner (v4)
--------------------------------------------------
v4 additions (professional-trader improvements, no paid data sources):
  - MARKET BREADTH: what % of the scanned universe is green right now.
    A single coin's breakout means something different in a 90%-green
    market (everything is up) vs a 15%-green market (this coin is a real
    outlier). Written as market_breadth_pct_green on every snapshot.
  - CATEGORY CLUSTERING WARNING: if 2+ flagged coins this run share a
    tracked category (from categories.json), that's one sector move, not
    N independent opportunities - same "don't count correlated signals as
    independent" discipline used everywhere else. Reported as
    category_clusters at the top level of radar-flags.json.

v3 changes (still present):
  - FIXED radar-flags.json's "count" to match the actual saved array length.
  - REMOVED the unreliable ATR(14) approximation.
  - ADDED BTC-relative excess return per coin (beta filter).
  - CONSOLIDATED reversal + sharp-move flags to avoid double-reporting the
    same 24h number under two labels.

Approximation notice: history points are 15-minute snapshots, not true
exchange candles. RSI/EMA computed from them are directional approximations
over a short window, not the same as chart-read RSI(14)/EMA(9,21).
"""
import json
import os
import time
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from statistics import mean

BASE_URL = "https://api.coingecko.com/api/v3/coins/markets"
CATEGORY_URL = "https://api.coingecko.com/api/v3/coins/markets"
VS_CURRENCY = "usd"
PER_PAGE = 250
PAGES = 1
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.json"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

HISTORY_PATH = DATA_DIR / "price-history.json"
INDICATORS_PATH = DATA_DIR / "indicators.json"
CATEGORIES_PATH = DATA_DIR / "categories.json"

MAX_HISTORY_POINTS = 500          # ~5 days at 15-min intervals
MIN_POINTS_FOR_INDICATORS = 14    # RSI(14) minimum
OPPORTUNITY_LIFECYCLE_PATH = DATA_DIR / "opportunity-lifecycle.json"
OPPORTUNITY_DECAY_MOVE_PCT = 6.0    # price already moved this much since first flag -> the move likely already happened
OPPORTUNITY_DECAY_HOURS = 48        # flagged this long without resolving -> stale regardless of price

CATEGORIES_TO_TRACK = [
    "privacy-coins",
    "decentralized-exchange",
    "liquid-staking-tokens",
    "layer-1",
    "meme-token",
    "real-world-assets-rwa",
    "artificial-intelligence",
    "yield-farming",
]

FLAG_24H_PCT = 8.0
FLAG_7D_PCT = 20.0
FLAG_REVERSAL_24H = 5.0
FLAG_REVERSAL_7D = 5.0
UNUSUAL_VOLUME_MULTIPLE = 2.5
EXCESS_VS_BTC_PCT = 10.0

# --- v15: Layer-2 cheap universal early-signal screening -------------------
# Rationale (from the 22/9/2026 committee audit + brainstorm): every signal
# in breakout_check.py is CONFIRMATORY - it needs the price to have already
# broken out or extended. This runs on every coin already accumulating
# price-history.json (not just this run's top-40 flagged list, which
# reshuffles every run) at ZERO extra API cost, purely from data already
# being collected. A hit here doesn't open any trade by itself - it only
# marks priority_review so breakout_check.py's rotation gives the coin an
# immediate deep-evaluation slot instead of waiting up to ~2-3 runs for its
# normal turn (the detection-latency gap the audit found via ZAMA).
SYNTHETIC_CANDLE_HOURS = 4      # bucket size for turning 15-min snapshots into swing-detectable bars
MIN_CANDLES_FOR_STRUCTURE = 8   # need at least this many synthetic candles before trusting a swing read
SWING_LOOKBACK = 2              # a candle is a swing point if it's the extreme of the 2 candles either side (fewer than breakout_check's TRENDLINE_SWING_LOOKBACK_CANDLES=5 since far fewer candles are available here)

SQUEEZE_LOOKBACK_CANDLES = 12   # older comparison window
SQUEEZE_COMPARE_CANDLES = 6     # recent window being checked for contraction
SQUEEZE_CONTRACTION_RATIO = 0.7  # recent avg range must be <= 70% of the older avg range to count as coiling

RS_CONSOLIDATION_LOOKBACK_HOURS = 48
RS_CONSOLIDATION_MAX_OWN_MOVE_PCT = 8.0     # "consolidating" - hasn't already made its own big move (that's what FLAG_24H_PCT/FLAG_7D_PCT already catch)
RS_CONSOLIDATION_MIN_OUTPERFORM_PCT = 6.0   # ...while still quietly beating BTC by at least this many points over the same window

CLUSTER_LAG_MIN_FLAGGED_PEERS = 6  # v52 fix (26/9/2026): was 2 - CoinGecko's category endpoint always returns
                                     # up to 50 members regardless of how many of them we actually track, so "2
                                     # movers in a 50-member category" is a trivially low bar during any broad
                                     # rally (measured firing 41.6-58% of tracked coins on a real 73.6%-breadth
                                     # day). 6 measured at 26.7% - still fires meaningfully during genuine sector
                                     # moves, without being satisfied by ordinary market-wide breadth alone.


def build_synthetic_candles(points: list, bucket_hours: int = SYNTHETIC_CANDLE_HOURS) -> list:
    """Aggregates 15-min price snapshots into synthetic OHLC-ish candles.
    IMPORTANT LIMITATION: only the 'price' field is bucketed into open/high/
    low/close. high_24h/low_24h/volume on each point are CoinGecko's
    ROLLING 24h aggregates, not deltas for that specific 15-min slice, so
    they cannot be safely turned into a candle's own high/low/volume - that
    would silently smear 24h-old extremes into a 4h bar. This makes these
    candles closer to "a line chart resampled into bars" than true OHLC -
    good enough for swing/structure detection (which only needs relative
    highs and lows of the PRICE series), not for anything that needs real
    intrabar range or volume."""
    buckets = {}
    for p in points:
        price = p.get("price")
        ts = p.get("t")
        if price is None or ts is None:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            continue
        bucket_hour = (dt.hour // bucket_hours) * bucket_hours
        bucket_key = dt.replace(hour=bucket_hour, minute=0, second=0, microsecond=0)
        buckets.setdefault(bucket_key, []).append(price)
    candles = []
    for bucket_key in sorted(buckets.keys()):
        prices = buckets[bucket_key]
        candles.append({
            "t": bucket_key.isoformat(),
            "open": prices[0],
            "high": max(prices),
            "low": min(prices),
            "close": prices[-1],
        })
    return candles


def find_swings(candles: list, lookback: int = SWING_LOOKBACK) -> list:
    """Fractal swing-point detection, same principle as breakout_check.py's
    descending-trendline swing highs, applied here to both highs and lows.
    Returns swings in chronological order as {"index", "t", "price", "type"}."""
    swings = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        window = candles[i - lookback:i + lookback + 1]
        if candles[i]["high"] == max(c["high"] for c in window):
            swings.append({"index": i, "t": candles[i]["t"], "price": candles[i]["high"], "type": "high"})
        if candles[i]["low"] == min(c["low"] for c in window):
            swings.append({"index": i, "t": candles[i]["t"], "price": candles[i]["low"], "type": "low"})
    return swings


def detect_structure_signal(candles: list):
    """CHoCH/BOS read (same definitions as the LuxAlgo SMC tool already in
    use): BOS = price breaks the last swing point IN the current structure's
    direction (continuation - not new information, breakout_check.py's own
    logic already catches this once it's underway). CHoCH = price breaks
    the last swing point AGAINST the current structure's direction for the
    first time - this is the early one, the whole reason to run this before
    any confirmed breakout. Returns None (not a guess) with too little
    history, matching the insufficient-history discipline used elsewhere."""
    if len(candles) < MIN_CANDLES_FOR_STRUCTURE:
        return None
    swings = find_swings(candles)
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return None
    last_high, prev_high = highs[-1], highs[-2]
    last_low, prev_low = lows[-1], lows[-2]
    if last_high["price"] > prev_high["price"] and last_low["price"] > prev_low["price"]:
        structure = "uptrend"
    elif last_high["price"] < prev_high["price"] and last_low["price"] < prev_low["price"]:
        structure = "downtrend"
    else:
        structure = "unclear"
    latest_close = candles[-1]["close"]
    signal = None
    if structure == "downtrend" and latest_close > last_high["price"]:
        signal = "CHoCH_bullish"    # the early-reversal case this whole layer exists to catch
    elif structure == "uptrend" and latest_close < last_low["price"]:
        signal = "CHoCH_bearish"
    elif structure == "uptrend" and latest_close > last_high["price"]:
        signal = "BOS_bullish"
    elif structure == "downtrend" and latest_close < last_low["price"]:
        signal = "BOS_bearish"
    return {"structure": structure, "signal": signal,
            "last_swing_high": last_high["price"], "last_swing_low": last_low["price"]}


def detect_volatility_squeeze(candles: list) -> bool:
    """True-range-as-%-of-close, comparing the recent window against the
    window before it. A contracting range ahead of a move is the classic
    pre-breakout "coiling" tell (Bollinger squeeze / VCP logic) - catching
    it BEFORE the expansion, not after, is the point of this layer."""
    need = SQUEEZE_LOOKBACK_CANDLES + SQUEEZE_COMPARE_CANDLES
    if len(candles) < need:
        return False

    def avg_range_pct(subset):
        ranges = [(c["high"] - c["low"]) / c["close"] for c in subset if c.get("close")]
        return mean(ranges) if ranges else None

    older = candles[-need:-SQUEEZE_COMPARE_CANDLES]
    recent = candles[-SQUEEZE_COMPARE_CANDLES:]
    older_range = avg_range_pct(older)
    recent_range = avg_range_pct(recent)
    if not older_range or recent_range is None:
        return False
    return recent_range <= older_range * SQUEEZE_CONTRACTION_RATIO


def compute_rsi_series(closes: list, period: int = 14) -> list:
    """Rolling RSI(period) at every candle, aligned index-for-index with
    `closes` (leading entries are None until enough history exists). The
    existing compute_rsi() only ever returns the single latest value, which
    isn't enough to compare RSI at two different swing lows for divergence."""
    n = len(closes)
    series = [None] * n
    if n < period + 1:
        return series
    gains, losses = [], []
    for i in range(1, n):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    for i in range(period, n):
        g = gains[i - period:i]
        l = losses[i - period:i]
        avg_gain = mean(g)
        avg_loss = mean(l)
        series[i] = 100.0 if avg_loss == 0 else round(100 - (100 / (1 + avg_gain / avg_loss)), 2)
    return series


def detect_bullish_rsi_divergence(candles: list) -> bool:
    """Price makes a lower low while RSI makes a higher low: downside
    momentum is fading before the price itself turns - earlier than CHoCH,
    which needs the structure to actually break first."""
    if len(candles) < MIN_CANDLES_FOR_STRUCTURE:
        return False
    closes = [c["close"] for c in candles]
    rsi_series = compute_rsi_series(closes)
    swings = find_swings(candles)
    lows = [s for s in swings if s["type"] == "low" and rsi_series[s["index"]] is not None]
    if len(lows) < 2:
        return False
    prev_low, last_low = lows[-2], lows[-1]
    price_lower_low = last_low["price"] < prev_low["price"]
    rsi_higher_low = rsi_series[last_low["index"]] > rsi_series[prev_low["index"]]
    return price_lower_low and rsi_higher_low


def detect_cluster_rotation_lag(coin_id: str, categories: dict, base_flagged_ids: set):
    """If 2+ OTHER coins in the same tracked category already show a real
    (price/volume-based) flag THIS run but THIS coin doesn't, sector
    rotation lag makes it a reasonable early candidate for the next leg -
    sectors tend to move together with a delay, not simultaneously. Checked
    against base_flagged_ids only (not other coins' early signals), so this
    can't chain off another coin's own unconfirmed squeeze/CHoCH flag.

    v15.1 fix (found live on 22/9/2026): the coin ITSELF must not already
    have a base flag. Without this, a genuine sector-wide rally (e.g. the
    whole decentralized-exchange category moving together) labeled EVERY
    member "still quiet, lagging" - including coins like UNI/JUP/PENDLE
    that were themselves already showing a strong price-move flag, which is
    a factual contradiction (a coin that's already moving is not a lagging
    coin). Only a coin with NO base flag of its own can be a laggard.

    Returns the category name, or None."""
    if coin_id in base_flagged_ids:
        return None
    for category, ids in categories.items():
        if not isinstance(ids, list) or coin_id not in ids:
            continue
        peers_flagged = sum(1 for cid in ids if cid != coin_id and cid in base_flagged_ids)
        if peers_flagged >= CLUSTER_LAG_MIN_FLAGGED_PEERS:
            return category
    return None


def _pct_change_over_window(points: list, lookback_hours: float):
    if len(points) < 4:
        return None
    try:
        now = datetime.fromisoformat(points[-1]["t"])
    except (KeyError, ValueError):
        return None
    target = now - timedelta(hours=lookback_hours)
    past_point = min(
        points,
        key=lambda p: abs(datetime.fromisoformat(p["t"]) - target) if p.get("t") else timedelta.max,
    )
    past_price = past_point.get("price")
    latest_price = points[-1].get("price")
    if not past_price or latest_price is None:
        return None
    return (latest_price - past_price) / past_price * 100


def detect_relative_strength_consolidation(coin_points: list, btc_points: list) -> bool:
    """A coin that's roughly flat on its own (genuinely consolidating - not
    already mooning, which FLAG_24H_PCT/FLAG_7D_PCT already catch) while
    quietly beating BTC over the same window is showing hidden strength
    BEFORE any breakout - the "quiet accumulation" tell."""
    own_chg = _pct_change_over_window(coin_points, RS_CONSOLIDATION_LOOKBACK_HOURS)
    btc_chg = _pct_change_over_window(btc_points, RS_CONSOLIDATION_LOOKBACK_HOURS)
    if own_chg is None or btc_chg is None:
        return False
    return (abs(own_chg) <= RS_CONSOLIDATION_MAX_OWN_MOVE_PCT
            and (own_chg - btc_chg) >= RS_CONSOLIDATION_MIN_OUTPERFORM_PCT)


def compute_early_signals(coin_id: str, points: list, categories: dict,
                           base_flagged_ids: set, btc_points: list) -> dict:
    candles = build_synthetic_candles(points)
    return {
        "structure": detect_structure_signal(candles),
        "volatility_squeeze": detect_volatility_squeeze(candles),
        "bullish_rsi_divergence": detect_bullish_rsi_divergence(candles),
        "cluster_rotation_lag": detect_cluster_rotation_lag(coin_id, categories, base_flagged_ids),
        "relative_strength_consolidation": (
            detect_relative_strength_consolidation(points, btc_points) if btc_points else False
        ),
        "flag_pattern": detect_flag_pattern(candles),
        "double_bottom": detect_double_bottom(candles),
        "triangle": detect_triangle(candles),
    }


def early_signal_flags(signals: dict) -> list:
    """Translates raw early-signal booleans into the same Arabic flags/
    array style used by compute_flags() below, so they display identically
    and count identically toward a coin's flag total. NOTE: these do NOT
    feed compute_confidence_score() in breakout_check.py - per the 22/9
    design decision, an early signal only earns a coin a priority_review
    escalation to deep evaluation, never a scoring weight, until enough
    outcomes are logged in agent-room-log.json to justify one."""
    flags = []
    structure = signals.get("structure") or {}
    if structure.get("signal") == "CHoCH_bullish":
        flags.append("CHoCH صاعد مبكر — تغيّر هيكلي محتمل قبل أي اختراق مؤكد")
    if signals.get("volatility_squeeze"):
        flags.append("انضغاط تقلب (Squeeze) — مدى الحركة بيضيق قبل احتمال انفجار")
    if signals.get("bullish_rsi_divergence"):
        flags.append("تباعد RSI صاعد — زخم الهبوط بيضعف قبل انعكاس السعر")
    if signals.get("cluster_rotation_lag"):
        flags.append(f"تأخر دوران قطاعي — عملات تانية في {signals['cluster_rotation_lag']} تحركت وهي لسه ساكنة")
    if signals.get("relative_strength_consolidation"):
        flags.append("قوة نسبية خفية أثناء التماسك — بتتفوق على BTC وهي ساكنة ظاهريًا")
    flag_pattern = signals.get("flag_pattern")
    if flag_pattern and flag_pattern.get("direction") == "bullish":
        flags.append(f"نمط علم صاعد (Bull Flag) — هدف Kirkpatrick بعد الاختراق: {flag_pattern['target_price']}")
    double_bottom = signals.get("double_bottom")
    if double_bottom:
        stage = "مؤكد بعد كسر خط الرقبة" if double_bottom.get("confirmed") else "قيد التكوّن (لسه محدش كسر خط الرقبة)"
        flags.append(f"قاع مزدوج {stage} — خط الرقبة {double_bottom['neckline']}، الهدف {double_bottom['target_price']}")
    triangle = signals.get("triangle")
    if triangle:
        name = "مثلث صاعد" if triangle["pattern"] == "ascending_triangle" else "مثلث متماثل"
        flags.append(f"{name} — مقاومة {triangle['resistance_level']}، الهدف بعد الاختراق {triangle['target_price']}")
    return flags


# --- v25: classic chart patterns (Fidelity/Kirkpatrick toolkit) ------------
# Rationale: these three were picked from the full toolkit as the ones that
# translate to deterministic geometry rather than fuzzy visual judgment -
# Head & Shoulders, Cup & Handle etc. stay a manual/chat-only read for now
# (see the 22/9/2026 discussion). All three reuse find_swings/
# build_synthetic_candles already built for CHoCH/BOS detection - no new
# API calls, no new data collection.
FLAGPOLE_LOOKBACK_CANDLES = 6       # window searched for the sharp initial move (the "pole")
FLAGPOLE_MIN_MOVE_PCT = 15.0        # minimum % move within that window to count as a flagpole at all
FLAG_CONSOLIDATION_CANDLES = 6      # candles since the pole that must show a tight consolidation (the "flag")
FLAG_MAX_CONSOLIDATION_RANGE_PCT = 8.0   # consolidation must stay within this % range of its own midpoint

DOUBLE_BOTTOM_LEVEL_TOLERANCE_PCT = 1.2   # v36 fix (24/9/2026): was 3.0 - measured 75/264 coins (28%) firing on real
                                            # production data, the single cause of runs ballooning past an hour (every
                                            # hit forces an expensive real-API deep-eval in breakout_check.py). At this
                                            # synthetic-candle noise level, 3% was catching coincidental near-lows as
                                            # "the same level" far too often. 1.2% measured at 15/264 (6%), in line
                                            # with the other early-signal detectors' selectivity.
DOUBLE_BOTTOM_MIN_SEPARATION = 5          # v36: was 3 - too short a window between the two lows made noise look structural
DOUBLE_BOTTOM_MIN_NECKLINE_GAP_PCT = 2.0  # v36 new: neckline must clear the lows by a meaningful margin, not just >0

TRIANGLE_MIN_SWINGS_EACH_SIDE = 3   # v50 fix (26/9/2026): was 2 - measured 36.8% of 266 real coins firing
                                      # (the exact same over-firing pattern double_bottom had before v36) since
                                      # any 2 alternating swings satisfy "descending highs + ascending lows" by
                                      # ordinary noise alone. 3 swings each side measured at 3.8%, in line with
                                      # the other early-signal detectors' selectivity.
TRIANGLE_RESISTANCE_FLAT_TOLERANCE_PCT = 2.0   # highs within this % of each other count as "flat" (ascending triangle)


def detect_flag_pattern(candles: list):
    """Bull flag (per Kirkpatrick): a sharp impulsive move (the flagpole)
    followed by a tight, shallow consolidation sloping slightly against the
    trend. Target = flagpole height projected from the current price -
    breakout tends to repeat the pole's magnitude. Returns None if there's
    no sharp-enough recent move, or if one exists but the consolidation
    since isn't tight enough yet to call it a flag (still just a raw
    extension, which extension_continuation_signal elsewhere already
    covers)."""
    need = FLAGPOLE_LOOKBACK_CANDLES + FLAG_CONSOLIDATION_CANDLES
    if len(candles) < need:
        return None
    consolidation = candles[-FLAG_CONSOLIDATION_CANDLES:]
    pole = candles[-need:-FLAG_CONSOLIDATION_CANDLES]
    if not pole:
        return None
    pole_start, pole_end = pole[0]["open"], pole[-1]["close"]
    if not pole_start:
        return None
    pole_move_pct = (pole_end - pole_start) / pole_start * 100
    if abs(pole_move_pct) < FLAGPOLE_MIN_MOVE_PCT:
        return None
    cons_high = max(c["high"] for c in consolidation)
    cons_low = min(c["low"] for c in consolidation)
    cons_mid = (cons_high + cons_low) / 2
    if not cons_mid:
        return None
    cons_range_pct = (cons_high - cons_low) / cons_mid * 100
    if cons_range_pct > FLAG_MAX_CONSOLIDATION_RANGE_PCT:
        return None
    direction = "bullish" if pole_move_pct > 0 else "bearish"
    flagpole_height = abs(pole_end - pole_start)
    latest_close = candles[-1]["close"]
    target = latest_close + flagpole_height if direction == "bullish" else latest_close - flagpole_height
    return {
        "direction": direction,
        "flagpole_move_pct": round(pole_move_pct, 2),
        "consolidation_range_pct": round(cons_range_pct, 2),
        "target_price": round(target, 8),
    }


def detect_double_bottom(candles: list):
    """Developing (not yet necessarily confirmed) double bottom: two swing
    lows at roughly the same level, separated by a swing high (the
    neckline). Per Kirkpatrick the pattern only COMPLETES on a confirmed
    close above the neckline - reporting the SETUP forming, before that
    break, is exactly the point of an early/proactive signal here.
    confirmed=True once the latest close has already cleared the neckline.
    Target (once confirmed) = neckline + (neckline - lower low), the
    standard height-projection formula."""
    swings = find_swings(candles)
    lows = [s for s in swings if s["type"] == "low"]
    highs = [s for s in swings if s["type"] == "high"]
    if len(lows) < 2:
        return None
    prev_low, last_low = lows[-2], lows[-1]
    if last_low["index"] - prev_low["index"] < DOUBLE_BOTTOM_MIN_SEPARATION:
        return None
    lower_low = min(last_low["price"], prev_low["price"])
    if not lower_low:
        return None
    level_diff_pct = abs(last_low["price"] - prev_low["price"]) / lower_low * 100
    if level_diff_pct > DOUBLE_BOTTOM_LEVEL_TOLERANCE_PCT:
        return None
    between_highs = [h for h in highs if prev_low["index"] < h["index"] < last_low["index"]]
    if not between_highs:
        return None
    neckline = max(h["price"] for h in between_highs)
    if neckline <= lower_low:
        return None
    if (neckline - lower_low) / lower_low * 100 < DOUBLE_BOTTOM_MIN_NECKLINE_GAP_PCT:
        return None  # v36: neckline barely above the lows isn't a meaningful reversal structure
    target = neckline + (neckline - lower_low)
    return {
        "lower_low": lower_low,
        "neckline": round(neckline, 8),
        "target_price": round(target, 8),
        "confirmed": candles[-1]["close"] > neckline,
    }


def detect_triangle(candles: list):
    """Ascending triangle: flat horizontal resistance (highs within
    TRIANGLE_RESISTANCE_FLAT_TOLERANCE_PCT of each other) + rising support
    (strictly increasing swing lows). Symmetrical triangle: descending
    resistance + rising support (both converging). Target = pattern
    height (resistance - lowest low) projected from the resistance level,
    same formula for both per Kirkpatrick. Needs at least
    TRIANGLE_MIN_SWINGS_EACH_SIDE swing highs and lows to even attempt a
    read - returns None otherwise rather than guessing from too little
    structure."""
    swings = find_swings(candles)
    highs = [s for s in swings if s["type"] == "high"]
    lows = [s for s in swings if s["type"] == "low"]
    if len(highs) < TRIANGLE_MIN_SWINGS_EACH_SIDE or len(lows) < TRIANGLE_MIN_SWINGS_EACH_SIDE:
        return None
    recent_highs = [h["price"] for h in highs[-TRIANGLE_MIN_SWINGS_EACH_SIDE:]]
    recent_lows = [l["price"] for l in lows[-TRIANGLE_MIN_SWINGS_EACH_SIDE:]]
    if min(recent_highs) <= 0:
        return None

    highs_flat = (max(recent_highs) - min(recent_highs)) / min(recent_highs) * 100 <= TRIANGLE_RESISTANCE_FLAT_TOLERANCE_PCT
    highs_descending = all(recent_highs[i] > recent_highs[i + 1] for i in range(len(recent_highs) - 1))
    lows_ascending = all(recent_lows[i] < recent_lows[i + 1] for i in range(len(recent_lows) - 1))

    if highs_flat and lows_ascending:
        pattern_type = "ascending_triangle"
    elif highs_descending and lows_ascending:
        pattern_type = "symmetrical_triangle"
    else:
        return None

    resistance_level = max(recent_highs)
    lowest_low = min(recent_lows)
    height = resistance_level - lowest_low
    if height <= 0:
        return None
    return {
        "pattern": pattern_type,
        "resistance_level": round(resistance_level, 8),
        "target_price": round(resistance_level + height, 8),
    }


# v39 fix (24/9/2026): keyless CoinGecko requests are rate-limited PER SHARED IP
# (CoinGecko's own docs: "shared across all users on the same IP") - GitHub
# Actions runners share IP ranges with countless unrelated projects, so our
# calls were being throttled by OTHER users' traffic, not our own pacing.
# A free Demo API key is billed against the ACCOUNT instead, sidestepping
# this entirely. Optional by design (falls back to keyless if unset) so
# nothing breaks before the secret is configured in the repo.
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY")


def fetch_json(url: str, params: dict) -> list:
    full_url = url + "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": "investment-radar/4.0"}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
    req = urllib.request.Request(full_url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_top(per_page=PER_PAGE, pages=PAGES) -> list:
    out = []
    for page in range(1, pages + 1):
        params = {
            "vs_currency": VS_CURRENCY,
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
            "price_change_percentage": "24h,7d",
        }
        out.extend(fetch_json(BASE_URL, params))
        time.sleep(1.5)
    return out


def fetch_watchlist(ids: list) -> list:
    if not ids:
        return []
    params = {
        "vs_currency": VS_CURRENCY,
        "ids": ",".join(ids),
        "price_change_percentage": "24h,7d",
    }
    return fetch_json(BASE_URL, params)


def merge_unique(*lists) -> list:
    seen = {}
    for lst in lists:
        for coin in lst:
            seen[coin["id"]] = coin
    return list(seen.values())


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def update_opportunity_lifecycle(lifecycle: dict, coin_id: str, is_flagged_now: bool,
                                   price_now, now_iso: str) -> dict | None:
    """v33 (24/9/2026): Opportunity Decay - step 5 of the V2-merge plan. The
    radar runs every 30 minutes; without this, a coin flagged at 10:00 that
    already moved +7% by 11:00 would still read as a fresh "high priority"
    early signal at 11:00, when the anticipated move has likely already
    happened. Tracks, per coin, the price and time at first flag, and
    reports how much of that move has already played out. Resets (returns
    None, entry removed) once a coin drops out of "currently flagged" for a
    run, so the NEXT time it gets flagged starts a genuinely fresh clock
    rather than inheriting a stale one."""
    if not is_flagged_now:
        return None  # not flagged this run - clear any prior tracking, ready for a fresh future flag

    entry = lifecycle.get(coin_id)
    if entry is None:
        return {"first_flagged_at": now_iso, "price_at_first_flag": price_now,
                "move_since_flag_pct": 0.0, "hours_since_flag": 0.0, "decay_state": "fresh"}

    price_at_flag = entry.get("price_at_first_flag")
    move_pct = round((price_now - price_at_flag) / price_at_flag * 100, 2) if price_at_flag and price_now else None
    try:
        hours = round((datetime.fromisoformat(now_iso) - datetime.fromisoformat(entry["first_flagged_at"])).total_seconds() / 3600, 1)
    except (KeyError, ValueError):
        hours = None

    if move_pct is None or hours is None:
        decay_state = "unknown"
    elif abs(move_pct) >= OPPORTUNITY_DECAY_MOVE_PCT or hours >= OPPORTUNITY_DECAY_HOURS:
        decay_state = "decayed"
    elif abs(move_pct) >= OPPORTUNITY_DECAY_MOVE_PCT / 2 or hours >= OPPORTUNITY_DECAY_HOURS / 2:
        decay_state = "developing"
    else:
        decay_state = "fresh"

    return {
        "first_flagged_at": entry["first_flagged_at"],
        "price_at_first_flag": price_at_flag,
        "move_since_flag_pct": move_pct,
        "hours_since_flag": hours,
        "decay_state": decay_state,
    }


def update_history(history: dict, coin: dict, timestamp: str, always_track: set) -> None:
    cid = coin["id"]
    should_track = (
        cid in always_track
        or cid in history
        or coin.get("_will_flag", False)
    )
    if not should_track:
        return
    points = history.setdefault(cid, [])
    points.append({
        "t": timestamp,
        "price": coin.get("current_price"),
        "high_24h": coin.get("high_24h"),
        "low_24h": coin.get("low_24h"),
        "volume": coin.get("total_volume"),
    })
    if len(points) > MAX_HISTORY_POINTS:
        del points[: len(points) - MAX_HISTORY_POINTS]


def compute_rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_ema(prices: list, period: int) -> float:
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 8)


def compute_volume_baseline(points: list) -> float:
    vols = [p["volume"] for p in points if p.get("volume") is not None]
    if len(vols) < 4:
        return None
    return sum(vols) / len(vols)


def build_indicators(history: dict) -> dict:
    result = {}
    for cid, points in history.items():
        if len(points) < MIN_POINTS_FOR_INDICATORS:
            continue
        prices = [p["price"] for p in points if p.get("price") is not None]
        result[cid] = {
            "rsi14": compute_rsi(prices, 14),
            "ema9": compute_ema(prices, 9),
            "ema21": compute_ema(prices, 21),
            "volume_baseline_avg": compute_volume_baseline(points),
            "n_points": len(points),
            "note": "approximated from 15-min snapshots (~3.5h window for RSI14), not true candles",
        }
    return result


def categories_stale(existing: dict, max_age_hours: float = 20.0) -> bool:
    updated_at = existing.get("updated_at")
    if not updated_at:
        return True
    try:
        last = datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    age_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return age_hours >= max_age_hours


def fetch_categories() -> dict:
    mapping = {}
    for category in CATEGORIES_TO_TRACK:
        params = {
            "vs_currency": VS_CURRENCY,
            "category": category,
            "order": "market_cap_desc",
            "per_page": 50,
            "page": 1,
        }
        try:
            coins = fetch_json(CATEGORY_URL, params)
            mapping[category] = [c["id"] for c in coins]
        except Exception as exc:  # noqa: BLE001
            mapping[category] = {"error": str(exc)}
        time.sleep(1.5)
    return mapping


def compute_flags(coin: dict, volume_baseline: float, btc_chg24: float, btc_chg7d: float) -> list:
    flags = []
    chg24 = coin.get("price_change_percentage_24h_in_currency")
    chg7d = coin.get("price_change_percentage_7d_in_currency")
    vol = coin.get("total_volume") or 0

    reversal_added = False
    if chg24 is not None and chg7d is not None:
        if chg24 >= FLAG_REVERSAL_24H and chg7d <= -FLAG_REVERSAL_7D:
            flags.append(f"انعكاس صاعد محتمل ({chg24:.1f}% خلال 24 ساعة بعد أسبوع {chg7d:.1f}%)")
            reversal_added = True
        elif chg24 <= -FLAG_REVERSAL_24H and chg7d >= FLAG_REVERSAL_7D:
            flags.append(f"انعكاس هابط محتمل ({chg24:.1f}% خلال 24 ساعة بعد أسبوع {chg7d:.1f}%)")
            reversal_added = True

    if not reversal_added and chg24 is not None and abs(chg24) >= FLAG_24H_PCT:
        flags.append(f"حركة سعرية حادة خلال 24 ساعة ({chg24:.1f}%)")

    if chg7d is not None and abs(chg7d) >= FLAG_7D_PCT:
        flags.append(f"حركة سعرية حادة خلال 7 أيام ({chg7d:.1f}%)")

    if volume_baseline and volume_baseline > 0 and vol / volume_baseline >= UNUSUAL_VOLUME_MULTIPLE:
        flags.append(
            f"نشاط تداول غير عادي مقارنة بمتوسط العملة نفسها (x{vol / volume_baseline:.1f})"
        )
    elif volume_baseline is None:
        mcap = coin.get("market_cap") or 0
        if mcap and vol / mcap >= 0.5:
            flags.append(f"نشاط تداول غير عادي نسبة لحجم السوق (Vol/MCap={vol / mcap:.2f}) [بدون خط أساس بعد]")

    if chg24 is not None and btc_chg24 is not None:
        excess = chg24 - btc_chg24
        if abs(excess) >= EXCESS_VS_BTC_PCT and excess > 0:
            flags.append(
                f"تفوق واضح على BTC خلال 24 ساعة ({chg24:.1f}% مقابل BTC {btc_chg24:.1f}%) — "
                f"حركة مش مجرد بيتا عام للسوق"
            )

    return flags


def build_record(coin: dict, flags: list, indicators: dict, btc_chg24: float) -> dict:
    chg24 = coin.get("price_change_percentage_24h_in_currency")
    record = {
        "id": coin["id"],
        "symbol": coin["symbol"].upper(),
        "name": coin["name"],
        "price_usd": coin.get("current_price"),
        "high_24h_usd": coin.get("high_24h"),
        "low_24h_usd": coin.get("low_24h"),
        "change_24h_pct": chg24,
        "change_7d_pct": coin.get("price_change_percentage_7d_in_currency"),
        "volume_24h_usd": coin.get("total_volume"),
        "market_cap_usd": coin.get("market_cap"),
        "market_cap_rank": coin.get("market_cap_rank"),
        "flags": flags,
    }
    if chg24 is not None and btc_chg24 is not None:
        record["excess_return_24h_vs_btc_pct"] = round(chg24 - btc_chg24, 2)
    ind = indicators.get(coin["id"])
    if ind:
        record["indicators"] = ind

    # v29 (24/9/2026): Data Tiering baseline - every coin gets at least this
    # much, even the ~250-40 that never reach breakout_check.py's deep-eval
    # rotation this run. breakout_check.py OVERWRITES this with the fuller
    # version (adding atr/resistance_trendline/derivatives tiers) for
    # whichever subset it actually processes each run.
    record["data_quality"] = {
        "price": {"tier": 1, "source": "coingecko_simple_price", "confidence": "real"},
        "rsi14": {"tier": 0, "source": "scan_synthetic_15m_samples", "confidence": "approximate"},
        "structure_layer2": {"tier": 0, "source": "scan_synthetic_15m_samples", "confidence": "approximate"},
    }
    return record


def compute_category_clusters(flagged_coins: list, categories: dict) -> dict:
    """For each tracked category, list which flagged coins (by symbol) belong
    to it. Only categories with 2+ flagged members are returned - a single
    flagged coin in a category isn't a cluster."""
    coin_id_to_symbol = {c["id"]: c["symbol"] for c in flagged_coins}
    clusters = {}
    for category, ids in categories.items():
        if not isinstance(ids, list):
            continue  # skip categories that errored during fetch
        members = [coin_id_to_symbol[cid] for cid in ids if cid in coin_id_to_symbol]
        if len(members) >= 2:
            clusters[category] = members
    return clusters


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    watchlist_ids = []
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        watchlist_ids = cfg.get("always_include", [])

    top_coins = fetch_top()
    watchlist_coins = fetch_watchlist(watchlist_ids)
    all_coins = merge_unique(top_coins, watchlist_coins)

    btc = next((c for c in all_coins if c["id"] == "bitcoin"), None)
    btc_chg24 = btc.get("price_change_percentage_24h_in_currency") if btc else None
    btc_chg7d = btc.get("price_change_percentage_7d_in_currency") if btc else None

    for coin in all_coins:
        prelim_flags = compute_flags(coin, volume_baseline=None, btc_chg24=btc_chg24, btc_chg7d=btc_chg7d)
        coin["_will_flag"] = bool(prelim_flags)

    history = load_json(HISTORY_PATH, {})
    timestamp = datetime.now(timezone.utc).isoformat()
    # v15: bitcoin must always accumulate history regardless of whether it
    # ever gets flagged itself - it's the benchmark every early-signal
    # relative-strength check below is computed against.
    # v18 (22/9/2026): previously only watchlist coins + bitcoin + coins that
    # had ALREADY been flagged at least once got their price history tracked -
    # meaning a coin had to make a noticeable move before Layer 2's early
    # signals (quiet-consolidation relative strength, squeeze, CHoCH) could
    # ever see it, which defeats the point of catching it BEFORE it moves.
    # Tracking the full scanned universe costs zero extra API calls (the
    # scan already fetches all of them every run) - just one more point
    # appended per coin in price-history.json.
    always_track = {c["id"] for c in all_coins} | {"bitcoin"}
    for coin in all_coins:
        update_history(history, coin, timestamp, always_track)
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")

    indicators = build_indicators(history)
    INDICATORS_PATH.write_text(json.dumps(indicators, ensure_ascii=False, indent=2), encoding="utf-8")

    existing_categories = load_json(CATEGORIES_PATH, {})
    if categories_stale(existing_categories):
        cat_mapping = fetch_categories()
        categories_out = {"updated_at": timestamp, "categories": cat_mapping}
        CATEGORIES_PATH.write_text(json.dumps(categories_out, ensure_ascii=False, indent=2), encoding="utf-8")
        current_categories = cat_mapping
    else:
        current_categories = existing_categories.get("categories", {})

    # v15: pass 1 - existing price/volume-based flags for every coin, so
    # cluster_rotation_lag below can check which PEERS had a REAL flag this
    # run before any coin's own early signals are computed (a squeeze flag
    # on coin A must never count as coin B's "peer moved" evidence).
    base_flags_by_id = {}
    for coin in all_coins:
        points = history.get(coin["id"], [])
        baseline = compute_volume_baseline(points) if len(points) >= 4 else None
        base_flags_by_id[coin["id"]] = compute_flags(coin, baseline, btc_chg24, btc_chg7d)
    base_flagged_ids = {cid for cid, flags in base_flags_by_id.items() if flags}

    # v15: pass 2 - Layer-2 cheap universal early-signal screening, on every
    # coin with enough accumulated history (not just this run's eventual
    # top-40), at zero extra API cost.
    btc_points = history.get("bitcoin", [])
    opportunity_lifecycle = load_json(OPPORTUNITY_LIFECYCLE_PATH, {})
    now_iso = timestamp
    records = []
    for coin in all_coins:
        cid = coin["id"]
        points = history.get(cid, [])
        flags = list(base_flags_by_id[cid])
        priority_review = False
        early_signals = None
        if len(points) >= 4:
            early_signals = compute_early_signals(cid, points, current_categories, base_flagged_ids, btc_points)
            new_flags = early_signal_flags(early_signals)
            if new_flags:
                flags = flags + new_flags
                priority_review = True
        record = build_record(coin, flags, indicators, btc_chg24)
        if early_signals is not None:
            record["early_signals"] = early_signals
        record["priority_review"] = priority_review

        # v33: Opportunity Decay - "currently flagged" means either an early
        # signal fired OR the coin already had its own real price-based flag
        # this run (base_flagged_ids) - either way, it's something the
        # system called out, and its urgency should visibly age.
        is_flagged_now = priority_review or cid in base_flagged_ids
        lifecycle_entry = update_opportunity_lifecycle(
            opportunity_lifecycle, cid, is_flagged_now, coin.get("current_price"), now_iso
        )
        if lifecycle_entry is not None:
            opportunity_lifecycle[cid] = lifecycle_entry
            record["opportunity_lifecycle"] = lifecycle_entry
        else:
            opportunity_lifecycle.pop(cid, None)

        records.append(record)

    OPPORTUNITY_LIFECYCLE_PATH.write_text(json.dumps(opportunity_lifecycle, ensure_ascii=False), encoding="utf-8")

    # market breadth - what fraction of the scanned universe is green right now
    with_change = [r for r in records if r.get("change_24h_pct") is not None]
    green = [r for r in with_change if r["change_24h_pct"] > 0]
    market_breadth_pct_green = round(len(green) / len(with_change) * 100, 1) if with_change else None

    full_snapshot = {
        "updated_at": timestamp,
        "count": len(records),
        "market_breadth_pct_green": market_breadth_pct_green,
        "coins": records,
    }
    (DATA_DIR / "market-scan.json").write_text(
        json.dumps(full_snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    flagged = [r for r in records if r["flags"]]
    flagged.sort(key=lambda r: len(r["flags"]), reverse=True)
    top_by_flag_count = flagged[:40]

    # v15: a coin escalated by Layer 2 (priority_review) must reach
    # breakout_check.py THIS run even if it ranks below the top 40 by raw
    # flag count (a lone squeeze/CHoCH flag can rank under five coins each
    # showing four ordinary price/volume flags) - otherwise the whole point
    # of catching it early is lost to it simply not making the cut.
    already_included_ids = {r["id"] for r in top_by_flag_count}
    priority_extras = [r for r in flagged
                        if r.get("priority_review") and r["id"] not in already_included_ids]
    top_flagged = top_by_flag_count + priority_extras

    category_clusters = compute_category_clusters(top_flagged, current_categories)

    radar_flags = {
        "updated_at": timestamp,
        "count": len(top_flagged),
        "market_breadth_pct_green": market_breadth_pct_green,
        "category_clusters": category_clusters,
        "coins": top_flagged,
    }
    (DATA_DIR / "radar-flags.json").write_text(
        json.dumps(radar_flags, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    n_priority = sum(1 for r in records if r.get("priority_review"))
    print(f"Scanned {len(records)} coins, {len(flagged)} flagged (saved {len(top_flagged)}: "
          f"top {len(top_by_flag_count)} by flag count + {len(priority_extras)} priority-escalated), "
          f"breadth={market_breadth_pct_green}% green, {len(category_clusters)} category clusters, "
          f"{n_priority} coins with a Layer-2 early signal this run, "
          f"{len(indicators)} with computed indicators, history for {len(history)} coins.")


if __name__ == "__main__":
    main()
