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

# --- v7: descending-trendline break detection ------------------------------
# Rationale (from Azez's own repeated pattern observation on 2026-09-20,
# FARTCOIN): breakout_signal/resistance_level above only catch a FLAT
# horizontal level breaking. A lot of real setups are downtrend -> price
# breaks above a DIAGONAL trendline connecting a series of lower highs ->
# holds above it for a while -> then launches. This adds that as its own,
# separate signal so it can be watched and entry-timed independently of
# the horizontal-breakout logic above.
TRENDLINE_WATCHLIST_PATH = DATA_DIR / "trendline-watchlist.json"
TRENDLINE_SWING_LOOKBACK_CANDLES = 5        # a candle counts as a swing high if it's the max of the K candles either side
TRENDLINE_MAX_CANDLES = 90                  # only look for swing highs within this recent window
TRENDLINE_MIN_SWING_POINTS = 2              # need at least this many genuinely descending highs to fit a line
TRENDLINE_CONFIRMATION_CANDLES = 2          # v10: total candles (not necessarily consecutive) that must CLOSE above the frozen line before "confirmed" - lowered from 3
TRENDLINE_INVALIDATION_CONSECUTIVE = 2      # v10: a REAL breakdown back below needs this many CONSECUTIVE closes below the line - a single dip/wick doesn't erase progress
TRENDLINE_MAX_AGE_DAYS = 10                 # stop watching a break this old if it never confirms

# --- v8: composite confidence score, ATR-based stop, Binance derivatives --
# Rationale (from the 2026-09-20 design review): a single pass/fail signal
# hides how MUCH evidence actually supports it. This replaces "did it fire"
# with a 0-100 score built from weighted components, where weights start
# conservative and are meant to be recalibrated later from agent-room-log.json
# once there's enough sample size (see calibrate_weights.py note below) -
# NOT frozen opinions. Our own backtest-results.json showed the raw
# breakout_signal itself barely beats a coin flip (53.3% vs 51.2% baseline),
# which is why it gets a LOW starting weight here, not a high one.
INDICATOR_WEIGHTS_PATH = Path(__file__).resolve().parent.parent / "config" / "indicator-weights.json"
DEFAULT_INDICATOR_WEIGHTS = {
    "breakout_or_trendline_signal": 15,   # weak standalone edge per our own backtest - counted, not trusted alone
    "volume_confirmed": 10,
    "trend_aligned": 10,
    "idiosyncratic_quality": 25,          # the single factor that actually flipped OP and ARB's verdicts
    "oi_price_confirms": 15,              # new v8: real demand vs short-covering (the exact ARB gap)
    "funding_not_crowded": 10,            # new v8: penalizes chasing an already one-sided, crowded trade
    "deep_drawdown_penalty": -10,
    "unlock_risk_penalty": -10,
}
ATR_PERIOD = 14
LIQUIDITY_STOP_BUFFER_ATR_MULT = 0.5     # push the stop this many ATRs past the obvious level
LIQUIDITY_STOP_MIN_BUFFER_PCT = 0.3      # ...or at least this % of price, whichever is bigger (for low-volatility coins where 0.5*ATR would be tiny)
BYBIT_API_BASE = "https://api.bybit.com/v5/market"  # v12: switched from Binance (fapi.binance.com), which returns HTTP 451 for GitHub Actions' IP ranges - confirmed via the v11 diagnostic logging Azez ran
FUNDING_REVERSAL_LOOKBACK = 6                # how many recent 8h funding readings to check for a sign flip
OI_BASELINE_PATH = DATA_DIR / "oi-baseline.json"
POLITE_DELAY = 7
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 15
# v9: raised from 10 - each extra candidate costs ~2-3 CoinGecko calls (+1
# Binance pair for fired signals only), so at POLITE_DELAY=7s the run grows
# from ~2-3min to ~4-5min at 16. Kept below 20 to leave headroom under
# CoinGecko's free-tier rate limit rather than pushing it to the edge.
MAX_CANDIDATES_PER_RUN = 16


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


# ---------- v7: descending-trendline break detection ----------

def find_swing_highs(candles: list, k: int = TRENDLINE_SWING_LOOKBACK_CANDLES) -> list:
    """Returns [(index, high_price), ...] for local maxima - a candle whose
    high is >= every candle's high within k positions on either side."""
    highs = [c[2] for c in candles]
    n = len(highs)
    swings = []
    for i in range(k, n - k):
        window = highs[i - k:i + k + 1]
        if highs[i] == max(window):
            swings.append((i, highs[i]))
    return swings


def fit_descending_trendline(candles: list):
    """Finds the most recent run of genuinely descending swing highs (each
    earlier one higher than the next, moving forward in time - the exact
    'connect the lower highs' line a chart reader draws by hand) within
    TRENDLINE_MAX_CANDLES, and fits a least-squares line through them in
    (candle_index, price) space. Returns (slope, intercept, n_points) or
    None if there's no qualifying descending sequence. Pure-python least
    squares (no numpy dependency, consistent with the rest of this file)."""
    recent = candles[-TRENDLINE_MAX_CANDLES:]
    offset = len(candles) - len(recent)
    swings = find_swing_highs(recent)
    if len(swings) < TRENDLINE_MIN_SWING_POINTS:
        return None
    descending = [swings[-1]]
    for point in reversed(swings[:-1]):
        if point[1] > descending[-1][1]:
            descending.append(point)
    descending.reverse()
    if len(descending) < TRENDLINE_MIN_SWING_POINTS:
        return None
    xs = [p[0] + offset for p in descending]
    ys = [p[1] for p in descending]
    mean_x, mean_y = mean(xs), mean(ys)
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    if slope >= 0:
        return None  # fitted line isn't actually descending - reject rather than force it
    intercept = mean_y - slope * mean_x
    return slope, intercept, len(xs)


def load_trendline_watchlist() -> dict:
    if not TRENDLINE_WATCHLIST_PATH.exists():
        return {}
    try:
        return json.loads(TRENDLINE_WATCHLIST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_trendline_watchlist(watchlist: dict) -> None:
    TRENDLINE_WATCHLIST_PATH.write_text(json.dumps(watchlist, ensure_ascii=False, indent=2), encoding="utf-8")


def check_trendline_break(watchlist: dict, coin: dict, candles: list) -> dict:
    """v10 (refined from v7 per Azez's 2026-09-20 feedback): confirmation and
    invalidation are now two SEPARATE counters instead of one all-or-nothing
    streak. Rationale: a single wick/dip back below a freshly-broken
    trendline is normal noise, not proof the break failed - resetting all
    progress to zero on one such candle (the old v7 behavior) was too
    strict and could erase a genuinely good break over one brief pullback.
    - trendline_candles_held: CUMULATIVE count of every candle (not
      necessarily consecutive) that closed above the frozen line since
      detection - confirmation only needs TRENDLINE_CONFIRMATION_CANDLES
      of these total, so a single dip and recovery still counts both
      "above" candles toward it.
    - consecutive closes BELOW the line are tracked separately and only
      invalidate the whole watch entry once they reach
      TRENDLINE_INVALIDATION_CONSECUTIVE in a row - a real reversal back
      under the line, not a single poke.
    """
    coin_id = coin["id"]
    last_index = len(candles) - 1
    last_close = candles[-1][4]
    result = {
        "trendline_break_detected": False,
        "trendline_break_confirmed_signal": False,
        "trendline_candles_held": 0,
        "trendline_value_now": None,
    }

    entry = watchlist.get(coin_id)
    if entry and entry.get("status") in ("watching", "confirmed"):
        slope, intercept = entry["slope"], entry["intercept"]
        projected_now = slope * last_index + intercept
        result["trendline_value_now"] = round(projected_now, 8)

        held = 0                # cumulative candles closed above the line - never resets on a dip
        consecutive_below = 0   # resets to 0 the moment a candle closes back above
        for idx in range(entry["detected_at_index"], len(candles)):
            proj = slope * idx + intercept
            if candles[idx][4] > proj * (1 + BREAKOUT_BUFFER