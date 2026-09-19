"""
Investment Radar - Historical Backtest (new in v5)
------------------------------------------------------------
Standalone, manually-triggered script (NOT part of the 15-min live cron -
it's too API-heavy for that). Solves a real problem: evaluate_signals.py
only accumulates one real-world outcome per fired signal per day, so
reaching a statistically meaningful sample (~30+) takes weeks of live
running. This script walks backward through real historical price data
for a fixed sample of coins and simulates what the resistance+breakout
logic WOULD have flagged at each point in time, then checks what actually
happened next - generating dozens of historical outcomes in one run.

HONESTY ABOUT WHAT THIS DOES AND DOESN'T TEST:
  - It mirrors find_resistance_zone() and check_breakout() from
    breakout_check.py as closely as possible (kept in sync manually - if
    you change the thresholds in breakout_check.py, update them here too).
  - It does NOT replicate the volume confirmation, trend filter, VWAP, or
    fibonacci extension checks - CoinGecko's OHLC endpoint has no volume
    field, and aligning a separate hourly-volume fetch to each historical
    candle for every simulated day would multiply the API cost far beyond
    what's practical for a manual script. So this backtests the RESISTANCE
    + BREAKOUT component in isolation - a weaker, simpler signal than the
    live system's full breakout_signal (which also requires volume and
    trend agreement). Treat these results as informative about "does
    breaking a touched resistance level tend to lead anywhere," not as a
    backtest of the exact live breakout_signal condition.
  - No lookahead bias: at each simulated point i, only candles[0:i+1] are
    used to find the resistance zone and check the break - exactly what a
    live run would have known at that moment. The outcome is read from
    candles AFTER i only.
  - Candle granularity for OHLC over 30-90 days is 4h (CoinGecko's own
    bucketing) - the same as the live system's OHLC_DAYS=45 window - so
    this is time-comparable, not daily vs 4h apples-to-oranges.

Output: data/backtest-results.json - per-coin and aggregate stats (n,
% positive, avg return) for the simulated breakout condition vs a
no-signal baseline sampled from the same price series, plus each coin's
simple buy-and-hold return over the same window as a third reference point
(does the signal beat just holding the coin, not only beat "no signal").
"""
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from statistics import mean

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.json"
OUTPUT_PATH = DATA_DIR / "backtest-results.json"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# mirrors breakout_check.py's resistance/breakout constants - keep in sync manually
OHLC_DAYS = 30                  # CoinGecko's OHLC granularity: 1-2 days=30min, 3-30 days=4h,
                                  # 31+ days=4-DAY candles. A prior value of 90 fell into the
                                  # 4-day bucket, giving only ~22 candles total - far below the
                                  # 82 minimum this script needs (MIN_LOOKBACK_CANDLES +
                                  # FORWARD_WINDOW_CANDLES + 10), so every single coin was
                                  # silently skipped as "not enough history" with no error at
                                  # all. 30 days keeps 4h candles (~180 total), comfortably enough.
PEAK_NEIGHBORS = 2
TOUCH_TOLERANCE_PCT = 1.0
MIN_TOUCHES = 2
EXCLUDE_RECENT_CANDLES = 3
BREAKOUT_BUFFER_PCT = 0.3
CONFIRM_CANDLES = 2

MIN_LOOKBACK_CANDLES = 60        # need enough history before we start simulating signals
FORWARD_WINDOW_CANDLES = 12      # ~2 days at 4h - how far forward we measure the outcome
BASELINE_SAMPLE_EVERY = 8        # sample every Nth non-signal candle as a baseline point (cost control)

REQUEST_TIMEOUT = 20
POLITE_DELAY = 7
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 15

# Extra sample coins beyond the watchlist, for a broader/more diverse test set
SAMPLE_EXTRA_IDS = ["uniswap", "avalanche-2", "injective-protocol", "aave", "optimism"]


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "investment-radar-backtest/1.0"})
    last_exc = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code == 429 and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_BASE * (2 ** attempt)
                print(f"  429 rate-limited, retrying in {wait}s")
                time.sleep(wait)
                continue
            raise
    raise last_exc


def fetch_ohlc(coin_id: str):
    url = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc?{urllib.parse.urlencode({'vs_currency': 'usd', 'days': OHLC_DAYS})}"
    return fetch_json(url)


def find_resistance_zone(candles_so_far: list):
    """Identical logic to breakout_check.py's find_resistance_zone."""
    if len(candles_so_far) < (PEAK_NEIGHBORS * 2 + MIN_TOUCHES + EXCLUDE_RECENT_CANDLES):
        return None
    history = candles_so_far[:-EXCLUDE_RECENT_CANDLES] if EXCLUDE_RECENT_CANDLES else candles_so_far
    highs = [c[2] for c in history]

    peaks = []
    for i in range(PEAK_NEIGHBORS, len(highs) - PEAK_NEIGHBORS):
        window = highs[i - PEAK_NEIGHBORS: i + PEAK_NEIGHBORS + 1]
        if highs[i] == max(window):
            peaks.append(highs[i])

    if len(peaks) < MIN_TOUCHES:
        return None

    peaks.sort()
    clusters, current = [], [peaks[0]]
    for p in peaks[1:]:
        if (p - current[-1]) / current[-1] * 100 <= TOUCH_TOLERANCE_PCT:
            current.append(p)
        else:
            clusters.append(current)
            current = [p]
    clusters.append(current)

    valid = [c for c in clusters if len(c) >= MIN_TOUCHES]
    if not valid:
        return None
    best = max(valid, key=lambda c: (len(c), mean(c)))
    return {"level": mean(best), "touches": len(best)}


def check_breakout(candles_so_far: list, zone: dict):
    level = zone["level"]
    recent = candles_so_far[-CONFIRM_CANDLES:]
    closes_above = all(c[4] >= level * (1 + BREAKOUT_BUFFER_PCT / 100) for c in recent)
    pre_break = candles_so_far[-(CONFIRM_CANDLES + 3):-CONFIRM_CANDLES]
    was_below = any(c[4] < level for c in pre_break) if pre_break else True
    return closes_above and was_below


def simulate_coin(coin_id: str, candles: list) -> list:
    """Walk forward through the candle series, simulating a breakout check
    at each point using only data available up to that point, then reading
    the outcome from candles after it."""
    results = []
    n = len(candles)
    for i in range(MIN_LOOKBACK_CANDLES, n - FORWARD_WINDOW_CANDLES):
        known_so_far = candles[: i + 1]
        zone = find_resistance_zone(known_so_far)
        is_signal = False
        if zone is not None:
            is_signal = check_breakout(known_so_far, zone)

        if not is_signal and (i % BASELINE_SAMPLE_EVERY != 0):
            continue  # skip most non-signal points to control output size, keep a sample as baseline

        entry_close = candles[i][4]
        exit_close = candles[i + FORWARD_WINDOW_CANDLES][4]
        pct_change = round((exit_close - entry_close) / entry_close * 100, 2) if entry_close else None

        results.append({
            "coin_id": coin_id,
            "candle_index": i,
            "is_signal": is_signal,
            "resistance_touches": zone["touches"] if (zone and is_signal) else None,
            "entry_close": entry_close,
            "pct_change_after_window": pct_change,
        })
    return results


def compute_buy_and_hold(candles: list) -> float:
    """Simple buy-and-hold return over the full backtest window: buy at the
    first candle's close, hold to the last candle's close. Reported as a
    baseline so the simulated signals can be judged against 'doing nothing
    but holding', not only against each other."""
    if len(candles) < 2:
        return None
    first_close = candles[0][4]
    last_close = candles[-1][4]
    if not first_close:
        return None
    return round((last_close - first_close) / first_close * 100, 2)


def summarize(all_results: list) -> dict:
    def stats_for(subset):
        n = len(subset)
        changes = [r["pct_change_after_window"] for r in subset if r["pct_change_after_window"] is not None]
        positive = [c for c in changes if c > 0]
        return {
            "n": n,
            "pct_positive": round(len(positive) / n * 100, 1) if n else None,
            "avg_change_pct": round(mean(changes), 2) if changes else None,
        }

    signals = [r for r in all_results if r["is_signal"]]
    baseline = [r for r in all_results if not r["is_signal"]]
    return {
        "simulated_breakout": stats_for(signals),
        "no_signal_baseline": stats_for(baseline),
    }


def main():
    watchlist_ids = []
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        watchlist_ids = cfg.get("always_include", [])

    coin_ids = sorted(set(watchlist_ids) | set(SAMPLE_EXTRA_IDS))
    print(f"Backtesting {len(coin_ids)} coins over {OHLC_DAYS} days of 4h candles each...")

    per_coin = {}
    all_results = []
    for coin_id in coin_ids:
        try:
            candles = fetch_ohlc(coin_id)
        except Exception as exc:  # noqa: BLE001
            print(f"  {coin_id}: fetch failed - {exc}")
            time.sleep(POLITE_DELAY)
            continue
        time.sleep(POLITE_DELAY)

        if len(candles) < MIN_LOOKBACK_CANDLES + FORWARD_WINDOW_CANDLES + 10:
            print(f"  {coin_id}: not enough history ({len(candles)} candles), skipping")
            continue

        coin_results = simulate_coin(coin_id, candles)
        all_results.extend(coin_results)
        coin_summary = summarize(coin_results)
        coin_summary["buy_and_hold_pct"] = compute_buy_and_hold(candles)
        per_coin[coin_id] = coin_summary
        n_sig = coin_summary["simulated_breakout"]["n"]
        print(f"  {coin_id}: {len(candles)} candles, {n_sig} simulated signals, "
              f"buy_and_hold={coin_summary['buy_and_hold_pct']}%")

    overall = summarize(all_results)
    bh_values = [c["buy_and_hold_pct"] for c in per_coin.values() if c.get("buy_and_hold_pct") is not None]
    overall_buy_and_hold_avg_pct = round(mean(bh_values), 2) if bh_values else None

    output = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": (
            "Tests the resistance+breakout logic ONLY (no volume/trend/VWAP confirmation - "
            "see script docstring for why). n under ~30 is directional only, not conclusive. "
            "overall_buy_and_hold_avg_pct is the simple average of each coin's buy-and-hold "
            "return over the same window - a baseline to judge whether the simulated signal "
            "beats 'doing nothing but holding', not a signal-weighted comparison."
        ),
        "ohlc_days": OHLC_DAYS,
        "forward_window_candles": FORWARD_WINDOW_CANDLES,
        "overall": overall,
        "overall_buy_and_hold_avg_pct": overall_buy_and_hold_avg_pct,
        "per_coin": per_coin,
    }
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nOverall: simulated_breakout n={overall['simulated_breakout']['n']} "
          f"pct_positive={overall['simulated_breakout']['pct_positive']} "
          f"avg_change={overall['simulated_breakout']['avg_change_pct']}%")
    print(f"Baseline: n={overall['no_signal_baseline']['n']} "
          f"pct_positive={overall['no_signal_baseline']['pct_positive']} "
          f"avg_change={overall['no_signal_baseline']['avg_change_pct']}%")
    print(f"Buy-and-hold average across all coins (full window): {overall_buy_and_hold_avg_pct}%")


if __name__ == "__main__":
    main()
