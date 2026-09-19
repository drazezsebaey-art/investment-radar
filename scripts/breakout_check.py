"""
Investment Radar - Resistance Breakout Check (v5)
------------------------------------------------------------
Runs after check_liquidity.py, only against coins already confirmed
binance_listed == True in data/radar-flags.json.

v5 additions (from reviewing an academic scalping-bot paper and a general
scalping guide):
  - VWAP CONFIRMATION (independent evidence layer): Volume Weighted Average
    Price answers a different question than EMA - not "which direction is
    price drifting" but "where has most of the actual traded volume
    happened." Computed by bucketing the hourly volume series (already
    fetched for volume confirmation) into the same 4h windows as the OHLC
    candles, then vwap = sum(typical_price * bucket_volume) / sum(bucket_volume)
    over the lookback window. above_vwap is reported alongside breakout
    checks as ADDITIONAL confluence - it does NOT gate breakout_signal
    (changing that definition now would break comparability with existing
    logged signals), but a new composite field
    breakout_signal_high_confidence = breakout_signal AND above_vwap is
    added for anyone who wants the stricter read.
  - PULLBACK ENTRY SIGNAL (a second, independent signal type): catches a
    different situation than a resistance break - a coin already in a
    confirmed uptrend (price above both EMA50 and EMA100 on the same 4h
    series) pulling back and then resuming, confirmed by a Stochastic
    Oscillator(14) %K crossing back above 20 from oversold. This doesn't
    require or use the resistance-zone logic at all, so it can fire on
    coins where no clean resistance zone was found. Adapted from a
    reviewed EMA+Stochastic scalping strategy (using EMA50/EMA100 instead
    of the original's EMA50/EMA200, since our OHLC_DAYS window can't
    reliably seed an EMA200 on 4h candles).
  - OHLC_DAYS raised from 30 to 45 to give the EMA100 calculation enough
    candles to seed properly (CoinGecko still returns 4h candles up to 90
    days, so this doesn't change candle granularity, just history depth).

v2-v4 fixes/features preserved: CoinGecko-based Binance-listing rotation,
daily-aggregated volume confirmation, retry-with-backoff on HTTP 429,
resistance-zone clustering + breakout confirmation, fibonacci extension
continuation signal, EMA50-on-4h trend filter, insufficient-history flag,
break-and-retest tracking, signal logging for scripts/evaluate_signals.py.
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
OHLC_DAYS = 30                  # CoinGecko's /ohlc endpoint only accepts specific values
                                  # (1, 7, 14, 30, 90, 180, 365) - NOT an arbitrary number like 45.
                                  # 30 days already gives ~180 4h-candles, comfortably enough to
                                  # seed EMA100; a prior v5 change to 45 was invalid and caused a
                                  # uniform HTTP 400 on every single coin - reverted here.
PEAK_NEIGHBORS = 2
TOUCH_TOLERANCE_PCT = 1.0
MIN_TOUCHES = 2
EXCLUDE_RECENT_CANDLES = 3
BREAKOUT_BUFFER_PCT = 0.3
CONFIRM_CANDLES = 2

VOLUME_DAYS = 30                # market_chart accepts any integer, but keep matched to OHLC_DAYS for consistency
VOLUME_CONFIRM_MULTIPLIER = 1.3

# --- trend filter -----------------------------------------------------
TREND_EMA_PERIOD = 50
TREND_EMA_LONG_PERIOD = 100     # v5: for pullback-signal trend confirmation

# --- fibonacci extension continuation ---------------------------------
FIB_EXTENSION_RATIOS = {"1.272": 0.272, "1.618": 0.618}
EXTENSION_BUFFER_PCT = 0.3
EXTENSION_TARGET = "1.272"

# --- insufficient history -----------------------------------------------
EXPECTED_CANDLES = OHLC_DAYS * 6
MIN_CANDLE_COVERAGE_RATIO = 0.5

# --- retest tracking ---------------------------------------------------
RETEST_TOLERANCE_PCT = 1.5
RETEST_MAX_AGE_DAYS = 10

# --- v5: VWAP -------------------------------------------------------------
VWAP_LOOKBACK_CANDLES = 30      # ~5 days at 4h - a shorter, more reactive window than the full history

# --- v5: pullback entry (Stochastic) --------------------------------------
STOCH_PERIOD = 14
STOCH_OVERSOLD_LEVEL = 20.0

# --- signal log ---------------------------------------------------------
SIGNAL_LOG_MAX_ENTRIES = 2000

# --- v6: cluster / beta-driven filter --------------------------------------
# Rationale (from the 2026-09-19 OP/STRK/NEAR/JUP/PENDLE review): a fired
# breakout_signal on its own doesn't distinguish an idiosyncratic move from
# "everything is pumping together because the whole market/sector is
# risk-on right now" - the latter is lower-quality because it's really one
# correlated bet, not five independent ones, and it reverses in lockstep
# too. Two independent checks for this, both cheap:
#   1. within-run cluster count: if >= CLUSTER_SIGNAL_THRESHOLD candidates
#      in the SAME run fire a signal, they're flagged cluster_wide_signal.
#   2. BTC correlation: if a candidate's own last ~7 days of 4h closes
#      correlate highly with BTC's, its move is mostly beta, not alpha.
# Either one downgrades signal_quality from "idiosyncratic" to
# "beta_driven_or_cluster" - the signal still fires (data isn't hidden),
# it's just labeled so a human (or Agent Room) doesn't treat 5 correlated
# pumps as 5 independent opportunities.
BTC_COIN_ID = "bitcoin"
CORRELATION_LOOKBACK_CANDLES = 42       # ~7 days at 4h candles
CORRELATION_MIN_OVERLAP = 20            # need at least this many paired points to trust the number
HIGH_CORRELATION_THRESHOLD = 0.75
CLUSTER_SIGNAL_THRESHOLD = 3            # >= this many fired signals in one run = treat as cluster/beta-driven

# --- v6: lightweight fundamental red flags ---------------------------------
# Deliberately NOT a full fundamental engine (that stays a human/Claude
# Agent Room job) - just two cheap, objective, automatable checks that
# would have correctly down-weighted OP on 2026-09-19 (deep multi-year
# drawdown + large scheduled unlocks) without needing news judgment.
DEEP_DRAWDOWN_ATH_PCT = -90.0            # ath_change_percentage_usd below this = flagged
UNLOCK_WARNING_DAYS = 14                 # flag if a KNOWN upcoming unlock lands within this window
KNOWN_UNLOCKS_PATH = Path(__file__).resolve().parent.parent / "config" / "known-unlocks.json"

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


def fetch_coin_market_data(coin_id: str):
    """v6: one extra call for ath_change_percentage - the cheapest available
    proxy for 'is this a structurally beaten-down asset', regardless of how
    clean the short-term chart looks."""
    params = {
        "localization": "false", "tickers": "false", "market_data": "true",
        "community_data": "false", "developer_data": "false", "sparkline": "false",
    }
    url = f"{COINGECKO_BASE}/coins/{coin_id}?{urllib.parse.urlencode(params)}"
    data = fetch_json(url)
    md = data.get("market_data", {}) or {}
    ath_change = (md.get("ath_change_percentage") or {}).get("usd")
    return ath_change


# ---------- v6: BTC correlation (cluster/beta filter) ----------

def compute_pearson_correlation(a: list, b: list):
    n = min(len(a), len(b))
    if n < CORRELATION_MIN_OVERLAP:
        return None
    a, b = a[-n:], b[-n:]
    mean_a, mean_b = mean(a), mean(b)
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    denom = (var_a * var_b) ** 0.5
    if denom == 0:
        return None
    return cov / denom


def fetch_btc_closes():
    """Fetched ONCE per run (not per candidate) and reused - keeps the
    extra API cost flat regardless of how many candidates are checked."""
    try:
        candles = fetch_ohlc(BTC_COIN_ID)
        time.sleep(POLITE_DELAY)
        return [c[4] for c in candles]
    except Exception as exc:  # noqa: BLE001
        print(f"  Warning: could not fetch BTC closes for correlation check: {exc}")
        return None


# ---------- v6: known-unlocks config (manually maintained) ----------

def load_known_unlocks() -> dict:
    """Free CoinGecko tiers don't expose reliable vesting/unlock-schedule
    data, so this is a small manually-maintained config instead of an API
    call. Format: {"coin_id": [{"date": "YYYY-MM-DD", "pct_of_supply": 1.2,
    "note": "..."}]}. Missing file or missing coin_id = no flag, not an
    error - this is meant to be filled in gradually as you research
    specific watchlist coins, not a complete database."""
    if not KNOWN_UNLOCKS_PATH.exists():
        return {}
    try:
        return json.loads(KNOWN_UNLOCKS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def check_unlock_risk(coin_id: str, unlocks_config: dict, now: datetime):
    events = unlocks_config.get(coin_id, [])
    upcoming = []
    for ev in events:
        try:
            ev_date = datetime.fromisoformat(ev["date"]).replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        days_until = (ev_date - now).total_seconds() / 86400
        if 0 <= days_until <= UNLOCK_WARNING_DAYS:
            upcoming.append((days_until, ev))
    if not upcoming:
        return False, None, None
    upcoming.sort(key=lambda t: t[0])
    _, nearest = upcoming[0]
    return True, nearest["date"], nearest.get("pct_of_supply")


def daily_volumes_from_hourly(hourly: list):
    daily = defaultdict(float)
    for ts_ms, vol in hourly:
        day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
        daily[day] += vol or 0
    return [daily[d] for d in sorted(daily.keys())]


def bucket_volumes_to_candles(candles: list, hourly_volumes: list) -> list:
    """Sum hourly volume points falling within each candle's time window,
    returning a list of volumes parallel to candles. Used for VWAP, since
    the OHLC endpoint itself carries no volume field."""
    if not candles or not hourly_volumes:
        return [0] * len(candles)
    candle_starts = [c[0] for c in candles]
    bucket_vols = [0.0] * len(candles)
    for ts_ms, vol in hourly_volumes:
        # find the last candle whose start is <= this volume point's timestamp
        idx = None
        for i, start in enumerate(candle_starts):
            if start <= ts_ms:
                idx = i
            else:
                break
        if idx is not None:
            bucket_vols[idx] += vol or 0
    return bucket_vols


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


def compute_ema(values: list, period: int) -> float:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def check_trend_aligned(candles: list):
    closes = [c[4] for c in candles]
    ema = compute_ema(closes, TREND_EMA_PERIOD)
    if ema is None:
        return None, None
    return closes[-1] > ema, round(ema, 8)


def compute_fib_extensions(low: float, high: float) -> dict:
    diff = high - low
    return {name: high + ratio * diff for name, ratio in FIB_EXTENSION_RATIOS.items()}


def check_extension_continuation(candles: list, zone: dict, volume_confirmed: bool, trend_aligned):
    history = candles[:-EXCLUDE_RECENT_CANDLES]
    if not history:
        return None
    swing_low = min(c[3] for c in history)
    high = zone["level"]
    if swing_low >= high:
        return None
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


# ---------- v5: VWAP ----------

def compute_vwap(candles: list, bucket_volumes: list, lookback: int) -> float:
    window_candles = candles[-lookback:]
    window_volumes = bucket_volumes[-lookback:]
    total_vol = sum(window_volumes)
    if total_vol <= 0:
        return None
    weighted_sum = 0.0
    for c, vol in zip(window_candles, window_volumes):
        typical_price = (c[2] + c[3] + c[4]) / 3  # (high+low+close)/3
        weighted_sum += typical_price * vol
    return weighted_sum / total_vol


# ---------- v5: Stochastic + pullback entry ----------

def compute_stochastic_k_series(candles: list, period: int) -> list:
    """Returns %K for every candle index where enough lookback exists (else None)."""
    k_values = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1: i + 1]
        highest_high = max(c[2] for c in window)
        lowest_low = min(c[3] for c in window)
        close = candles[i][4]
        if highest_high == lowest_low:
            k_values[i] = 50.0
        else:
            k_values[i] = (close - lowest_low) / (highest_high - lowest_low) * 100
    return k_values


def check_pullback_entry(candles: list, volume_confirmed: bool):
    """Independent of resistance-zone logic entirely: uptrend (price above
    both EMA50 and EMA100) + Stochastic %K crossing back above the oversold
    level = a pullback resuming, adapted from a reviewed EMA+Stochastic
    scalping strategy (their EMA50/EMA200 -> our EMA50/EMA100, since our
    window can't reliably seed EMA200 on 4h candles)."""
    closes = [c[4] for c in candles]
    ema50 = compute_ema(closes, TREND_EMA_PERIOD)
    ema100 = compute_ema(closes, TREND_EMA_LONG_PERIOD)
    if ema50 is None or ema100 is None:
        return None

    k_series = compute_stochastic_k_series(candles, STOCH_PERIOD)
    if k_series[-1] is None or k_series[-2] is None:
        return None

    uptrend_aligned = closes[-1] > ema50 and closes[-1] > ema100
    crossed_up = k_series[-2] <= STOCH_OVERSOLD_LEVEL < k_series[-1]

    signal = bool(uptrend_aligned and crossed_up and volume_confirmed)
    return {
        "ema50_4h_approx": round(ema50, 8),
        "ema100_4h_approx": round(ema100, 8),
        "stochastic_k": round(k_series[-1], 2),
        "stochastic_k_prev": round(k_series[-2], 2),
        "pullback_entry_signal": signal,
    }


# ---------- retest watchlist ----------

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
        return
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
                for later in candles[i + 1:]:
                    if later[4] > level * (1 + BREAKOUT_BUFFER_PCT / 100):
                        entry["retest_confirmed"] = True
                        entry["status"] = "confirmed"
                        entry["confirmed_at"] = now.isoformat()
                        break
        if touched and entry["status"] == "watching":
            entry["status"] = "touched_awaiting_bounce"


def queue_signal_log(pending: list, coin: dict, record: dict) -> None:
    """v6: builds the log entry in memory instead of writing immediately.
    cluster_wide_signal/signal_quality are only knowable after ALL
    candidates in this run have been checked (need the full-run signal
    count), so the actual file write is deferred to flush_signal_log()
    after that's computed - this also cuts signal-log.json from N reads+
    writes per run down to one."""
    pending.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "coin_id": coin["id"],
        "symbol": coin["symbol"],
        "price_at_check": coin.get("price_usd"),
        **record,
        "btc_correlation_7d": coin.get("btc_correlation_7d"),
        "ath_change_pct": coin.get("ath_change_pct"),
        "deep_drawdown_flag": coin.get("deep_drawdown_flag"),
        "unlock_risk_flag": coin.get("unlock_risk_flag"),
        "_coin_ref": coin,  # temporary - resolved to cluster_wide_signal/signal_quality in flush_signal_log
        "evaluated": False,
    })


def flush_signal_log(pending: list) -> None:
    log = []
    if SIGNAL_LOG_PATH.exists():
        try:
            log = json.loads(SIGNAL_LOG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log = []
    for entry in pending:
        coin_ref = entry.pop("_coin_ref")
        entry["cluster_wide_signal"] = coin_ref.get("cluster_wide_signal")
        entry["signal_quality"] = coin_ref.get("signal_quality")
        log.append(entry)
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
          f"(rotating selection).")

    retest_watchlist = load_retest_watchlist()

    # v6: fetched/loaded once per run, reused for every candidate below
    btc_closes = fetch_btc_closes()
    unlocks_config = load_known_unlocks()
    now = datetime.now(timezone.utc)
    pending_log_entries = []

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

        if candles:
            update_retest_entries_for_coin(retest_watchlist, coin_id, candles)

        # v6: BTC correlation (skip for BTC itself - correlating BTC with BTC is meaningless)
        if candles and btc_closes and coin_id != BTC_COIN_ID:
            candidate_closes = [c[4] for c in candles][-CORRELATION_LOOKBACK_CANDLES:]
            btc_tail = btc_closes[-CORRELATION_LOOKBACK_CANDLES:]
            coin["btc_correlation_7d"] = round(compute_pearson_correlation(candidate_closes, btc_tail) or 0, 3) \
                if compute_pearson_correlation(candidate_closes, btc_tail) is not None else None
        else:
            coin["btc_correlation_7d"] = None

        # v6: ATH drawdown (lightweight fundamental red flag #1)
        try:
            ath_change = fetch_coin_market_data(coin_id)
            time.sleep(POLITE_DELAY)
            coin["ath_change_pct"] = round(ath_change, 2) if ath_change is not None else None
            coin["deep_drawdown_flag"] = bool(ath_change is not None and ath_change <= DEEP_DRAWDOWN_ATH_PCT)
        except Exception as exc:  # noqa: BLE001
            print(f"  Warning: ATH fetch failed for {coin_id}: {exc}")
            coin["ath_change_pct"] = None
            coin["deep_drawdown_flag"] = None

        # v6: known-unlock proximity (lightweight fundamental red flag #2)
        unlock_flag, unlock_date, unlock_pct = check_unlock_risk(coin_id, unlocks_config, now)
        coin["unlock_risk_flag"] = unlock_flag
        coin["next_known_unlock_date"] = unlock_date
        coin["next_known_unlock_pct_supply"] = unlock_pct

        if candles and len(candles) < EXPECTED_CANDLES * MIN_CANDLE_COVERAGE_RATIO:
            coin["insufficient_history"] = True
            coin["breakout_signal"] = False
            coin["extension_continuation_signal"] = False
            coin["pullback_entry_signal"] = False
            coin.pop("breakout_error", None)
            queue_signal_log(pending_log_entries, coin, {
                "breakout_signal": False, "extension_continuation_signal": False,
                "pullback_entry_signal": False, "reason": "insufficient_history", "n_candles": len(candles),
            })
            continue

        volume_ratio, volume_confirmed = check_volume(hourly_volumes) if hourly_volumes else (None, False)
        bucket_vols = bucket_volumes_to_candles(candles, hourly_volumes) if hourly_volumes else [0] * len(candles)
        vwap = compute_vwap(candles, bucket_vols, VWAP_LOOKBACK_CANDLES)
        above_vwap = (candles[-1][4] > vwap) if vwap else None
        coin["vwap_4h_approx"] = round(vwap, 8) if vwap else None
        coin["above_vwap"] = above_vwap

        # pullback entry signal - independent of resistance zone
        pullback_result = check_pullback_entry(candles, volume_confirmed)
        if pullback_result:
            coin.update(pullback_result)
        else:
            coin["pullback_entry_signal"] = False

        zone = find_resistance_zone(candles) if candles else None
        if zone is None:
            coin["breakout_signal"] = False
            coin["extension_continuation_signal"] = False
            coin.pop("breakout_error", None)
            queue_signal_log(pending_log_entries, coin, {
                "breakout_signal": False, "extension_continuation_signal": False,
                "pullback_entry_signal": coin["pullback_entry_signal"], "reason": "no_resistance_zone_found",
            })
            continue

        breakout_confirmed, pct_above = check_breakout(candles, zone)
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
        coin["breakout_signal_high_confidence"] = bool(coin["breakout_signal"] and above_vwap)
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

        queue_signal_log(pending_log_entries, coin, {
            "breakout_signal": coin["breakout_signal"],
            "breakout_signal_high_confidence": coin["breakout_signal_high_confidence"],
            "extension_continuation_signal": coin.get("extension_continuation_signal", False),
            "pullback_entry_signal": coin["pullback_entry_signal"],
            "resistance_level": coin["resistance_level"],
            "volume_ratio": volume_ratio,
            "trend_aligned": trend_aligned,
            "above_vwap": above_vwap,
        })

    # v6: cluster-wide detection - only knowable after every candidate in this
    # run has been checked. A coin only gets a signal_quality label if it
    # actually fired something (no point labeling non-signals).
    fired = [c for c in candidates if c.get("breakout_signal") or c.get("extension_continuation_signal")]
    is_cluster_run = len(fired) >= CLUSTER_SIGNAL_THRESHOLD
    for coin in fired:
        coin["cluster_wide_signal"] = is_cluster_run
        high_corr = (coin.get("btc_correlation_7d") or 0) >= HIGH_CORRELATION_THRESHOLD
        coin["signal_quality"] = "beta_driven_or_cluster" if (is_cluster_run or high_corr) else "idiosyncratic"

    flush_signal_log(pending_log_entries)
    save_retest_watchlist(retest_watchlist)

    RADAR_FLAGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    signals = sum(1 for c in candidates if c.get("breakout_signal"))
    ext_signals = sum(1 for c in candidates if c.get("extension_continuation_signal"))
    pullback_signals = sum(1 for c in candidates if c.get("pullback_entry_signal"))
    idiosyncratic = sum(1 for c in fired if c.get("signal_quality") == "idiosyncratic")
    print(f"Checked {len(candidates)} candidates: {signals} breakouts, {ext_signals} extensions, "
          f"{pullback_signals} pullback entries ({idiosyncratic}/{len(fired)} fired signals rated "
          f"idiosyncratic vs beta_driven_or_cluster).")


if __name__ == "__main__":
    main()
