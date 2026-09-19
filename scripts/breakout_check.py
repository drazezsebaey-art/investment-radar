"""
Investment Radar - Resistance Breakout Check (v2)
------------------------------------------------------------
Runs after check_liquidity.py, only against coins already confirmed
binance_listed == True in data/radar-flags.json (no point spending API
calls on a structural breakout read for a coin we can't trade anyway -
same "check the hard gate first" discipline as the Binance listing fix).

v2 changes from v1 (post-audit fixes):
  - FIXED: rotation instead of a fixed top-N-by-rank slice. v1 always
    checked the SAME highest-ranked MAX_CANDIDATES_PER_RUN coins every run
    (sorting by market_cap_rank is deterministic, so lower-ranked coins
    were NEVER checked unless their rank improved) - this silently broke
    the promise that "the rest get checked on later runs." v2 persists a
    rotation offset in data/breakout-rotation-state.json and advances it
    each run, so every Binance-listed candidate gets checked in turn over
    successive runs.
  - FIXED: volume confirmation was comparing one HOURLY data point against
    a ~30-day average of hourly points (CoinGecko's market_chart returns
    hourly granularity for any days value between 2 and 90, not daily as
    the v1 code assumed) - a much noisier, different metric than intended.
    v2 aggregates the hourly series into daily buckets first, drops the
    final (likely partial/incomplete) day, and compares the latest COMPLETE
    day's volume against the average of the prior complete days.

WHAT THIS ADDS THAT THE BASE SCANNER CAN'T SEE:
scan.py only ever compares two numbers (price now vs price N days ago).
It has no concept of "level" - it can't tell you a coin broke a ceiling
that had rejected it three times before. This script gives it that
concept, using CoinGecko's historical OHLC + volume data:

  1. RESISTANCE DETECTION - find local price peaks in the recent history
     (excluding the most recent few candles, which are reserved for the
     breakout itself), cluster peaks that sit within a tolerance of each
     other into "levels", and keep levels touched at least MIN_TOUCHES
     times. This is a zone, not a single exact price - consistent with
     "support/resistance as zones, not exact numbers."

  2. BREAKOUT CONFIRMATION - the latest candle's CLOSE (not a wick) must
     sit meaningfully above the resistance zone, and the candle before it
     must also have closed above (or very near) it - one lone candle
     poking through is not treated as a confirmed break.

  3. VOLUME CONFIRMATION - the latest COMPLETE day's trading volume must be
     noticeably above the average of the prior complete days. A breakout on
     thin volume is exactly the kind of false break this step exists to
     filter out.

A coin only gets breakout_signal: true when ALL THREE agree. This is a
first-pass heuristic (simple peak-clustering), not full chart-pattern
recognition (head & shoulders, triangles, etc.) - those are still left
for manual/visual review, per the standing "don't force patterns onto
ambiguous structure" rule.

Fields added to each qualifying coin in radar-flags.json:
  resistance_level: float - the identified zone's reference price
  resistance_touches: int - how many prior peaks clustered into this zone
  breakout_confirmed: bool - close-based break confirmed over 2 candles
  breakout_pct_above: float - how far the latest close sits above the zone
  volume_ratio: float - latest complete day's volume / avg of prior complete days
  volume_confirmed: bool - true if volume_ratio >= VOLUME_CONFIRM_MULTIPLIER
  breakout_signal: bool - true only if breakout_confirmed AND volume_confirmed
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
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# --- tunable thresholds -----------------------------------------------
OHLC_DAYS = 30                 # history window for resistance detection (4h candles on this range)
PEAK_NEIGHBORS = 2              # a high must exceed this many candles on each side to count as a local peak
TOUCH_TOLERANCE_PCT = 1.0       # peaks within this % of each other cluster into the same "zone"
MIN_TOUCHES = 2                 # a zone needs at least this many prior peaks to count as real resistance
EXCLUDE_RECENT_CANDLES = 3      # candles reserved for the breakout itself, not used to find the level
BREAKOUT_BUFFER_PCT = 0.3       # latest close must clear the zone by at least this % to count as a real break
CONFIRM_CANDLES = 2             # this many of the most recent candles must have closed above the zone

VOLUME_DAYS = 30                # history window for the volume average (fetched hourly, aggregated to daily)
VOLUME_CONFIRM_MULTIPLIER = 1.3  # latest complete day's volume must be at least this many times the recent average

REQUEST_TIMEOUT = 20
POLITE_DELAY = 7                # seconds between CoinGecko calls - GitHub Actions runners share IPs
                                 # with many other users hitting CoinGecko at the same time, so the
                                 # documented 10-30 calls/min limit is not reliably available to us;
                                 # this is deliberately conservative

MAX_RETRIES = 3                 # retries on HTTP 429 before giving up on that call
RETRY_BACKOFF_BASE = 15         # seconds - first retry waits this long, then doubles each attempt

MAX_CANDIDATES_PER_RUN = 10     # coins checked per run (2 calls each = up to 20 calls, well inside
                                 # a 15-minute cron window even at 7s spacing); WHICH 10 rotates each
                                 # run (see load_rotation_offset/save_rotation_offset) so every
                                 # Binance-listed candidate eventually gets checked, not just the
                                 # same highest-ranked ones every time


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
    """Pick the next MAX_CANDIDATES_PER_RUN coins in rotation order (by a
    stable sort on id, so the ordering doesn't shift just because a coin's
    market_cap_rank wiggled), advancing the persisted offset each run so
    every coin gets its turn over successive runs instead of the same
    top-N being picked forever."""
    if not all_listed:
        return []
    ordered = sorted(all_listed, key=lambda c: c["id"])  # stable, rank-independent order
    n = len(ordered)
    offset = load_rotation_offset() % n
    # take MAX_CANDIDATES_PER_RUN starting at offset, wrapping around
    candidates = [ordered[(offset + i) % n] for i in range(min(MAX_CANDIDATES_PER_RUN, n))]
    save_rotation_offset((offset + len(candidates)) % n)
    return candidates


def fetch_json(url: str):
    """Fetch JSON with retry-with-backoff on HTTP 429 (rate limit)."""
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
    """[[timestamp, open, high, low, close], ...] ascending by time."""
    url = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc?{urllib.parse.urlencode({'vs_currency': 'usd', 'days': OHLC_DAYS})}"
    return fetch_json(url)


def fetch_hourly_volumes(coin_id: str):
    """Returns [(timestamp_ms, volume), ...] from market_chart - CoinGecko
    returns HOURLY granularity for any days value between 2 and 90, not
    daily, so this must be aggregated before use (see daily_volumes_from_hourly)."""
    url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart?{urllib.parse.urlencode({'vs_currency': 'usd', 'days': VOLUME_DAYS})}"
    data = fetch_json(url)
    return data.get("total_volumes", [])


def daily_volumes_from_hourly(hourly: list):
    """Aggregate hourly (timestamp_ms, volume) points into daily totals,
    ascending by date. The result's last entry is likely a partial day
    (today, still in progress) - callers should drop it before averaging."""
    daily = defaultdict(float)
    for ts_ms, vol in hourly:
        day = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date()
        daily[day] += vol or 0
    return [daily[d] for d in sorted(daily.keys())]


def find_resistance_zone(candles: list):
    """Cluster local high-peaks in the historical portion of the candles
    (excluding the most recent EXCLUDE_RECENT_CANDLES) and return the
    best-touched zone below the current price, or None."""
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
    """Confirm the latest CONFIRM_CANDLES candles closed meaningfully above
    the zone, and that price was genuinely below it beforehand."""
    level = zone["level"]
    recent = candles[-CONFIRM_CANDLES:]
    closes_above = all(c[4] >= level * (1 + BREAKOUT_BUFFER_PCT / 100) for c in recent)

    pre_break = candles[-(CONFIRM_CANDLES + 3):-CONFIRM_CANDLES]
    was_below = any(c[4] < level for c in pre_break) if pre_break else True

    latest_close = candles[-1][4]
    pct_above = (latest_close - level) / level * 100
    return closes_above and was_below, pct_above


def check_volume(hourly_volumes: list):
    """Latest COMPLETE day's total volume vs the average of the prior
    complete days (aggregated from hourly points - see daily_volumes_from_hourly)."""
    daily = daily_volumes_from_hourly(hourly_volumes)
    if len(daily) < 6:  # need at least a few complete days plus the partial one to drop
        return None, False
    complete_days = daily[:-1]  # drop the last (likely partial/in-progress) day
    latest = complete_days[-1]
    baseline = complete_days[:-1]
    avg = mean(baseline) if baseline else 0
    if avg == 0:
        return None, False
    ratio = latest / avg
    return round(ratio, 2), ratio >= VOLUME_CONFIRM_MULTIPLIER


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

    for coin in candidates:
        coin_id = coin["id"]
        try:
            candles = fetch_ohlc(coin_id)
            time.sleep(POLITE_DELAY)
            hourly_volumes = fetch_hourly_volumes(coin_id)
            time.sleep(POLITE_DELAY)
        except Exception as exc:  # noqa: BLE001 - never let one bad coin kill the run
            coin["breakout_signal"] = None
            coin["breakout_error"] = str(exc)
            continue

        zone = find_resistance_zone(candles) if candles else None
        if zone is None:
            coin["breakout_signal"] = False
            coin.pop("breakout_error", None)
            continue

        breakout_confirmed, pct_above = check_breakout(candles, zone)
        volume_ratio, volume_confirmed = check_volume(hourly_volumes)

        coin["resistance_level"] = round(zone["level"], 6)
        coin["resistance_touches"] = zone["touches"]
        coin["breakout_confirmed"] = breakout_confirmed
        coin["breakout_pct_above"] = round(pct_above, 2)
        coin["volume_ratio"] = volume_ratio
        coin["volume_confirmed"] = volume_confirmed
        coin["breakout_signal"] = bool(breakout_confirmed and volume_confirmed)
        coin.pop("breakout_error", None)

    RADAR_FLAGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    signals = sum(1 for c in candidates if c.get("breakout_signal"))
    print(f"Checked {len(candidates)} candidates, {signals} with a confirmed resistance breakout.")


if __name__ == "__main__":
    main()
