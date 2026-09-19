"""
Investment Radar - Resistance Breakout Check (v4)
------------------------------------------------------------
Runs after check_liquidity.py, only against coins already confirmed
binance_listed == True in data/radar-flags.json.

v4 additions (professional-trader improvements, no paid data sources):
  - FIBONACCI EXTENSION CONTINUATION SIGNAL: for a coin that already broke
    its resistance a while ago (like INJ - up 25%+ but breakout_confirmed
    is correctly False because the break itself is old news), this computes
    a Fibonacci extension (1.272x, 1.618x) from the swing low that led into
    the resistance zone, and checks whether price has pushed through and
    HELD above the 1.272 extension with volume support. This is a second,
    independent signal type (extension_continuation_signal) for catching
    an already-broken coin's next leg, separate from the fresh-break signal.
    Anchor choice: swing low = lowest low in the same lookback window used
    to find the resistance zone (a defensible, if simple, choice - not a
    substitute for judgment on which leg actually matters).
  - TREND FILTER: computes an EMA50 on the same 4h OHLC series already
    fetched (an approximation of "the medium-term trend", not a true daily
    EMA50 - documented honestly) and requires price to be above it for
    either signal type to count. A breakout against the medium-term trend
    is statistically weaker than one with it.
  - INSUFFICIENT-HISTORY FLAG: if the OHLC series returned far fewer candles
    than the requested window implies, the coin likely doesn't have enough
    trading history for a reliable resistance read (e.g. a very recent
    listing) - flagged and excluded from both signal types rather than
    given a falsely confident answer.
  - BREAK-AND-RETEST TRACKING: when a breakout or extension signal fires,
    the coin is added to data/breakout-retest-watchlist.json. On later runs
    (when that coin comes up again in the rotation), its watchlist entry is
    checked against fresh OHLC for a retest-and-bounce pattern (price came
    back within RETEST_TOLERANCE_PCT of the level and closed back above it)
    - professionally, a confirmed retest is a stronger entry than the raw
    break itself. retest_confirmed appears on the coin's watchlist entry.
  - SIGNAL LOGGING: every check (fired or not) is appended to
    data/signal-log.json, capped at SIGNAL_LOG_MAX_ENTRIES. This is raw
    material for scripts/evaluate_signals.py to later measure whether these
    signals actually outperform doing nothing - calibration from real
    outcomes instead of assumptions about what threshold "should" work.

v2/v3 fixes (still present): CoinGecko-based Binance-listing rotation
(round-robin, not the same top-N every run), daily-aggregated volume
confirmation (not raw hourly points), retry-with-backoff on HTTP 429.
"""
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from statistics import mean
from datetime import datetime, timezone
from collections import defaultdict

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RADAR_FLAGS_PATH = DATA_DIR / "radar-flags.json"
ROTATION_STATE_PATH = DATA_DIR / "breakout-rotation-state.json"
RETEST_WATCHLIST_PATH = DATA_DIR / "breakout-retest-watchlist.json"
SIGNAL_LOG_PATH = DATA_DIR / "signal-log.json"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# --- resistance / breakout thresholds -----------------------------------
OHLC_DAYS = 30
PEAK_NEIGHBORS = 2
TOUCH_TOLERANCE_PCT = 1.0
MIN_TOUCHES = 2
EXCLUDE_RECENT_CANDLES = 3
BREAKOUT_BUFFER_PCT = 0.3
CONFIRM_CANDLES = 2

VOLUME_DAYS = 30
VOLUME_CONFIRM_MULTIPLIER = 1.3

# --- v4: trend filter -----------------------------------------------------
TREND_EMA_PERIOD = 50   # on the 4h candle series already fetched (~8-9 days) -
                          # an approximation of medium-term trend, NOT a true
                          # daily EMA50; documented as such wherever it's used

# --- v4: fibonacci extension continuation ---------------------------------
FIB_EXTENSION_RATIOS = {"1.272": 0.272, "1.618": 0.618}  # applied as level = high + ratio*(high-low)
EXTENSION_BUFFER_PCT = 0.3
EXTENSION_TARGET = "1.272"  # which extension level extension_continuation_signal requires clearing

# --- v4: insufficient history -----------------------------------------------
# 4h candles over OHLC_DAYS days implies roughly OHLC_DAYS*6 candles; if we
# get much less than that, the coin probably hasn't traded that long
EXPECTED_CANDLES = OHLC_DAYS * 6
MIN_CANDLE_COVERAGE_RATIO = 0.5  # need at least half the expected candles

# --- v4: retest tracking ---------------------------------------------------
RETEST_TOLERANCE_PCT = 1.5     # price must come back within this % of the level to count as a retest
RETEST_MAX_AGE_DAYS = 10       # stop watching for a retest after this long

# --- v4: signal log ---------------------------------------------------------
SIGNAL_LOG_MAX_ENTRIES = 2000

REQUEST_TIMEOUT = 20
POLITE_DELAY = 7
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 15
MAX_CANDIDATES_PER_RUN = 10


def load_rotation_offset() -> int:
    if not ROTATION_STATE_PATH.exists():
        return 0
    try:
        return json.loads(ROTATION_STATE_PATH.read_text(encoding="utf-8")).get("next_offset", 0)
    except (json.JSONDecodeError, AttributeError):
        return 0


def save_rotation_offset(offset: int) -> None:
    ROTATION_STATE_PATH.write_text(json.dumps({"next_offset": offset}), encoding="utf-8")


def select_rotating_candidates(all_listed: list) -> list:
    if not all_listed:
        return []
    ordered = sorted(all_listed, key=lambda c: c["id"])
    n = len(ordered)
    offset = load_rotation_offset() % n
    candidates = [ordered[(offset + i) % n] for i in range(min(MAX_CANDIDATES_PER_RUN, n))]
    save_rotation_offset((offset + len(candidates)) % n)
    return candidates


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "investment-radar/1.0"})
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code == 429 and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                print(f"  429 rate-limited, retrying in {wait}s (attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(wait)
                continue
            raise
    raise last_exc


def fetch_ohlc(coin_id: str):
    url = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc?{urllib.parse.urlencode({'vs_currency': 'usd', 'days': OHLC_DAYS})}"
    return fetch_json(url)


def fetch_hourly_volumes(coin_id: str):
    url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart?{urllib.parse.urlencode({'vs_currency': 'usd', 'days': VOLUME_DAYS})}"
    data = fetch_json(url)
    return data.get("total_volumes", [])


def daily_volumes_from_hourly(hourly: list):
    daily = defaultdict(float)
    for ts_ms, vol in hourly:
        day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
        daily[day] += vol or 0
    return [daily[d] for d in sorted(daily.keys())]


def find_resistance_zone(candles: list):
    if len(candles) < (PEAK_NEIGHBORS * 2 + MIN_TOUCHES + EXCLUDE_RECENT_CANDLES):
        return None

    history = candles[:-EXCLUDE_RECENT_CANDLES]
    highs = [c[2] for c in history]

    peaks = []
    for i in range(PEAK_NEIGHBORS, len(highs) - PEAK_NEIGHBORS):
        window = highs[i - PEAK_NEIGHBORS: i + PEAK_NEIGHBORS + 1]
        if highs[i] == max(window):
            peaks.append(highs[i])

    if len(peaks) < MIN_TOUCHES:
        return None

    peaks.sort()
    clusters = []
    current_cluster = [peaks[0]]
    for p in peaks[1:]:
        if (p - current_cluster[-1]) / current_cluster[-1] * 100 <= TOUCH_TOLERANCE_PCT:
            current_cluster.append(p)
        else:
            clusters.append(current_cluster)
            current_cluster = [p]
    clusters.append(current_cluster)

    valid = [c for c in clusters if len(c) >= MIN_TOUCHES]
    if not valid:
        return None
    best = max(valid, key=lambda c: (len(c), mean(c)))
    return {"level": mean(best), "touches": len(best)}


def check_breakout(candles: list, zone: dict):
    level = zone["level"]
    recent = candles[-CONFIRM_CANDLES:]
    closes_above = all(c[4] >= level * (1 + BREAKOUT_BUFFER_PCT / 100) for c in recent)

    pre_break = candles[-(CONFIRM_CANDLES + 3):-CONFIRM_CANDLES]
    was_below = any(c[4] < level for c in pre_break) if pre_break else True

    latest_close = candles[-1][4]
    pct_above = (latest_close - level) / level * 100
    return closes_above and was_below, pct_above


def check_volume(hourly_volumes: list):
    daily = daily_volumes_from_hourly(hourly_volumes)
    if len(daily) < 6:
        return None, False
    complete_days = daily[:-1]
    latest = complete_days[-1]
    baseline = complete_days[:-1]
    avg = mean(baseline) if baseline else 0
    if avg == 0:
        return None, False
    ratio = latest / avg
    return round(ratio, 2), ratio >= VOLUME_CONFIRM_MULTIPLIER


# ---------- v4: trend filter ----------

def compute_ema(values: list, period: int) -> float:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def check_trend_aligned(candles: list):
    """True/False if we have enough candles for the EMA, else None (filter
    is skipped rather than blocking the signal on insufficient data)."""
    closes = [c[4] for c in candles]
    ema = compute_ema(closes, TREND_EMA_PERIOD)
    if ema is None:
        return None, None
    return closes[-1] > ema, round(ema, 8)


# ---------- v4: fibonacci extension ----------

def compute_fib_extensions(low: float, high: float) -> dict:
    diff = high - low
    return {name: high + ratio * diff for name, ratio in FIB_EXTENSION_RATIOS.items()}


def check_extension_continuation(candles: list, zone: dict, volume_confirmed: bool, trend_aligned):
    """Anchor A = lowest low in the same lookback window used to find the
    resistance zone; Anchor B = the zone level itself. Signal fires when the
    latest close clears the target extension with volume support and the
    trend filter (when available) agrees - this is meant for coins that
    already broke resistance a while ago (breakout_confirmed=False because
    the break isn't fresh) and are still extending, like INJ."""
    history = candles[:-EXCLUDE_RECENT_CANDLES]
    if not history:
        return None

    swing_low = min(c[3] for c in history)  # c[3] = low
    high = zone["level"]
    if swing_low >= high:
        return None  # degenerate anchor, skip

    extensions = compute_fib_extensions(swing_low, high)
    target_level = extensions[EXTENSION_TARGET]
    latest_close = candles[-1][4]

    cleared = latest_close >= target_level * (1 + EXTENSION_BUFFER_PCT / 100)
    signal = bool(cleared and volume_confirmed and (trend_aligned is not False))

    return {
        "fib_anchor_low": round(swing_low, 8),
        "fib_anchor_high": round(high, 8),
        "fib_extension_1272": round(extensions["1.272"], 8),
        "fib_extension_1618": round(extensions["1.618"], 8),
        "extension_continuation_signal": signal,
    }


# ---------- v4: retest watchlist ----------

def load_retest_watchlist() -> dict:
    if not RETEST_WATCHLIST_PATH.exists():
        return {}
    try:
        return json.loads(RETEST_WATCHLIST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_retest_watchlist(watchlist: dict) -> None:
    RETEST_WATCHLIST_PATH.write_text(json.dumps(watchlist, ensure_ascii=False, indent=2), encoding="utf-8")


def add_to_retest_watchlist(watchlist: dict, coin: dict, level: float, signal_type: str) -> None:
    entry_id = f"{coin['id']}:{signal_type}:{round(level, 6)}"
    if entry_id in watchlist:
        return  # already watching this exact level/signal for this coin
    watchlist[entry_id] = {
        "coin_id": coin["id"],
        "symbol": coin["symbol"],
        "level": level,
        "signal_type": signal_type,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "retest_confirmed": False,
        "status": "watching",
    }


def update_retest_entries_for_coin(watchlist: dict, coin_id: str, candles: list) -> None:
    """Check this coin's fresh candles against any open watchlist entries for
    it: a retest is a low that came within RETEST_TOLERANCE_PCT of the level
    followed by a later close back above it."""
    now = datetime.now(timezone.utc)
    for entry in watchlist.values():
        if entry["coin_id"] != coin_id or entry["status"] != "watching":
            continue

        detected_at = datetime.fromisoformat(entry["detected_at"])
        age_days = (now - detected_at).total_seconds() / 86400
        if age_days > RETEST_MAX_AGE_DAYS:
            entry["status"] = "expired"
            continue

        level = entry["level"]
        touched = False
        for i, c in enumerate(candles):
            low = c[3]
            if abs(low - level) / level * 100 <= RETEST_TOLERANCE_PCT:
                touched = True
                # look for a later close back above the level (the bounce)
                for later in candles[i + 1:]:
                    if later[4] > level * (1 + BREAKOUT_BUFFER_PCT / 100):
                        entry["retest_confirmed"] = True
                        entry["status"] = "confirmed"
                        entry["confirmed_at"] = now.isoformat()
                        break
        if touched and entry["status"] == "watching":
            entry["status"] = "touched_awaiting_bounce"


# ---------- v4: signal log ----------

def append_signal_log(coin: dict, record: dict) -> None:
    log = []
    if SIGNAL_LOG_PATH.exists():
        try:
            log = json.loads(SIGNAL_LOG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log = []
    log.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "coin_id": coin["id"],
        "symbol": coin["symbol"],
        "price_at_check": coin.get("price_usd"),
        **record,
        "evaluated": False,
    })
    if len(log) > SIGNAL_LOG_MAX_ENTRIES:
        log = log[-SIGNAL_LOG_MAX_ENTRIES:]
    SIGNAL_LOG_PATH.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    if not RADAR_FLAGS_PATH.exists():
        print("No radar-flags.json found, skipping breakout check.")
        return

    data = json.loads(RADAR_FLAGS_PATH.read_text(encoding="utf-8"))
    coins = data.get("coins", [])

    all_listed = [c for c in coins if c.get("binance_listed") is True]
    candidates = select_rotating_candidates(all_listed)

    print(f"Running breakout check on {len(candidates)} of {len(all_listed)} Binance-listed candidates "
          f"(rotating selection - skipping {len(coins) - len(all_listed)} not listed/unchecked).")

    retest_watchlist = load_retest_watchlist()

    for coin in candidates:
        coin_id = coin["id"]
        try:
            candles = fetch_ohlc(coin_id)
            time.sleep(POLITE_DELAY)
            hourly_volumes = fetch_hourly_volumes(coin_id)
            time.sleep(POLITE_DELAY)
        except Exception as exc:  # noqa: BLE001
            coin["breakout_signal"] = None
            coin["breakout_error"] = str(exc)
            continue

        # always update retest tracking for this coin, even if the rest fails
        if candles:
            update_retest_entries_for_coin(retest_watchlist, coin_id, candles)

        if candles and len(candles) < EXPECTED_CANDLES * MIN_CANDLE_COVERAGE_RATIO:
            coin["insufficient_history"] = True
            coin["breakout_signal"] = False
            coin["extension_continuation_signal"] = False
            coin.pop("breakout_error", None)
            append_signal_log(coin, {
                "breakout_signal": False, "extension_continuation_signal": False,
                "reason": "insufficient_history", "n_candles": len(candles),
            })
            continue

        zone = find_resistance_zone(candles) if candles else None
        if zone is None:
            coin["breakout_signal"] = False
            coin["extension_continuation_signal"] = False
            coin.pop("breakout_error", None)
            append_signal_log(coin, {"breakout_signal": False, "extension_continuation_signal": False, "reason": "no_resistance_zone_found"})
            continue

        breakout_confirmed, pct_above = check_breakout(candles, zone)
        volume_ratio, volume_confirmed = check_volume(hourly_volumes)
        trend_aligned, trend_ema = check_trend_aligned(candles)

        coin["resistance_level"] = round(zone["level"], 6)
        coin["resistance_touches"] = zone["touches"]
        coin["breakout_confirmed"] = breakout_confirmed
        coin["breakout_pct_above"] = round(pct_above, 2)
        coin["volume_ratio"] = volume_ratio
        coin["volume_confirmed"] = volume_confirmed
        coin["trend_aligned"] = trend_aligned
        coin["trend_ema50_4h_approx"] = round(trend_ema, 8) if trend_ema else None
        coin["breakout_signal"] = bool(breakout_confirmed and volume_confirmed and (trend_aligned is not False))
        coin.pop("breakout_error", None)

        ext_result = check_extension_continuation(candles, zone, volume_confirmed, trend_aligned)
        if ext_result:
            coin.update(ext_result)
        else:
            coin["extension_continuation_signal"] = False

        if coin["breakout_signal"]:
            add_to_retest_watchlist(retest_watchlist, coin, zone["level"], "breakout")
        if coin.get("extension_continuation_signal"):
            add_to_retest_watchlist(retest_watchlist, coin, ext_result["fib_extension_1272"], "extension")

        append_signal_log(coin, {
            "breakout_signal": coin["breakout_signal"],
            "extension_continuation_signal": coin.get("extension_continuation_signal", False),
            "resistance_level": coin["resistance_level"],
            "volume_ratio": volume_ratio,
            "trend_aligned": trend_aligned,
        })

    save_retest_watchlist(retest_watchlist)

    RADAR_FLAGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    signals = sum(1 for c in candidates if c.get("breakout_signal"))
    ext_signals = sum(1 for c in candidates if c.get("extension_continuation_signal"))
    confirmed_retests = sum(1 for e in retest_watchlist.values() if e.get("retest_confirmed"))
    print(f"Checked {len(candidates)} candidates: {signals} fresh breakouts, {ext_signals} extension "
          f"continuations, {confirmed_retests} confirmed retests in the watchlist.")


if __name__ == "__main__":
    main()
