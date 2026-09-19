"""
Investment Radar - Resistance Breakout Check
------------------------------------------------------------
Runs after check_liquidity.py, only against coins already confirmed
binance_listed == True in data/radar-flags.json (no point spending API
calls on a structural breakout read for a coin we can't trade anyway -
same "check the hard gate first" discipline as the Binance listing fix).

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

  3. VOLUME CONFIRMATION - the latest day's trading volume must be
     noticeably above its recent daily average. A breakout on thin volume
     is exactly the kind of false break this step exists to filter out.

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
  volume_ratio: float - latest day's volume / recent average daily volume
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

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RADAR_FLAGS_PATH = DATA_DIR / "radar-flags.json"
COINGECKO_BASE = "https://api.coingecko.com/api/v3"

# --- tunable thresholds -----------------------------------------------
OHLC_DAYS = 30                 # history window for resistance detection (4h candles on this range)
PEAK_NEIGHBORS = 2              # a high must exceed this many candles on each side to count as a local peak
TOUCH_TOLERANCE_PCT = 1.0       # peaks within this % of each other cluster into the same "zone"
MIN_TOUCHES = 2                 # a zone needs at least this many prior peaks to count as real resistance
EXCLUDE_RECENT_CANDLES = 3      # candles reserved for the breakout itself, not used to find the level
BREAKOUT_BUFFER_PCT = 0.3       # latest close must clear the zone by at least this % to count as a real break
CONFIRM_CANDLES = 2             # this many of the most recent candles must have closed above the zone

VOLUME_DAYS = 30                # history window for the volume average
VOLUME_CONFIRM_MULTIPLIER = 1.3  # latest day's volume must be at least this many times the recent average

REQUEST_TIMEOUT = 20
POLITE_DELAY = 1.5              # seconds between CoinGecko calls


def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "investment-radar/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def fetch_ohlc(coin_id: str):
    """[[timestamp, open, high, low, close], ...] ascending by time."""
    url = f"{COINGECKO_BASE}/coins/{coin_id}/ohlc?{urllib.parse.urlencode({'vs_currency': 'usd', 'days': OHLC_DAYS})}"
    return fetch_json(url)


def fetch_daily_volumes(coin_id: str):
    """Returns a list of (timestamp, volume) from market_chart, ascending by time."""
    url = f"{COINGECKO_BASE}/coins/{coin_id}/market_chart?{urllib.parse.urlencode({'vs_currency': 'usd', 'days': VOLUME_DAYS})}"
    data = fetch_json(url)
    return data.get("total_volumes", [])


def find_resistance_zone(candles: list):
    """Cluster local high-peaks in the historical portion of the candles
    (excluding the most recent EXCLUDE_RECENT_CANDLES) and return the
    best-touched zone below the current price, or None."""
    if len(candles) < (PEAK_NEIGHBORS * 2 + MIN_TOUCHES + EXCLUDE_RECENT_CANDLES):
        return None

    history = candles[:-EXCLUDE_RECENT_CANDLES]
    highs = [c[2] for c in history]

    # 1) find local peaks
    peaks = []
    for i in range(PEAK_NEIGHBORS, len(highs) - PEAK_NEIGHBORS):
        window = highs[i - PEAK_NEIGHBORS: i + PEAK_NEIGHBORS + 1]
        if highs[i] == max(window):
            peaks.append(highs[i])

    if len(peaks) < MIN_TOUCHES:
        return None

    # 2) cluster peaks within TOUCH_TOLERANCE_PCT of each other
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

    # 3) keep clusters with enough touches, pick the one with the most touches
    #    (ties broken by picking the highest zone - the most recently relevant ceiling)
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

    # make sure this is a genuine break, not a level the price was already above
    pre_break = candles[-(CONFIRM_CANDLES + 3):-CONFIRM_CANDLES]
    was_below = any(c[4] < level for c in pre_break) if pre_break else True

    latest_close = candles[-1][4]
    pct_above = (latest_close - level) / level * 100
    return closes_above and was_below, pct_above


def check_volume(volumes: list):
    """Latest day's volume vs the average of the preceding days."""
    if len(volumes) < 5:
        return None, False
    values = [v[1] for v in volumes]
    latest = values[-1]
    baseline = values[:-1]
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

    candidates = [c for c in coins if c.get("binance_listed") is True]
    print(f"Running breakout check on {len(candidates)} Binance-listed candidates "
          f"(skipping {len(coins) - len(candidates)} not listed/unchecked).")

    for coin in candidates:
        coin_id = coin["id"]
        try:
            candles = fetch_ohlc(coin_id)
            time.sleep(POLITE_DELAY)
            volumes = fetch_daily_volumes(coin_id)
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
        volume_ratio, volume_confirmed = check_volume(volumes)

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
