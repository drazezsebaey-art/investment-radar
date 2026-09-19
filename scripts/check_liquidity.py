"""
Investment Radar - Binance Liquidity Check (via CoinGecko)
------------------------------------------------------------
Runs after scan.py, only against the coins already in data/radar-flags.json.

WHY THIS VERSION EXISTS:
GitHub Actions runners are geo-blocked by Binance itself (HTTP 451 -
"Unavailable For Legal Reasons", Binance's response to requests from
US-based cloud/CI IP ranges). Querying api.binance.com directly from this
workflow will never work, no matter how the request is built.

Instead, this checks Binance listing/liquidity indirectly through
CoinGecko's per-exchange tickers endpoint, which reports Binance's own
order-book data (bid/ask spread, 24h volume) without ever contacting
Binance's servers. CoinGecko is not geo-blocked (scan.py already proves
this - it hits CoinGecko successfully on the same runners).

Fields added to each coin in radar-flags.json:
  binance_listed: true/false
  binance_spread_pct: CoinGecko's reported bid/ask spread % for the coin's
                       USDT pair on Binance, if listed
  binance_quote_volume_24h: 24h volume (in USD) for that pair on Binance
  binance_liquidity_ok: true if binance_quote_volume_24h >= MIN_QUOTE_VOLUME_USD

On any per-coin lookup failure, binance_listed is set to None and
binance_liquidity_error records why - never let one bad coin kill the run.
"""
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RADAR_FLAGS_PATH = DATA_DIR / "radar-flags.json"
TICKERS_URL = "https://api.coingecko.com/api/v3/exchanges/binance/tickers"

MIN_QUOTE_VOLUME_USD = 2_000_000  # below this, treat as too thin for SCALP
BATCH_SIZE = 40        # coin_ids per request - keeps URL length and response size sane
MAX_PAGES_PER_BATCH = 3  # safety cap in case a batch's tickers span many pages
REQUEST_TIMEOUT = 20
POLITE_DELAY = 1.5     # seconds between calls - stay well under CoinGecko's free-tier limit


def fetch_tickers_page(coin_ids_param: str, page: int):
    """Fetch one page of Binance tickers filtered to the given coin_ids."""
    params = {"coin_ids": coin_ids_param, "page": page, "order": "volume_desc"}
    url = f"{TICKERS_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "investment-radar/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def fetch_tickers_for_batch(coin_ids: list) -> list:
    """Fetch all Binance tickers (all pages) for one batch of CoinGecko coin ids."""
    all_tickers = []
    ids_param = ",".join(coin_ids)
    for page in range(1, MAX_PAGES_PER_BATCH + 1):
        try:
            data = fetch_tickers_page(ids_param, page)
        except Exception as exc:  # noqa: BLE001 - never let one bad batch kill the run
            print(f"  batch fetch error (page {page}): {exc}")
            break

        tickers = data.get("tickers", [])
        if not tickers:
            break
        all_tickers.extend(tickers)
        if len(tickers) < 100:  # CoinGecko returns up to 100 tickers per page
            break
        time.sleep(POLITE_DELAY)

    return all_tickers


def best_usdt_ticker(tickers: list, coin_id: str):
    """Among fetched tickers, return the coin's USDT pair with the highest
    24h volume (a coin can appear with more than one USDT-quoted pair)."""
    candidates = [
        t for t in tickers
        if t.get("coin_id") == coin_id and t.get("target") == "USDT"
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda t: (t.get("converted_volume") or {}).get("usd", 0) or 0,
    )


def main():
    if not RADAR_FLAGS_PATH.exists():
        print("No radar-flags.json found, skipping liquidity check.")
        return

    data = json.loads(RADAR_FLAGS_PATH.read_text(encoding="utf-8"))
    coins = data.get("coins", [])
    if not coins:
        print("No coins in radar-flags.json, nothing to check.")
        return

    coin_ids = [c["id"] for c in coins]
    all_tickers = []
    for i in range(0, len(coin_ids), BATCH_SIZE):
        batch = coin_ids[i:i + BATCH_SIZE]
        try:
            all_tickers.extend(fetch_tickers_for_batch(batch))
        except Exception as exc:  # noqa: BLE001 - never let one bad batch kill the whole run
            print(f"  unexpected error on batch {batch}: {exc}")
        time.sleep(POLITE_DELAY)

    for coin in coins:
        try:
            ticker = best_usdt_ticker(all_tickers, coin["id"])
        except Exception as exc:  # noqa: BLE001
            coin["binance_listed"] = None
            coin["binance_liquidity_error"] = str(exc)
            continue

        if ticker is None:
            coin["binance_listed"] = False
            coin.pop("binance_liquidity_error", None)
            coin.pop("binance_spread_pct", None)
            coin.pop("binance_quote_volume_24h", None)
            coin.pop("binance_liquidity_ok", None)
            continue

        quote_vol = (ticker.get("converted_volume") or {}).get("usd", 0) or 0
        spread = ticker.get("bid_ask_spread_percentage")
        coin["binance_listed"] = True
        coin["binance_spread_pct"] = round(spread, 4) if spread is not None else None
        coin["binance_quote_volume_24h"] = quote_vol
        coin["binance_liquidity_ok"] = quote_vol >= MIN_QUOTE_VOLUME_USD
        coin.pop("binance_liquidity_error", None)

    RADAR_FLAGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    listed = sum(1 for c in coins if c.get("binance_listed"))
    print(f"Checked {len(coins)} candidates, {listed} listed on Binance with a USDT pair (via CoinGecko).")


if __name__ == "__main__":
    main()
