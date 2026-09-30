"""
asset_filters.py (v67) - one shared answer to "is this a tradeable crypto asset
for the radar?" used by scan.py, breakout_check.py, prepump_watchlist.py and
digest.py.

Excluded:
  * pegged assets - stablecoins and gold-pegged tokens (no room for a stop
    beyond trading costs; same list as auto_paper_trade.py v65.5)
  * tokenized equities - on-chain wrappers of stocks/preferreds (e.g.
    "strategy-pp-variable-xstock" / STRCX). They move with an equity market,
    not with crypto flows, so crypto signals about them are noise.

Pure functions, no network. auto_paper_trade.py keeps its own copy of the
pegged list on purpose (v65.5 is already live and tested); keep both in sync.
"""

PEGGED_SYMBOLS = {"USDT", "USDC", "DAI", "USDE", "FDUSD", "USDS", "PYUSD", "TUSD", "FRAX", "USD1", "RLUSD",
                  "USDD", "BUSD", "GHO", "CRVUSD", "EURC", "USDG", "USD0", "PAXG", "XAUT"}
PEGGED_IDS = {"tether", "usd-coin", "dai", "ethena-usde", "first-digital-usd", "usds", "paypal-usd",
              "true-usd", "frax", "pax-gold", "tether-gold", "ripple-usd", "usd1-wlfi", "global-dollar",
              "binance-bridged-usdt-bnb-smart-chain"}
# substrings in a CoinGecko id that mark a tokenized stock / ETF / preferred share
TOKENIZED_EQUITY_ID_MARKERS = ("xstock", "tokenized-stock", "ondo-tokenized", "backed-")


def is_pegged(coin_id=None, symbol=None) -> bool:
    return (coin_id or "") in PEGGED_IDS or (symbol or "").upper() in PEGGED_SYMBOLS


def is_tokenized_equity(coin_id=None) -> bool:
    cid = (coin_id or "").lower()
    return any(m in cid for m in TOKENIZED_EQUITY_ID_MARKERS)


def exclusion_reason(coin_id=None, symbol=None):
    """None when the asset is a normal crypto asset, else a short reason code."""
    if is_pegged(coin_id, symbol):
        return "PEGGED_ASSET"
    if is_tokenized_equity(coin_id):
        return "TOKENIZED_EQUITY"
    return None


def is_excluded(coin: dict) -> bool:
    return exclusion_reason(coin.get("id"), coin.get("symbol")) is not None
