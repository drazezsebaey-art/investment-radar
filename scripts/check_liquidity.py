"""
Investment Radar - Binance Liquidity Check
--------------------------------------------
Runs after scan.py, only against the coins already in data/radar-flags.json
(≤40 coins, so this stays cheap on Binance's rate limits). For each, checks
whether a <SYMBOL>USDT pair actually exists on Binance, and if so, pulls the
live bid/ask spread and 24h quote volume from Binance's own public API.

This runs on GitHub's servers, not through Claude's own fetch tool, so it is
not affected by the robots-disallow limits Claude runs into when it tries to
reach exchange APIs directly.

Fields added to each coin in radar-flags.json:
  binance_listed: true/false
  binance_spread_pct: (ask-bid)/ask * 100, if listed
  binance_quote_volume_24h: 24h USDT-quote volume on Binance specifically
"""
import json
import time
import urllib.request
import urllib.error
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RADAR_FLAGS_PATH = DATA_DIR / "radar-flags.json"
TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"

MIN_QUOTE_VOLUME_USD = 2_000_000  # below this, treat as too thin for SCALP


def fetch_ticker(symbol: str):
    url = f"{TICKER_URL}?symbol={symbol}"
    req = urllib.request.Request(url, headers={"User-Agent": "investment-radar/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 400:
            return None  # symbol does not exist on Binance
        raise


def main():
    if not RADAR_FLAGS_PATH.exists():
        print("No radar-flags.json found, skipping liquidity check.")
        return

    data = json.loads(RADAR_FLAGS_PATH.read_text(encoding="utf-8"))
    coins = data.get("coins", [])

    for coin in coins:
        symbol = f"{coin['symbol'].upper()}USDT"
        try:
            ticker = fetch_ticker(symbol)
        except Exception as exc:  # noqa: BLE001 - never let one bad symbol kill the run
            coin["binance_listed"] = None
            coin["binance_liquidity_error"] = str(exc)
            time.sleep(0.3)
            continue

        if ticker is None:
            coin["binance_listed"] = False
        else:
            bid = float(ticker.get("bidPrice", 0) or 0)
            ask = float(ticker.get("askPrice", 0) or 0)
            quote_vol = float(ticker.get("quoteVolume", 0) or 0)
            coin["binance_listed"] = True
            coin["binance_spread_pct"] = round((ask - bid) / ask * 100, 4) if ask else None
            coin["binance_quote_volume_24h"] = quote_vol
            coin["binance_liquidity_ok"] = quote_vol >= MIN_QUOTE_VOLUME_USD

        time.sleep(0.3)  # stay well under Binance's generous public rate limit

    RADAR_FLAGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    listed = sum(1 for c in coins if c.get("binance_listed"))
    print(f"Checked {len(coins)} candidates, {listed} listed on Binance with a USDT pair.")


if __name__ == "__main__":
    main()
