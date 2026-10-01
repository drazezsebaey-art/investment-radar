"""
asset_filters.py (v67, v68 behaviour rule) - one shared answer to "is this a tradeable crypto asset
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
                  "USDD", "BUSD", "GHO", "CRVUSD", "EURC", "USDG", "USD0", "PAXG", "XAUT",
                  "USAT", "USDTB", "BFUSD", "USDF", "AUSD", "GUSD", "BUIDL", "USDAI", "USDGO", "USX"}
PEGGED_IDS = {"tether", "usd-coin", "dai", "ethena-usde", "first-digital-usd", "usds", "paypal-usd",
              "true-usd", "frax", "pax-gold", "tether-gold", "ripple-usd", "usd1-wlfi", "global-dollar",
              "binance-bridged-usdt-bnb-smart-chain"}
# v68: a static list cannot keep up with new dollar products (USAT, USDTB, BUIDL,
# tokenized credit funds ...), so a coin that BEHAVES like a dollar peg is
# excluded too: price within PEG_BAND_PCT of $1 and almost no movement.
PEG_BAND_PCT = 3.0
PEG_MAX_ABS_7D_PCT = 0.6
PEG_MAX_ABS_24H_PCT = 0.3
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


def _first(coin, *keys):
    for k in keys:
        if coin.get(k) is not None:
            return coin[k]
    return None


def behaves_like_usd_peg(price, chg_7d, chg_24h) -> bool:
    """v68: True for a coin pinned to $1 (stablecoin / tokenized cash fund)."""
    try:
        if price is None or chg_7d is None:
            return False
        if abs(float(price) - 1.0) > PEG_BAND_PCT / 100:
            return False
        if abs(float(chg_7d)) > PEG_MAX_ABS_7D_PCT:
            return False
        return chg_24h is None or abs(float(chg_24h)) <= PEG_MAX_ABS_24H_PCT
    except (TypeError, ValueError):
        return False


def exclusion_reason_for(coin: dict):
    """v68: id/symbol rules plus the peg-behaviour rule, for any record shape
    (CoinGecko markets row, market-scan / radar-flags record, pre-pump dict)."""
    r = exclusion_reason(coin.get("id"), coin.get("symbol"))
    if r:
        return r
    price = _first(coin, "current_price", "price_usd")
    c7 = _first(coin, "price_change_percentage_7d_in_currency", "change_7d_pct")
    c24 = _first(coin, "price_change_percentage_24h_in_currency", "change_24h_pct")
    return "PEGGED_BEHAVIOUR" if behaves_like_usd_peg(price, c7, c24) else None


def is_excluded(coin: dict) -> bool:
    return exclusion_reason_for(coin) is not None
