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

v15 additions (from the 22/9/2026 committee audit): select_rotating_candidates()
now also escalates any coin scan.py flagged priority_review this run (a
Layer-2 early signal - CHoCH/squeeze/RSI-divergence/cluster-lag/relative-
strength-in-consolidation) into the SAME run's deep evaluation, on top of
the normal rotation slot, closing the detection-latency gap the audit found.
check_unlock_risk() now also reports unlock_data_checked, distinguishing
"never researched" from "researched, nothing upcoming" - previously both
looked identical as unlock_risk_flag=False.

v2-v4 fixes/features preserved: CoinGecko-based Binance-listing rotation,
daily-aggregated volume confirmation, retry-with-backoff on HTTP 429,
resistance-zone clustering + breakout confirmation, fibonacci extension
continuation signal, EMA50-on-4h trend filter, insufficient-history flag,
break-and-retest tracking, signal logging for scripts/evaluate_signals.py.
"""
import json
import os
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
    "relative_strength_bonus": 15,        # v14: multi-week outperformance vs BTC - catches sustained strength even when signal_quality is beta_driven_or_cluster (the NEAR gap)
    "deep_drawdown_penalty": -10,
    "unlock_risk_penalty": -10,
}
ATR_PERIOD = 14
LIQUIDITY_STOP_BUFFER_ATR_MULT = 0.5     # push the stop this many ATRs past the obvious level
LIQUIDITY_STOP_MIN_BUFFER_PCT = 0.3      # ...or at least this % of price, whichever is bigger (for low-volatility coins where 0.5*ATR would be tiny)
OKX_API_BASE = "https://www.okx.com/api/v5/public"  # v13: switched from Bybit (403 Forbidden from GitHub Actions IPs) - third attempt after Binance (451) and Bybit (403)
FUNDING_REVERSAL_LOOKBACK = 6                # how many recent 8h funding readings to check for a sign flip
OI_BASELINE_PATH = DATA_DIR / "oi-baseline.json"
BTC_VOLATILITY_BASELINE_PATH = DATA_DIR / "btc-volatility-baseline.json"
BTC_VOLATILITY_BASELINE_WINDOW = 48   # rolling average over this many runs (~24h at 30min cadence)
REGIME_BREADTH_RISK_ON = 55
REGIME_BREADTH_RISK_OFF = 45
REGIME_BTC_TREND_PCT = 1.0    # BTC move over the fetched OHLC window big enough to call a trend

# --- v14: Trend-Following Entry mode + Relative Strength Rating ------------
# Rationale (from the 2026-09-21 review): the Entry Quality Gate's distance-
# to-nearest-support R:R systematically penalizes the STRONGEST, most
# persistently-trending coins (NEAR, AVAX) - they haven't pulled back, so
# their nearest support is far away, which reads as "bad R:R" even though
# the underlying trend is exactly what we'd want to be in. This isn't a
# logic bug, it's a blind spot: the gate was designed to catch chasing an
# extended move, not to distinguish that from genuine sustained strength.
SCORE_STREAK_PATH = DATA_DIR / "score-streak.json"
TREND_FOLLOWING_MIN_SCORE = 38          # same tier already used elsewhere for "worth a look"
TREND_FOLLOWING_MIN_STREAK = 3          # consecutive runs scoring >= the threshold, no pullback in between
TREND_FOLLOWING_ATR_STOP_MULT = 2.0     # stop = current price - 2x ATR, NOT distance-to-support - this is the actual fix
TREND_FOLLOWING_TARGET_ATR_MULTS = [3.0, 5.0, 8.0]  # targets as ATR multiples from entry, matching the wider risk unit

# Relative Strength: multi-week outperformance vs BTC, distinct from
# btc_correlation_7d (which measures CO-MOVEMENT direction, not who's
# winning). A coin can be highly correlated with BTC's direction (tagged
# beta_driven_or_cluster) while still meaningfully OUTPERFORMING it over
# weeks - that's real relative strength, not beta, and NEAR showed exactly
# this pattern for weeks before this was built.
RS_STRONG_OUTPERFORM_PCT = 15.0         # coin beat BTC by at least this many percentage points over the window to earn the bonus
POLITE_DELAY = 10   # v38 fix (24/9/2026): raised from 7 - live log evidence (attached to the 24/9 run) showed
                     # EVERY candidate's CoinGecko call hitting 429 across all 3 retry attempts, never once
                     # succeeding even after a 60s backoff wait - consistent with GitHub Actions' shared runner
                     # IPs now being rate-limited more aggressively by CoinGecko's free tier (the same pattern
                     # already seen with Binance/Bybit's outright IP blocks earlier in this project). A slightly
                     # longer gap between OUR OWN calls reduces how often we trigger it in the first place.
MAX_RETRIES = 1      # v38 fix: was 3. Since retrying was observed to NEVER succeed in that log (every candidate
                     # burned all 3 attempts = ~105s for nothing), more retries were pure wasted time, not a
                     # real chance at real data. One quick retry still catches genuinely transient blips; giving
                     # up faster after that lets the run move on and accept "unavailable" for that field this
                     # run (already handled honestly via data_quality from v29) rather than stall the whole
                     # pipeline chasing a call that log evidence shows won't succeed anyway.
RETRY_BACKOFF_BASE = 15
# v9: raised from 10 - each extra candidate costs ~2-3 CoinGecko calls (+1
# Binance pair for fired signals only), so at POLITE_DELAY=7s the run grows
# from ~2-3min to ~4-5min at 16. Kept below 20 to leave headroom under
# CoinGecko's free-tier rate limit rather than pushing it to the edge.
MAX_CANDIDATES_PER_RUN = 16

# v37 fix (24/9/2026): the REAL root cause of runs reaching 45+ minutes,
# found by measuring rather than guessing a timeout value. priority_review
# escalation (v15) was purely ADDITIVE on top of the 16-slot rotation with
# NO cap at all - documented cost is ~3 real API calls x POLITE_DELAY(7s)
# per candidate (~21s each). At 115 priority_review coins in one real run
# (16 + 115) x 21s ~= 46 minutes, matching the observed slowdown exactly.
# v36 tightened double_bottom's over-firing, but ANY early-signal detector
# firing broadly on a volatile day (squeeze alone hit 36-40 coins) can
# reproduce this with no ceiling. This caps the COMBINED total instead of
# hoping no detector ever over-fires again - when escalations exceed the
# remaining budget, the freshest (per opportunity_lifecycle's decay_state,
# built in v33) are kept and the rest simply wait for a later run rather
# than all cramming into this one.
MAX_TOTAL_CANDIDATES_PER_RUN = 40
DECAY_PRIORITY_ORDER = {"fresh": 0, "developing": 1, "unknown": 2, "decayed": 3}


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
    """v15 fix (22/9/2026 audit): the rotation alone left a real detection-
    latency gap - a coin only got its deep confidence/OI/trend-following
    fields recomputed roughly once every 2-3 runs (confirmed live: ZAMA had
    full v14 fields one run, none the next). Any coin scan.py flagged
    priority_review this run (a Layer-2 early signal fired) now gets a deep
    check THIS run too, on top of the normal rotation slot - it doesn't
    consume or shift the rotation offset, it's purely additive."""
    if not all_listed:
        return []
    ordered = sorted(all_listed, key=lambda c: c["id"])
    n = len(ordered)
    offset = load_rotation_offset() % n
    rotation_slice = [ordered[(offset + i) % n] for i in range(min(MAX_CANDIDATES_PER_RUN, n))]
    save_rotation_offset((offset + len(rotation_slice)) % n)

    rotation_ids = {c["id"] for c in rotation_slice}
    escalated = [c for c in ordered if c.get("priority_review") and c["id"] not in rotation_ids]

    budget = max(0, MAX_TOTAL_CANDIDATES_PER_RUN - len(rotation_slice))
    if len(escalated) > budget:
        def escalation_priority(c):
            decay = (c.get("opportunity_lifecycle") or {}).get("decay_state", "unknown")
            n_signals = sum(1 for v in (c.get("early_signals") or {}).values() if v)
            return (DECAY_PRIORITY_ORDER.get(decay, 2), -n_signals)
        escalated = sorted(escalated, key=escalation_priority)[:budget]

    return rotation_slice + escalated


# v39 fix (24/9/2026): see scan.py's identical comment - keyless CoinGecko
# calls are rate-limited per shared IP (confirmed in CoinGecko's own docs),
# which is what the 24/9 run's 429 storm was. Optional, falls back to
# keyless if the secret isn't configured yet.
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY")


def fetch_json(url: str):
    headers = {"User-Agent": "investment-radar/1.0"}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY
    req = urllib.request.Request(url, headers=headers)
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


def load_btc_volatility_baseline() -> dict:
    if not BTC_VOLATILITY_BASELINE_PATH.exists():
        return {"recent_readings": []}
    try:
        return json.loads(BTC_VOLATILITY_BASELINE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"recent_readings": []}


def save_btc_volatility_baseline(baseline: dict) -> None:
    BTC_VOLATILITY_BASELINE_PATH.write_text(json.dumps(baseline, ensure_ascii=False), encoding="utf-8")


def compute_market_regime(btc_closes: list, breadth_pct_green, baseline: dict) -> dict:
    """v32 (24/9/2026): Market Regime Classification - step 4 of the
    V2-merge plan. Reuses btc_closes already fetched for the correlation
    check (zero extra API cost) - no new data source, just a new read of
    data we already pay for every run. Two independent axes, matching V2's
    own framing that regime is CONTEXT for a candidate, never a standalone
    trade signal by itself:
      risk_state: combines BTC's own recent trend with market breadth -
        deliberately requires BOTH to agree before calling it risk_on/off,
        rather than either alone (a lone-BTC-pump with flat breadth, or
        broad breadth with BTC itself flat, is a genuinely ambiguous state
        and stays "neutral" rather than forcing a label).
      volatility_state: BTC's own recent volatility vs its OWN rolling
        history (not an arbitrary fixed threshold) - so "high volatility"
        means high FOR BTC LATELY, not high by some hardcoded number that
        stops making sense across different market eras.
    """
    if not btc_closes or len(btc_closes) < 3:
        return {"risk_state": "unavailable", "volatility_state": "unavailable",
                "btc_trend_recent_pct": None, "breadth_pct_green": breadth_pct_green}

    recent_window = min(12, len(btc_closes))
    recent = btc_closes[-recent_window:]
    btc_trend_recent_pct = round((recent[-1] - recent[0]) / recent[0] * 100, 2) if recent[0] else None
    btc_trend_full_pct = round((btc_closes[-1] - btc_closes[0]) / btc_closes[0] * 100, 2) if btc_closes[0] else None

    pct_changes = [
        (recent[i] - recent[i - 1]) / recent[i - 1] * 100
        for i in range(1, len(recent)) if recent[i - 1]
    ]
    current_vol = (sum((c - sum(pct_changes) / len(pct_changes)) ** 2 for c in pct_changes) / len(pct_changes)) ** 0.5 if pct_changes else None

    readings = baseline.get("recent_readings", [])
    baseline_avg = sum(readings) / len(readings) if readings else None
    if current_vol is not None:
        readings.append(current_vol)
        baseline["recent_readings"] = readings[-BTC_VOLATILITY_BASELINE_WINDOW:]

    if baseline_avg is None or current_vol is None:
        volatility_state = "baseline_building"   # not enough history yet - honest, not a guess
    elif current_vol < 0.7 * baseline_avg:
        volatility_state = "compressed"
    elif current_vol > 1.3 * baseline_avg:
        volatility_state = "expanded"
    else:
        volatility_state = "normal"

    risk_state = "neutral"
    if btc_trend_recent_pct is not None and breadth_pct_green is not None:
        if btc_trend_recent_pct >= REGIME_BTC_TREND_PCT and breadth_pct_green >= REGIME_BREADTH_RISK_ON:
            risk_state = "risk_on"
        elif btc_trend_recent_pct <= -REGIME_BTC_TREND_PCT and breadth_pct_green <= REGIME_BREADTH_RISK_OFF:
            risk_state = "risk_off"

    return {
        "risk_state": risk_state,
        "volatility_state": volatility_state,
        "btc_trend_recent_pct": btc_trend_recent_pct,
        "btc_trend_full_window_pct": btc_trend_full_pct,
        "btc_volatility_now": round(current_vol, 3) if current_vol is not None else None,
        "btc_volatility_baseline_avg": round(baseline_avg, 3) if baseline_avg is not None else None,
        "breadth_pct_green": breadth_pct_green,
    }


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
    """v15: also returns whether this coin_id has EVER been manually
    researched into known-unlocks.json at all - the 22/9 audit found
    known-unlocks.json had exactly one entry (optimism) covering dozens of
    traded coins, so unlock_risk_flag=False was silently reading as
    "confirmed no unlock" everywhere else when it actually meant "never
    checked". unlock_data_checked=False now makes that gap visible in the
    output instead of hiding it."""
    data_checked = coin_id in unlocks_config and coin_id != "_readme"
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
        return False, None, None, data_checked
    upcoming.sort(key=lambda t: t[0])
    _, nearest = upcoming[0]
    return True, nearest["date"], nearest.get("pct_of_supply"), data_checked


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
            if candles[idx][4] > proj * (1 + BREAKOUT_BUFFER_PCT / 100):
                held += 1
                consecutive_below = 0
            else:
                consecutive_below += 1

        entry["candles_held"] = held
        result["trendline_break_detected"] = True
        result["trendline_candles_held"] = held
        detected_at = datetime.fromisoformat(entry["detected_at"])
        age_days = (datetime.now(timezone.utc) - detected_at).total_seconds() / 86400

        if age_days > TRENDLINE_MAX_AGE_DAYS or consecutive_below >= TRENDLINE_INVALIDATION_CONSECUTIVE:
            entry["status"] = "invalidated"
        elif held >= TRENDLINE_CONFIRMATION_CANDLES:
            entry["status"] = "confirmed"
            result["trendline_break_confirmed_signal"] = True
        return result

    fit = fit_descending_trendline(candles)
    if not fit:
        return result
    slope, intercept, n_points = fit
    projected_now = slope * last_index + intercept
    result["trendline_value_now"] = round(projected_now, 8)
    if last_close > projected_now * (1 + BREAKOUT_BUFFER_PCT / 100):
        watchlist[coin_id] = {
            "symbol": coin["symbol"], "slope": slope, "intercept": intercept,
            "n_swing_points": n_points, "detected_at": datetime.now(timezone.utc).isoformat(),
            "detected_at_index": last_index, "candles_held": 1, "status": "watching",
        }
        result["trendline_break_detected"] = True
        result["trendline_candles_held"] = 1
    return result


# ---------- v8: ATR-based dynamic stop ----------

# --- v42: Fair Value Gap detector - step 10 of the V2-merge plan ----------
# Runs ONLY on real_candles (v41, top funnel tiers) - genuine OHLC, not the
# synthetic snapshots scan.py's early signals use. Bullish FVG per the
# standard ICT/SMC definition: candle[i-2].high < candle[i].low, i.e. a gap
# the middle (displacement) candle punched through that price never traded
# back into. Quality is graded, never a bare true/false, because a tiny
# gap from ordinary noise and a large gap from a genuine impulsive move are
# not the same evidence even though both technically satisfy the geometry.
FVG_QUALITY_GAP_ATR_STRONG = 0.5     # gap size >= this fraction of ATR to call it "strong" evidence
FVG_QUALITY_GAP_ATR_MODERATE = 0.25
FVG_QUALITY_BODY_RATIO_STRONG = 0.7  # displacement candle's body/range ratio - a decisive candle, not an indecisive one


def detect_fair_value_gaps(candles: list, atr_value) -> list:
    if not candles or len(candles) < 3 or not atr_value:
        return []
    gaps = []
    current_price = candles[-1][4]
    for i in range(2, len(candles)):
        c1, c2, c3 = candles[i - 2], candles[i - 1], candles[i]
        gap_low, gap_high = c1[2], c3[3]   # c1 high, c3 low
        if gap_high <= gap_low:
            continue  # no gap - the standard geometry isn't satisfied here
        gap_size = gap_high - gap_low
        gap_size_pct_atr = gap_size / atr_value
        c2_range = c2[2] - c2[3]
        body_ratio = abs(c2[4] - c2[1]) / c2_range if c2_range else 0
        # mitigation: has any LATER candle traded back down into [gap_low, gap_high]?
        later_candles = candles[i + 1:]
        mitigated = any(c[3] <= gap_high for c in later_candles)  # a later low reaching back into the gap
        fresh = not mitigated and current_price > gap_high  # price moved on without revisiting

        if gap_size_pct_atr >= FVG_QUALITY_GAP_ATR_STRONG and body_ratio >= FVG_QUALITY_BODY_RATIO_STRONG and fresh:
            quality = "exceptional"
        elif gap_size_pct_atr >= FVG_QUALITY_GAP_ATR_STRONG and fresh:
            quality = "strong"
        elif gap_size_pct_atr >= FVG_QUALITY_GAP_ATR_MODERATE:
            quality = "moderate"
        else:
            quality = "weak"

        gaps.append({
            "gap_low": round(gap_low, 8), "gap_high": round(gap_high, 8),
            "gap_size_pct_of_atr": round(gap_size_pct_atr * 100, 1),
            "displacement_body_ratio": round(body_ratio, 2),
            "mitigated": mitigated, "fresh": fresh, "quality": quality,
            "candle_index": i,
        })
    # most useful to a consumer: the freshest, highest-quality gaps first
    quality_rank = {"exceptional": 0, "strong": 1, "moderate": 2, "weak": 3}
    gaps.sort(key=lambda g: (g["mitigated"], quality_rank[g["quality"]]))
    return gaps[:5]  # cap - only the handful most relevant, not every gap in 30 days of candles


# --- v43: Liquidity Sweep detector - step 11 of the V2-merge plan --------
# Confidence tiers instead of a boolean, per the 24/9 design decision: a
# genuine institutional-style sweep (real pool + real displacement after)
# is categorically different evidence from a lone wick that happened to dip
# and close back up. Never labeled "confirmed smart money sweep" outright -
# "detected" is the honest floor when the geometry is there but the pool
# or the follow-through can't be verified as real.
LIQUIDITY_SWEEP_LOOKBACK = 20
LIQUIDITY_SWEEP_POOL_TOLERANCE_PCT = 1.0   # lows within this % of each other count as the same liquidity pool
LIQUIDITY_SWEEP_DISPLACEMENT_ATR_MULT = 0.5


def find_swing_lows(candles: list, neighbors: int = 2) -> list:
    lows = []
    for i in range(neighbors, len(candles) - neighbors):
        window = [candles[j][3] for j in range(i - neighbors, i + neighbors + 1)]
        if candles[i][3] == min(window):
            lows.append((i, candles[i][3]))
    return lows


def detect_liquidity_sweep(candles: list, atr_value):
    if not candles or len(candles) < 10 or not atr_value:
        return None
    recent = candles[-LIQUIDITY_SWEEP_LOOKBACK:]
    swing_lows = find_swing_lows(recent)
    pool_candidates = [(i, p) for i, p in swing_lows if i < len(recent) - 3]  # leave room for a sweep to happen after
    if not pool_candidates:
        return None

    ref_idx, ref_price = pool_candidates[-1]
    equal_lows = [p for i, p in pool_candidates if abs(p - ref_price) / ref_price * 100 <= LIQUIDITY_SWEEP_POOL_TOLERANCE_PCT]
    pool_level = min(equal_lows)
    is_genuine_pool = len(equal_lows) >= 2   # a real cluster of lows, not a single isolated swing point

    sweep_idx = None
    for j in range(ref_idx + 1, len(recent)):
        if recent[j][3] < pool_level and recent[j][4] > pool_level:  # wicked below, closed back above
            sweep_idx = j
            break
    if sweep_idx is None:
        return None

    displacement_confirmed = False
    for k in range(sweep_idx, min(sweep_idx + 3, len(recent))):
        c = recent[k]
        candle_range = c[2] - c[3]
        body = c[4] - c[1]
        if candle_range and body > 0 and candle_range >= LIQUIDITY_SWEEP_DISPLACEMENT_ATR_MULT * atr_value:
            displacement_confirmed = True
            break

    if is_genuine_pool and displacement_confirmed:
        confidence = "high_quality"
    elif displacement_confirmed:
        confidence = "confirmed"
    else:
        confidence = "detected"

    return {
        "pool_level": round(pool_level, 8),
        "is_genuine_pool": is_genuine_pool,
        "n_equal_lows": len(equal_lows),
        "displacement_confirmed": displacement_confirmed,
        "confidence": confidence,
    }


# --- v44: Inducement detector - step 12 of the V2-merge plan --------------
# Deliberately stricter than detect_liquidity_sweep above: V2's own spec
# (section 23) is explicit that inducement is NOT just "price moved before
# the real move" - it requires a minor structure (L1) that plausibly drew
# traders in, followed by a DEEPER sweep (L2 < L1) that took out both L1's
# and the induced positions' liquidity, THEN real displacement. Sequential
# two-low structure is what distinguishes this from a single-pool sweep.
INDUCEMENT_LOOKBACK = 25
INDUCEMENT_DISPLACEMENT_ATR_MULT = 0.5
INDUCEMENT_BOUNCE_MIN_PCT = 0.5   # L1 must be followed by at least this much of a bounce to call it plausible bait, not just noise on the way down


def detect_inducement(candles: list, atr_value):
    if not candles or len(candles) < 15 or not atr_value:
        return None
    recent = candles[-INDUCEMENT_LOOKBACK:]
    swing_lows = find_swing_lows(recent)
    if len(swing_lows) < 2:
        return None

    best = None
    for i in range(len(swing_lows) - 1):
        l1_idx, l1_price = swing_lows[i]
        for j in range(i + 1, len(swing_lows)):
            l2_idx, l2_price = swing_lows[j]
            if l2_price >= l1_price:
                continue  # L2 must sweep DEEPER than L1 to count as taking out its liquidity too
            between = recent[l1_idx + 1:l2_idx]
            if not between:
                continue
            bounce_high = max(c[2] for c in between)
            genuine_structure = l1_price and bounce_high >= l1_price * (1 + INDUCEMENT_BOUNCE_MIN_PCT / 100)

            after_l2 = recent[l2_idx + 1:]
            reversed_above_l1 = any(c[4] > l1_price for c in after_l2)
            if not reversed_above_l1:
                continue

            displacement_confirmed = False
            for k in range(l2_idx, min(l2_idx + 4, len(recent))):
                c = recent[k]
                candle_range = c[2] - c[3]
                body = c[4] - c[1]
                if candle_range and body > 0 and candle_range >= INDUCEMENT_DISPLACEMENT_ATR_MULT * atr_value:
                    displacement_confirmed = True
                    break

            if genuine_structure and displacement_confirmed:
                confidence = "high_quality"
            elif displacement_confirmed:
                confidence = "confirmed"
            else:
                confidence = "detected"

            candidate = {
                "induced_level": round(l1_price, 8), "swept_level": round(l2_price, 8),
                "genuine_structure": genuine_structure, "displacement_confirmed": displacement_confirmed,
                "confidence": confidence, "swept_at_index": l2_idx,
            }
            if best is None or (l2_idx, l1_idx) > (best["swept_at_index"], best["induced_at_index"]):
                candidate["induced_at_index"] = l1_idx
                best = candidate
    if best is not None:
        best.pop("induced_at_index", None)
    return best


# --- v45: Order Block detector - step 13 of the V2-merge plan ------------
# V2's own warning (section 28) is explicit: never treat every last opposite
# candle as an order block. This only qualifies a candle once it's followed
# by a genuinely strong bullish displacement (decisive body, ATR-relative
# size) - origin and displacement are checked, not just "last red candle".
ORDER_BLOCK_DISPLACEMENT_ATR_MULT = 0.6
ORDER_BLOCK_BODY_RATIO_MIN = 0.6


def detect_order_block(candles: list, atr_value):
    if not candles or len(candles) < 6 or not atr_value:
        return None
    best = None
    for i in range(1, len(candles)):
        c = candles[i]
        candle_range = c[2] - c[3]
        body = c[4] - c[1]
        is_strong_bullish = (
            body > 0 and candle_range > 0
            and body / candle_range >= ORDER_BLOCK_BODY_RATIO_MIN
            and candle_range >= ORDER_BLOCK_DISPLACEMENT_ATR_MULT * atr_value
        )
        if not is_strong_bullish:
            continue

        ob_idx = None
        for j in range(i - 1, -1, -1):
            if candles[j][4] < candles[j][1]:  # last BEARISH candle right before the displacement
                ob_idx = j
                break
        if ob_idx is None:
            continue

        ob_candle = candles[ob_idx]
        ob_low, ob_high = ob_candle[3], ob_candle[2]
        after = candles[i + 1:]
        mitigated = any(c2[3] <= ob_high for c2 in after)  # price has traded back down into the zone since
        displacement_strength_atr = round(candle_range / atr_value, 2)

        if mitigated:
            quality = "weak"     # already retested - the zone's predictive value going forward is much lower
        elif displacement_strength_atr >= 1.0:
            quality = "strong"
        else:
            quality = "moderate"

        candidate = {
            "ob_low": round(ob_low, 8), "ob_high": round(ob_high, 8),
            "displacement_strength_atr": displacement_strength_atr,
            "mitigated": mitigated, "fresh": not mitigated, "quality": quality,
            "formed_at_index": ob_idx,
        }
        if best is None or ob_idx > best["formed_at_index"]:
            best = candidate
    return best


def compute_atr(candles: list, period: int = ATR_PERIOD):
    """Average True Range over the last `period` candles - a per-asset
    volatility measure, so a naturally volatile coin gets a proportionally
    wider stop than a calm one, instead of everyone getting the same fixed
    percentage (the exact gap identified manually across the ARB/ZAMA/AVAX
    reviews). Returns (atr_value, atr_pct_of_price) or (None, None) if
    there isn't enough history."""
    if len(candles) < period + 1:
        return None, None
    true_ranges = []
    for i in range(1, len(candles)):
        high, low = candles[i][2], candles[i][3]
        prev_close = candles[i - 1][4]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    recent_tr = true_ranges[-period:]
    atr = mean(recent_tr)
    last_close = candles[-1][4]
    atr_pct = (atr / last_close * 100) if last_close else None
    return round(atr, 8), round(atr_pct, 3) if atr_pct is not None else None


def compute_liquidity_buffered_stop(reference_level: float, atr_value, direction: str = "long"):
    """v8.1 (defensive application of the 'everyone's stop sits at the
    obvious level' idea, per Azez's 2026-09-20 discussion): rather than
    placing a stop exactly AT a support/trendline/swing level - where the
    crowd's stops are also clustered and get hunted first - this pushes it
    a bit further away by whichever is bigger: half an ATR, or a small
    minimum percentage (the latter matters for low-volatility coins where
    0.5*ATR would round to almost nothing). This only changes WHERE the
    stop sits relative to a level we already compute (resistance_level,
    trendline_value_now, etc.) - it doesn't invent a new entry signal,
    which is deliberately deferred until sweep detection can be backtested
    (see the offensive-application discussion - not implemented yet)."""
    if reference_level is None or atr_value is None:
        return None
    pct_buffer = reference_level * (LIQUIDITY_STOP_MIN_BUFFER_PCT / 100)
    atr_buffer = atr_value * LIQUIDITY_STOP_BUFFER_ATR_MULT
    buffer = max(pct_buffer, atr_buffer)
    if direction == "long":
        return round(reference_level - buffer, 8)
    return round(reference_level + buffer, 8)


# ---------- v8: Binance derivatives (funding rate + open interest) ----------

def to_exchange_symbol(symbol: str) -> str:
    """v13: OKX's instrument-ID format for USDT-margined perpetual swaps:
    {BASE}-USDT-SWAP - different shape from Binance/Bybit's concatenated
    {BASE}USDT, so this format is now OKX-specific rather than shared."""
    return f"{symbol.upper()}-USDT-SWAP"


def fetch_funding_rate_history(symbol: str, limit: int = FUNDING_REVERSAL_LOOKBACK):
    """v13: switched from Bybit to OKX's public v5 API - Bybit's public
    market-data endpoints returned HTTP 403 Forbidden for GitHub Actions'
    IP ranges (confirmed via the v12 diagnostic logging: every single fetch
    failed with 403, following the same pattern as Binance's 451 before
    it). OKX is the third exchange tried. Returns a list of recent funding
    rates in chronological order (oldest first, most recent last, matching
    what detect_funding_reversal expects), or None if this instrument
    doesn't exist on OKX or the request otherwise fails."""
    params = {"instId": to_exchange_symbol(symbol), "limit": limit}
    url = f"{OKX_API_BASE}/funding-rate-history?{urllib.parse.urlencode(params)}"
    try:
        data = fetch_json(url)
        rows = data.get("data") or []
        if not rows:
            return None
        # OKX returns most-recent-first; reverse to oldest-first for our convention
        rows = list(reversed(rows))
        return [float(r["fundingRate"]) for r in rows]
    except Exception as exc:  # noqa: BLE001
        print(f"  [diagnostic] OKX funding-rate fetch failed for {symbol}: {type(exc).__name__}: {exc}")
        return None


def fetch_open_interest_now(symbol: str):
    """v13: OKX equivalent of the open-interest call - see
    fetch_funding_rate_history's docstring for why the switch happened."""
    params = {"instId": to_exchange_symbol(symbol)}
    url = f"{OKX_API_BASE}/open-interest?{urllib.parse.urlencode(params)}"
    try:
        data = fetch_json(url)
        rows = data.get("data") or []
        if not rows:
            return None
        return float(rows[0]["oi"])
    except Exception as exc:  # noqa: BLE001
        print(f"  [diagnostic] OKX open-interest fetch failed for {symbol}: {type(exc).__name__}: {exc}")
        return None


def detect_funding_reversal(rates: list):
    """v8: a sign flip in recent funding (negative -> positive or vice
    versa) often marks a crowded side getting squeezed/unwound - flagged
    as a TIMING note, not a hard gate (see the funding-vs-OI design
    discussion: these two answer related-but-different questions about
    positioning, so both are kept as separate, moderate-weight inputs)."""
    if not rates or len(rates) < 2:
        return False, None
    signs = [1 if r > 0 else (-1 if r < 0 else 0) for r in rates if r != 0]
    if len(signs) < 2:
        return False, None
    reversed_ = signs[-1] != signs[0] and signs[-1] != 0
    return reversed_, rates[-1]


def assess_oi_price_relationship(price_change_24h_pct, oi_now, oi_baseline):
    """v8: the exact check that was missing when we manually diagnosed ARB's
    27% rally as partly short-covering rather than pure new demand.
    price up + OI up = new money entering (healthier). price up + OI flat/
    down = existing shorts closing (less durable). Needs a stored baseline
    OI from a prior run to compare against - returns None (not "unhealthy")
    until we have two data points, since one snapshot alone can't divergence-check."""
    if oi_now is None or oi_baseline is None or price_change_24h_pct is None:
        return None
    oi_change_pct = (oi_now - oi_baseline) / oi_baseline * 100 if oi_baseline else None
    if oi_change_pct is None:
        return None
    if price_change_24h_pct > 1 and oi_change_pct > 1:
        return "confirms"       # price and OI both rising - new demand
    if price_change_24h_pct > 1 and oi_change_pct < -1:
        return "diverges"       # price up, OI down - short covering, not new demand
    return "neutral"


def load_oi_baseline() -> dict:
    if not OI_BASELINE_PATH.exists():
        return {}
    try:
        return json.loads(OI_BASELINE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_oi_baseline(baseline: dict) -> None:
    OI_BASELINE_PATH.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- v14: Trend-Following Entry (score streak + ATR stop) ----------

def load_score_streak() -> dict:
    if not SCORE_STREAK_PATH.exists():
        return {}
    try:
        return json.loads(SCORE_STREAK_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_score_streak(streaks: dict) -> None:
    SCORE_STREAK_PATH.write_text(json.dumps(streaks, ensure_ascii=False, indent=2), encoding="utf-8")


def update_score_streak(streaks: dict, coin_id: str, score) -> int:
    """v14: increments a per-coin streak of consecutive runs scoring at or
    above TREND_FOLLOWING_MIN_SCORE; any run below the threshold resets it
    to 0. This is what NEAR would have accumulated for weeks - a
    persistence signal the distance-to-support gate structurally can't
    see. Returns the streak count AFTER this update."""
    current = streaks.get(coin_id, 0)
    if score is not None and score >= TREND_FOLLOWING_MIN_SCORE:
        current += 1
    else:
        current = 0
    streaks[coin_id] = current
    return current


def compute_trend_following_stop(current_price, atr_value):
    """v14: the actual fix for the systemic bias - a stop sized from
    volatility (ATR) around the CURRENT price, not from distance to a
    support level that a strongly-trending coin may not have visited in
    weeks. Returns (stop, targets) or (None, None) if inputs are missing."""
    if current_price is None or atr_value is None:
        return None, None
    stop = round(current_price - TREND_FOLLOWING_ATR_STOP_MULT * atr_value, 8)
    risk = current_price - stop
    targets = [round(current_price + risk * mult / TREND_FOLLOWING_ATR_STOP_MULT, 8)
               for mult in TREND_FOLLOWING_TARGET_ATR_MULTS]
    return stop, targets


# ---------- v14: Relative Strength Rating ----------

def compute_relative_strength(coin_closes: list, btc_closes: list):
    """v14: multi-week OUTPERFORMANCE vs BTC, as a percentage-point spread
    of total return over the same window - different from
    btc_correlation_7d, which measures whether the two move together, not
    who's winning. A coin can be highly correlated (tagged
    beta_driven_or_cluster) while still meaningfully outperforming BTC over
    weeks - that's real relative strength being missed by correlation
    alone. Uses whatever overlapping window both series share (the full
    OHLC fetch, ~30 days) rather than the shorter 7-day correlation window,
    since this is deliberately a slower, more weeks-scale read."""
    if not coin_closes or not btc_closes:
        return None
    n = min(len(coin_closes), len(btc_closes))
    if n < CORRELATION_MIN_OVERLAP:
        return None
    coin_tail, btc_tail = coin_closes[-n:], btc_closes[-n:]
    if coin_tail[0] == 0 or btc_tail[0] == 0:
        return None
    coin_return_pct = (coin_tail[-1] / coin_tail[0] - 1) * 100
    btc_return_pct = (btc_tail[-1] / btc_tail[0] - 1) * 100
    return round(coin_return_pct - btc_return_pct, 2)


# ---------- v8: composite confidence score ----------

def load_indicator_weights() -> dict:
    if not INDICATOR_WEIGHTS_PATH.exists():
        return dict(DEFAULT_INDICATOR_WEIGHTS)
    try:
        loaded = json.loads(INDICATOR_WEIGHTS_PATH.read_text(encoding="utf-8"))
        merged = dict(DEFAULT_INDICATOR_WEIGHTS)
        merged.update(loaded)  # a partial file only overrides the keys it names
        return merged
    except json.JSONDecodeError:
        return dict(DEFAULT_INDICATOR_WEIGHTS)


# v30 (24/9/2026): Evidence Clusters - step 2 of the V2-merge plan. Groups
# the existing scoring components into named clusters (Structure/Volume/
# Derivatives/Context) with their own 0-100 sub-score, plus risk penalties
# kept separate and visible rather than buried as a silent subtraction.
# This does NOT change confidence_score's math at all - it's a read-only
# re-presentation of the exact same breakdown, purely for auditability and
# as the foundation the later archetype/rejection-engine steps build on.
# Cluster membership (max points per DEFAULT_INDICATOR_WEIGHTS):
#   structure:   breakout_or_trendline_signal(15) + trend_aligned(10) = 25
#   volume:      volume_confirmed(10) = 10
#   derivatives: oi_price_confirms(15) + funding_not_crowded(10) = 25
#   context:     idiosyncratic_quality(25) + relative_strength_bonus(15) = 40
# (structure+volume+derivatives+context max = 100, matching the score cap)
EVIDENCE_CLUSTER_MEMBERS = {
    "structure": ["breakout_or_trendline_signal", "trend_aligned"],
    "volume": ["volume_confirmed"],
    "derivatives": ["oi_price_confirms", "funding_not_crowded"],
    "context": ["idiosyncratic_quality", "relative_strength_bonus"],
}
RISK_PENALTY_COMPONENTS = ["deep_drawdown_penalty", "unlock_risk_penalty"]


def compute_evidence_clusters(breakdown: dict, weights: dict) -> dict:
    clusters = {}
    for cluster_name, components in EVIDENCE_CLUSTER_MEMBERS.items():
        max_points = sum(weights.get(c, 0) for c in components)
        earned_points = sum(breakdown.get(c, 0) for c in components)
        clusters[cluster_name] = {
            "score_0_100": round(earned_points / max_points * 100) if max_points else 0,
            "earned_points": earned_points,
            "max_points": max_points,
            "components": {c: breakdown.get(c, 0) for c in components},
        }
    risk_points = sum(breakdown.get(c, 0) for c in RISK_PENALTY_COMPONENTS)
    clusters["risk_penalties"] = {
        "points_deducted": risk_points,  # already negative or zero - not a 0-100 score, a visible deduction
        "components": {c: breakdown.get(c, 0) for c in RISK_PENALTY_COMPONENTS},
        "active_penalties": [c for c in RISK_PENALTY_COMPONENTS if breakdown.get(c, 0) < 0],
    }
    return clusters


def compute_confidence_score(coin: dict, weights: dict) -> dict:
    """v11 (was v8): replaces bare pass/fail with a transparent 0-100 score
    plus the breakdown that produced it (never just the number - the
    breakdown is what makes this auditable instead of a black box). Weights
    are starting priors documented in DEFAULT_INDICATOR_WEIGHTS, meant to be
    recalibrated later from real outcomes in agent-room-log.json, not
    treated as final.

    v11 change: extension_continuation_signal now earns HALF of
    breakout_or_trendline_signal's weight instead of zero - every fired
    signal reviewed so far has been an extension (not a fresh breakout or
    a confirmed trendline break), which meant this component was always
    0 for every real candidate, not because the signal was worthless but
    because full credit was reserved for a fresher signal type. An
    extension is real, weaker evidence - not zero evidence."""
    breakdown = {}
    has_full_signal = bool(coin.get("breakout_signal") or coin.get("trendline_break_confirmed_signal"))
    has_extension_only = bool(coin.get("extension_continuation_signal")) and not has_full_signal
    if has_full_signal:
        breakdown["breakout_or_trendline_signal"] = weights["breakout_or_trendline_signal"]
    elif has_extension_only:
        breakdown["breakout_or_trendline_signal"] = round(weights["breakout_or_trendline_signal"] / 2)
    else:
        breakdown["breakout_or_trendline_signal"] = 0
    breakdown["volume_confirmed"] = weights["volume_confirmed"] if coin.get("volume_confirmed") else 0
    breakdown["trend_aligned"] = weights["trend_aligned"] if coin.get("trend_aligned") else 0
    breakdown["idiosyncratic_quality"] = (
        weights["idiosyncratic_quality"] if coin.get("signal_quality") == "idiosyncratic" else 0
    )
    oi_rel = coin.get("oi_price_relationship")
    breakdown["oi_price_confirms"] = weights["oi_price_confirms"] if oi_rel == "confirms" else 0
    funding_reversed = coin.get("funding_reversal_detected")
    breakdown["funding_not_crowded"] = 0 if funding_reversed else weights["funding_not_crowded"]
    rs = coin.get("relative_strength_pct")
    breakdown["relative_strength_bonus"] = (
        weights["relative_strength_bonus"] if rs is not None and rs >= RS_STRONG_OUTPERFORM_PCT else 0
    )
    breakdown["deep_drawdown_penalty"] = weights["deep_drawdown_penalty"] if coin.get("deep_drawdown_flag") else 0
    breakdown["unlock_risk_penalty"] = weights["unlock_risk_penalty"] if coin.get("unlock_risk_flag") else 0

    score = sum(breakdown.values())
    score = max(0, min(100, score))
    return {"confidence_score": score, "confidence_breakdown": breakdown}


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


def find_all_resistance_levels(candles: list) -> list:
    """v34 (24/9/2026): Path-to-Target Analysis - step 6 of the V2-merge
    plan. find_resistance_zone() above only keeps the SINGLE best cluster
    and discards every other valid one - fine for picking one stop
    reference, but Path-to-Target needs to know about EVERY known level
    between here and a target, not just the strongest one. Duplicates the
    same peak/cluster logic deliberately (kept as a separate read-only
    function) rather than refactoring find_resistance_zone itself, so the
    existing stop-sizing behavior is provably unchanged."""
    if len(candles) < (PEAK_NEIGHBORS * 2 + MIN_TOUCHES + EXCLUDE_RECENT_CANDLES):
        return []
    history = candles[:-EXCLUDE_RECENT_CANDLES]
    highs = [c[2] for c in history]
    peaks = []
    for i in range(PEAK_NEIGHBORS, len(highs) - PEAK_NEIGHBORS):
        window = highs[i - PEAK_NEIGHBORS: i + PEAK_NEIGHBORS + 1]
        if highs[i] == max(window):
            peaks.append(highs[i])
    if len(peaks) < MIN_TOUCHES:
        return []
    peaks.sort()
    clusters, current_cluster = [], [peaks[0]]
    for p in peaks[1:]:
        if (p - current_cluster[-1]) / current_cluster[-1] * 100 <= TOUCH_TOLERANCE_PCT:
            current_cluster.append(p)
        else:
            clusters.append(current_cluster)
            current_cluster = [p]
    clusters.append(current_cluster)
    valid = [c for c in clusters if len(c) >= MIN_TOUCHES]
    return sorted(round(mean(c), 6) for c in valid)


def compute_path_to_target(current_price, targets: list, resistance_levels: list) -> dict:
    """For each target, counts how many already-known resistance levels sit
    strictly between current price and that target - the target being
    theoretically reachable (per ATR/risk-reward math) says nothing about
    what has to be cleared first to actually get there. path_quality per
    target: clear (0 obstacles) / moderate (1) / crowded (2+)."""
    if not targets or current_price is None:
        return {"targets": []}
    results = []
    for t in targets:
        lo, hi = (current_price, t) if t > current_price else (t, current_price)
        obstacles = [lvl for lvl in resistance_levels if lo < lvl < hi]
        if len(obstacles) == 0:
            quality = "clear"
        elif len(obstacles) == 1:
            quality = "moderate"
        else:
            quality = "crowded"
        results.append({
            "target_price": t,
            "obstacles_in_path": obstacles,
            "path_quality": quality,
        })
    return {"targets": results}


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
        "unlock_data_checked": coin.get("unlock_data_checked"),
        "trendline_break_detected": coin.get("trendline_break_detected"),
        "trendline_break_confirmed_signal": coin.get("trendline_break_confirmed_signal"),
        "trendline_candles_held": coin.get("trendline_candles_held"),
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
        entry["confidence_score"] = coin_ref.get("confidence_score")
        entry["confidence_breakdown"] = coin_ref.get("confidence_breakdown")
        entry["atr_pct_of_price"] = coin_ref.get("atr_pct_of_price")
        entry["oi_price_relationship"] = coin_ref.get("oi_price_relationship")
        entry["funding_reversal_detected"] = coin_ref.get("funding_reversal_detected")
        entry["suggested_stop_resistance_based"] = coin_ref.get("suggested_stop_resistance_based")
        entry["suggested_stop_trendline_based"] = coin_ref.get("suggested_stop_trendline_based")
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
    trendline_watchlist = load_trendline_watchlist()
    oi_baseline = load_oi_baseline()
    indicator_weights = load_indicator_weights()
    score_streak = load_score_streak()
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
            coin["_candles_raw"] = candles  # v41: transient - promoted to real_candles for top funnel tiers only, deleted for everyone else before saving

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
        # v7: descending-trendline break detection (independent of the horizontal breakout logic above)
        if candles:
            tl_result = check_trendline_break(trendline_watchlist, coin, candles)
            coin.update(tl_result)

        unlock_flag, unlock_date, unlock_pct, unlock_data_checked = check_unlock_risk(coin_id, unlocks_config, now)
        coin["unlock_risk_flag"] = unlock_flag
        coin["next_known_unlock_date"] = unlock_date
        coin["next_known_unlock_pct_supply"] = unlock_pct
        coin["unlock_data_checked"] = unlock_data_checked

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
        coin["all_resistance_levels"] = find_all_resistance_levels(candles)
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

        # v8: ATR-based volatility/stop sizing - free, uses candles already in memory
        atr_value, atr_pct = compute_atr(candles)
        coin["atr_value"] = atr_value
        coin["atr_pct_of_price"] = atr_pct

        # v14: relative strength vs BTC over the full fetched window - reuses
        # candles and btc_closes already in memory, no extra API cost.
        if candles and btc_closes and coin_id != BTC_COIN_ID:
            coin["relative_strength_pct"] = compute_relative_strength(
                [c[4] for c in candles], btc_closes
            )
        else:
            coin["relative_strength_pct"] = None

        # v8.1: defensive liquidity-buffer stop - pushes the stop past the
        # obvious level (resistance turned support, or the trendline) instead
        # of sitting exactly on it where the crowd's stops also cluster.
        # Computed against BOTH reference levels when available since a
        # trendline break and a horizontal breakout can suggest different
        # stops - the coin then carries both, not a single forced choice.
        coin["suggested_stop_resistance_based"] = compute_liquidity_buffered_stop(
            coin.get("resistance_level"), atr_value, "long"
        )
        coin["suggested_stop_trendline_based"] = compute_liquidity_buffered_stop(
            coin.get("trendline_value_now"), atr_value, "long"
        )
        # v22 fix (found via consistency_check.py, 22/9/2026): the helper
        # above assumes price is currently ABOVE the reference level (a
        # broken resistance/trendline now acting as support). If price has
        # since pulled back BELOW that level - confirmed live on PUMP,
        # where breakout_pct_above was -7.52% - the "stop below the level"
        # math still runs and can land ABOVE current price: a nonsensical,
        # inverted stop for a long. Null it out here rather than let a
        # downstream consumer (pick_stop, the Entry Quality Gate) treat it
        # as usable.
        current_price = coin.get("price_usd")
        if current_price is not None:
            if coin["suggested_stop_resistance_based"] is not None and coin["suggested_stop_resistance_based"] >= current_price:
                coin["suggested_stop_resistance_based"] = None
            if coin["suggested_stop_trendline_based"] is not None and coin["suggested_stop_trendline_based"] >= current_price:
                coin["suggested_stop_trendline_based"] = None

        # v8: Binance derivatives - only worth the extra calls for coins that
        # actually fired something; most small-caps have no futures market
        # there anyway, which is a routine None result, not an error.
        # v21 fix (22/9/2026, found via systematic grep for every remaining
        # breakout_signal/extension_continuation_signal OR-chain after the
        # v18/v19 gaps): this gate ran BEFORE and independently of the
        # `fired` list below, so even after v19 gave priority_review coins a
        # confidence_score, they still never got OI/funding fetched here -
        # oi_price_confirms (15pts) and funding_not_crowded (10pts), a full
        # quarter of the score, stayed permanently unavailable to them.
        has_any_signal = (coin["breakout_signal"] or coin.get("extension_continuation_signal")
                           or coin.get("pullback_entry_signal") or coin.get("priority_review"))
        funding_rates = fetch_funding_rate_history(coin["symbol"]) if has_any_signal else None
        oi_now = fetch_open_interest_now(coin["symbol"]) if has_any_signal else None
        if funding_rates or oi_now is not None:
            time.sleep(POLITE_DELAY)
        reversed_, latest_funding = detect_funding_reversal(funding_rates) if funding_rates else (False, None)
        coin["funding_reversal_detected"] = reversed_
        coin["latest_funding_rate"] = latest_funding
        coin["open_interest_now"] = oi_now
        coin["oi_price_relationship"] = assess_oi_price_relationship(
            coin.get("change_24h_pct"), oi_now, oi_baseline.get(coin["id"], {}).get("oi")
        )
        if oi_now is not None:
            oi_baseline[coin["id"]] = {"oi": oi_now, "ts": datetime.now(timezone.utc).isoformat()}

        # v29 (24/9/2026): Data Tiering - the first step of the V2-merge plan
        # agreed with Azez. Every important field must be honest about its
        # own source and precision, not just present a number. Correction
        # made while building this: ATR/resistance/trendline here are NOT
        # approximate like indicators.rsi14 (that one comes from scan.py's
        # 15-min synthetic snapshots) - they come from fetch_ohlc(), real
        # CoinGecko OHLC candles (30 days). Tier 1 = real market-derived
        # data (CoinGecko OHLC here; OKX derivatives elsewhere); Tier 0 =
        # synthetic/approximate (scan.py's snapshot-built candles); Tier 2
        # = deep market data (OI/funding). "unavailable" when a fetch
        # simply failed this run - never silently treated as Tier 0.
        has_resistance_data = coin.get("resistance_level") is not None or coin.get("trendline_value_now") is not None
        coin["data_quality"] = {
            "price": {"tier": 1, "source": "coingecko_simple_price", "confidence": "real"},
            "rsi14": {"tier": 0, "source": "scan_synthetic_15m_samples", "confidence": "approximate"},
            "atr": {
                "tier": 1 if atr_value is not None else None,
                "source": "coingecko_ohlc_30d",
                "confidence": "real" if atr_value is not None else "unavailable",
            },
            "resistance_trendline": {
                "tier": 1 if has_resistance_data else None,
                "source": "coingecko_ohlc_30d",
                "confidence": "real" if has_resistance_data else "unavailable",
            },
            "structure_layer2": {"tier": 0, "source": "scan_synthetic_15m_samples", "confidence": "approximate"},
            "derivatives": {
                "tier": 2 if oi_now is not None else None,
                "source": "okx",
                "confidence": "real" if oi_now is not None else "unavailable",
            },
        }

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
    # v19 fix (22/9/2026): this internal `fired` list is a SEPARATE gate from
    # select_rotating_candidates()'s escalation - a priority_review coin was
    # correctly pulled into `candidates` and got every individual indicator
    # computed (OI, funding, trend-following eligibility, VWAP...), but never
    # got signal_quality or confidence_score AT ALL unless it ALSO happened
    # to trip breakout_signal/extension_continuation/trendline_break here.
    # Confirmed live: CRV/FF/SNX sat with every other field populated but no
    # confidence_score across two consecutive runs, purely because this list
    # didn't know about priority_review - the same class of gap already
    # fixed in scan.py's radar-flags selection and auto_paper_trade.py's
    # get_fired_coins(), just one layer deeper inside this file.
    fired = [c for c in candidates if c.get("breakout_signal") or c.get("extension_continuation_signal")
             or c.get("trendline_break_confirmed_signal") or c.get("priority_review")]
    is_cluster_run = len(fired) >= CLUSTER_SIGNAL_THRESHOLD
    for coin in fired:
        coin["cluster_wide_signal"] = is_cluster_run
        high_corr = (coin.get("btc_correlation_7d") or 0) >= HIGH_CORRELATION_THRESHOLD
        coin["signal_quality"] = "beta_driven_or_cluster" if (is_cluster_run or high_corr) else "idiosyncratic"

    # v8: composite confidence score - only meaningful once signal_quality is
    # known (right above), which is only knowable after the full-run cluster
    # check, hence why this runs here rather than inside the per-coin loop.
    for coin in fired:
        score_result = compute_confidence_score(coin, indicator_weights)
        coin.update(score_result)
        coin["evidence_clusters"] = compute_evidence_clusters(score_result["confidence_breakdown"], indicator_weights)

    # v40 (24/9/2026): Staged Data Funnel - step 8 of the V2-merge plan.
    # "candidates" (up to MAX_TOTAL_CANDIDATES_PER_RUN, v37) already ARE the
    # ~250-coin universe's promotion to real CoinGecko OHLC validation - the
    # funnel's first narrowing already exists, it just wasn't labeled. This
    # adds the FURTHER narrowing stages V2 describes (40 -> 20 -> 10 -> top
    # 5 for Agent Room), using data already computed here at zero extra API
    # cost - purely a ranking/labeling pass over "fired", not new fetches.
    # Later steps (10-14, real-OHLCV SMC detectors) will target progressively
    # narrower funnel_stage tiers instead of re-scanning everyone.
    FUNNEL_TIERS = [
        (5, "agent_room_priority"),
        (10, "shortlist_10"),
        (20, "shortlist_20"),
    ]
    ranked = sorted(fired, key=lambda c: -(c.get("confidence_score") or 0))
    for i, coin in enumerate(ranked):
        rank = i + 1
        stage = "validated"  # got real OHLC data (fetch_ohlc/ATR/resistance) but outside the top narrowing tiers
        for cutoff, label in FUNNEL_TIERS:
            if rank <= cutoff:
                stage = label
                break
        coin["funnel_stage"] = stage
        coin["funnel_rank_this_run"] = rank

    # v41 (24/9/2026): Real OHLCV persistence - step 9 of the V2-merge plan.
    # fetch_ohlc() already returns real 4-hour CoinGecko candles (30-day
    # window) - previously used once for ATR/resistance and discarded, never
    # actually stored for steps 10-14's future SMC detectors (FVG, order
    # block, liquidity sweep) to build on. Persisting it for EVERY fired
    # coin would bloat radar-flags.json for data 30 of 40 candidates will
    # never need - only the top two funnel tiers (which steps 10-14 are
    # meant to target) keep it; everyone else's transient copy is dropped.
    REAL_CANDLES_FUNNEL_TIERS = {"agent_room_priority", "shortlist_10"}
    for coin in ranked:
        raw = coin.pop("_candles_raw", None)
        if raw is not None and coin.get("funnel_stage") in REAL_CANDLES_FUNNEL_TIERS:
            coin["real_candles"] = raw
            coin["real_candles_meta"] = {
                "granularity": "4h", "window_days": OHLC_DAYS, "source": "coingecko_ohlc",
                "format": "[timestamp_ms, open, high, low, close]", "n_candles": len(raw),
            }
            coin["fair_value_gaps"] = detect_fair_value_gaps(raw, coin.get("atr_value"))
            coin["liquidity_sweep"] = detect_liquidity_sweep(raw, coin.get("atr_value"))
            inducement = detect_inducement(raw, coin.get("atr_value"))
            if inducement is not None:
                inducement.pop("swept_at_index", None)  # internal-only, used for picking the most recent candidate
            coin["inducement"] = inducement
            order_block = detect_order_block(raw, coin.get("atr_value"))
            if order_block is not None:
                order_block.pop("formed_at_index", None)
            coin["order_block"] = order_block
    # a candidate that fetched candles but never fired (never entered
    # "ranked" above) still had _candles_raw set by the per-coin loop -
    # without this, the transient key would leak into the saved JSON.
    for coin in candidates:
        coin.pop("_candles_raw", None)

    # v14: update the persistent score streak for every candidate actually
    # checked this run (fired or not - a checked-but-not-fired coin still
    # breaks its streak, since update_score_streak(..., score=None) resets
    # it; a coin simply not selected by this run's rotation is left alone).
    for coin in candidates:
        streak = update_score_streak(score_streak, coin["id"], coin.get("confidence_score"))
        coin["score_streak"] = streak
        eligible = streak >= TREND_FOLLOWING_MIN_STREAK
        coin["trend_following_eligible"] = eligible
        if eligible:
            stop, targets = compute_trend_following_stop(coin.get("price_usd"), coin.get("atr_value"))
            coin["trend_following_stop"] = stop
            coin["trend_following_targets"] = targets
            coin["path_to_target"] = compute_path_to_target(
                coin.get("price_usd"), targets or [], coin.get("all_resistance_levels", [])
            )
        else:
            coin["trend_following_stop"] = None
            coin["trend_following_targets"] = None

    flush_signal_log(pending_log_entries)
    save_retest_watchlist(retest_watchlist)
    save_trendline_watchlist(trendline_watchlist)
    save_oi_baseline(oi_baseline)
    save_score_streak(score_streak)

    btc_volatility_baseline = load_btc_volatility_baseline()
    data["market_regime"] = compute_market_regime(
        btc_closes, data.get("market_breadth_pct_green"), btc_volatility_baseline
    )
    save_btc_volatility_baseline(btc_volatility_baseline)

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
