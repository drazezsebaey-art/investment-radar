"""
Investment Radar - CoinGecko Market Scanner (v4)
--------------------------------------------------
v4 additions (professional-trader improvements, no paid data sources):
  - MARKET BREADTH: what % of the scanned universe is green right now.
    A single coin's breakout means something different in a 90%-green
    market (everything is up) vs a 15%-green market (this coin is a real
    outlier). Written as market_breadth_pct_green on every snapshot.
  - CATEGORY CLUSTERING WARNING: if 2+ flagged coins this run share a
    tracked category (from categories.json), that's one sector move, not
    N independent opportunities - same "don't count correlated signals as
    independent" discipline used everywhere else. Reported as
    category_clusters at the top level of radar-flags.json.

v3 changes (still present):
  - FIXED radar-flags.json's "count" to match the actual saved array length.
  - REMOVED the unreliable ATR(14) approximation.
  - ADDED BTC-relative excess return per coin (beta filter).
  - CONSOLIDATED reversal + sharp-move flags to avoid double-reporting the
    same 24h number under two labels.

Approximation notice: history points are 15-minute snapshots, not true
exchange candles. RSI/EMA computed from them are directional approximations
over a short window, not the same as chart-read RSI(14)/EMA(9,21).
"""
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

BASE_URL = "https://api.coingecko.com/api/v3/coins/markets"
CATEGORY_URL = "https://api.coingecko.com/api/v3/coins/markets"
VS_CURRENCY = "usd"
PER_PAGE = 250
PAGES = 1
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.json"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

HISTORY_PATH = DATA_DIR / "price-history.json"
INDICATORS_PATH = DATA_DIR / "indicators.json"
CATEGORIES_PATH = DATA_DIR / "categories.json"

MAX_HISTORY_POINTS = 500          # ~5 days at 15-min intervals
MIN_POINTS_FOR_INDICATORS = 14    # RSI(14) minimum

CATEGORIES_TO_TRACK = [
    "privacy-coins",
    "decentralized-exchange",
    "liquid-staking-tokens",
    "layer-1",
    "meme-token",
    "real-world-assets-rwa",
    "artificial-intelligence",
    "yield-farming",
]

FLAG_24H_PCT = 8.0
FLAG_7D_PCT = 20.0
FLAG_REVERSAL_24H = 5.0
FLAG_REVERSAL_7D = 5.0
UNUSUAL_VOLUME_MULTIPLE = 2.5
EXCESS_VS_BTC_PCT = 10.0


def fetch_json(url: str, params: dict) -> list:
    full_url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full_url, headers={"User-Agent": "investment-radar/4.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def fetch_top(per_page=PER_PAGE, pages=PAGES) -> list:
    out = []
    for page in range(1, pages + 1):
        params = {
            "vs_currency": VS_CURRENCY,
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
            "price_change_percentage": "24h,7d",
        }
        out.extend(fetch_json(BASE_URL, params))
        time.sleep(1.5)
    return out


def fetch_watchlist(ids: list) -> list:
    if not ids:
        return []
    params = {
        "vs_currency": VS_CURRENCY,
        "ids": ",".join(ids),
        "price_change_percentage": "24h,7d",
    }
    return fetch_json(BASE_URL, params)


def merge_unique(*lists) -> list:
    seen = {}
    for lst in lists:
        for coin in lst:
            seen[coin["id"]] = coin
    return list(seen.values())


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def update_history(history: dict, coin: dict, timestamp: str, always_track: set) -> None:
    cid = coin["id"]
    should_track = (
        cid in always_track
        or cid in history
        or coin.get("_will_flag", False)
    )
    if not should_track:
        return
    points = history.setdefault(cid, [])
    points.append({
        "t": timestamp,
        "price": coin.get("current_price"),
        "high_24h": coin.get("high_24h"),
        "low_24h": coin.get("low_24h"),
        "volume": coin.get("total_volume"),
    })
    if len(points) > MAX_HISTORY_POINTS:
        del points[: len(points) - MAX_HISTORY_POINTS]


def compute_rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_ema(prices: list, period: int) -> float:
    if len(prices) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 8)


def compute_volume_baseline(points: list) -> float:
    vols = [p["volume"] for p in points if p.get("volume") is not None]
    if len(vols) < 4:
        return None
    return sum(vols) / len(vols)


def build_indicators(history: dict) -> dict:
    result = {}
    for cid, points in history.items():
        if len(points) < MIN_POINTS_FOR_INDICATORS:
            continue
        prices = [p["price"] for p in points if p.get("price") is not None]
        result[cid] = {
            "rsi14": compute_rsi(prices, 14),
            "ema9": compute_ema(prices, 9),
            "ema21": compute_ema(prices, 21),
            "volume_baseline_avg": compute_volume_baseline(points),
            "n_points": len(points),
            "note": "approximated from 15-min snapshots (~3.5h window for RSI14), not true candles",
        }
    return result


def categories_stale(existing: dict, max_age_hours: float = 20.0) -> bool:
    updated_at = existing.get("updated_at")
    if not updated_at:
        return True
    try:
        last = datetime.fromisoformat(updated_at)
    except ValueError:
        return True
    age_hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return age_hours >= max_age_hours


def fetch_categories() -> dict:
    mapping = {}
    for category in CATEGORIES_TO_TRACK:
        params = {
            "vs_currency": VS_CURRENCY,
            "category": category,
            "order": "market_cap_desc",
            "per_page": 50,
            "page": 1,
        }
        try:
            coins = fetch_json(CATEGORY_URL, params)
            mapping[category] = [c["id"] for c in coins]
        except Exception as exc:  # noqa: BLE001
            mapping[category] = {"error": str(exc)}
        time.sleep(1.5)
    return mapping


def compute_flags(coin: dict, volume_baseline: float, btc_chg24: float, btc_chg7d: float) -> list:
    flags = []
    chg24 = coin.get("price_change_percentage_24h_in_currency")
    chg7d = coin.get("price_change_percentage_7d_in_currency")
    vol = coin.get("total_volume") or 0

    reversal_added = False
    if chg24 is not None and chg7d is not None:
        if chg24 >= FLAG_REVERSAL_24H and chg7d <= -FLAG_REVERSAL_7D:
            flags.append(f"انعكاس صاعد محتمل ({chg24:.1f}% خلال 24 ساعة بعد أسبوع {chg7d:.1f}%)")
            reversal_added = True
        elif chg24 <= -FLAG_REVERSAL_24H and chg7d >= FLAG_REVERSAL_7D:
            flags.append(f"انعكاس هابط محتمل ({chg24:.1f}% خلال 24 ساعة بعد أسبوع {chg7d:.1f}%)")
            reversal_added = True

    if not reversal_added and chg24 is not None and abs(chg24) >= FLAG_24H_PCT:
        flags.append(f"حركة سعرية حادة خلال 24 ساعة ({chg24:.1f}%)")

    if chg7d is not None and abs(chg7d) >= FLAG_7D_PCT:
        flags.append(f"حركة سعرية حادة خلال 7 أيام ({chg7d:.1f}%)")

    if volume_baseline and volume_baseline > 0 and vol / volume_baseline >= UNUSUAL_VOLUME_MULTIPLE:
        flags.append(
            f"نشاط تداول غير عادي مقارنة بمتوسط العملة نفسها (x{vol / volume_baseline:.1f})"
        )
    elif volume_baseline is None:
        mcap = coin.get("market_cap") or 0
        if mcap and vol / mcap >= 0.5:
            flags.append(f"نشاط تداول غير عادي نسبة لحجم السوق (Vol/MCap={vol / mcap:.2f}) [بدون خط أساس بعد]")

    if chg24 is not None and btc_chg24 is not None:
        excess = chg24 - btc_chg24
        if abs(excess) >= EXCESS_VS_BTC_PCT and excess > 0:
            flags.append(
                f"تفوق واضح على BTC خلال 24 ساعة ({chg24:.1f}% مقابل BTC {btc_chg24:.1f}%) — "
                f"حركة مش مجرد بيتا عام للسوق"
            )

    return flags


def build_record(coin: dict, flags: list, indicators: dict, btc_chg24: float) -> dict:
    chg24 = coin.get("price_change_percentage_24h_in_currency")
    record = {
        "id": coin["id"],
        "symbol": coin["symbol"].upper(),
        "name": coin["name"],
        "price_usd": coin.get("current_price"),
        "high_24h_usd": coin.get("high_24h"),
        "low_24h_usd": coin.get("low_24h"),
        "change_24h_pct": chg24,
        "change_7d_pct": coin.get("price_change_percentage_7d_in_currency"),
        "volume_24h_usd": coin.get("total_volume"),
        "market_cap_usd": coin.get("market_cap"),
        "market_cap_rank": coin.get("market_cap_rank"),
        "flags": flags,
    }
    if chg24 is not None and btc_chg24 is not None:
        record["excess_return_24h_vs_btc_pct"] = round(chg24 - btc_chg24, 2)
    ind = indicators.get(coin["id"])
    if ind:
        record["indicators"] = ind
    return record


def compute_category_clusters(flagged_coins: list, categories: dict) -> dict:
    """For each tracked category, list which flagged coins (by symbol) belong
    to it. Only categories with 2+ flagged members are returned - a single
    flagged coin in a category isn't a cluster."""
    coin_id_to_symbol = {c["id"]: c["symbol"] for c in flagged_coins}
    clusters = {}
    for category, ids in categories.items():
        if not isinstance(ids, list):
            continue  # skip categories that errored during fetch
        members = [coin_id_to_symbol[cid] for cid in ids if cid in coin_id_to_symbol]
        if len(members) >= 2:
            clusters[category] = members
    return clusters


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    watchlist_ids = []
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        watchlist_ids = cfg.get("always_include", [])

    top_coins = fetch_top()
    watchlist_coins = fetch_watchlist(watchlist_ids)
    all_coins = merge_unique(top_coins, watchlist_coins)

    btc = next((c for c in all_coins if c["id"] == "bitcoin"), None)
    btc_chg24 = btc.get("price_change_percentage_24h_in_currency") if btc else None
    btc_chg7d = btc.get("price_change_percentage_7d_in_currency") if btc else None

    for coin in all_coins:
        prelim_flags = compute_flags(coin, volume_baseline=None, btc_chg24=btc_chg24, btc_chg7d=btc_chg7d)
        coin["_will_flag"] = bool(prelim_flags)

    history = load_json(HISTORY_PATH, {})
    timestamp = datetime.now(timezone.utc).isoformat()
    always_track = set(watchlist_ids)
    for coin in all_coins:
        update_history(history, coin, timestamp, always_track)
    HISTORY_PATH.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")

    indicators = build_indicators(history)
    INDICATORS_PATH.write_text(json.dumps(indicators, ensure_ascii=False, indent=2), encoding="utf-8")

    existing_categories = load_json(CATEGORIES_PATH, {})
    if categories_stale(existing_categories):
        cat_mapping = fetch_categories()
        categories_out = {"updated_at": timestamp, "categories": cat_mapping}
        CATEGORIES_PATH.write_text(json.dumps(categories_out, ensure_ascii=False, indent=2), encoding="utf-8")
        current_categories = cat_mapping
    else:
        current_categories = existing_categories.get("categories", {})

    records = []
    for coin in all_coins:
        points = history.get(coin["id"], [])
        baseline = compute_volume_baseline(points) if len(points) >= 4 else None
        flags = compute_flags(coin, baseline, btc_chg24, btc_chg7d)
        records.append(build_record(coin, flags, indicators, btc_chg24))

    # market breadth - what fraction of the scanned universe is green right now
    with_change = [r for r in records if r.get("change_24h_pct") is not None]
    green = [r for r in with_change if r["change_24h_pct"] > 0]
    market_breadth_pct_green = round(len(green) / len(with_change) * 100, 1) if with_change else None

    full_snapshot = {
        "updated_at": timestamp,
        "count": len(records),
        "market_breadth_pct_green": market_breadth_pct_green,
        "coins": records,
    }
    (DATA_DIR / "market-scan.json").write_text(
        json.dumps(full_snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    flagged = [r for r in records if r["flags"]]
    flagged.sort(key=lambda r: len(r["flags"]), reverse=True)
    top_flagged = flagged[:40]

    category_clusters = compute_category_clusters(top_flagged, current_categories)

    radar_flags = {
        "updated_at": timestamp,
        "count": len(top_flagged),
        "market_breadth_pct_green": market_breadth_pct_green,
        "category_clusters": category_clusters,
        "coins": top_flagged,
    }
    (DATA_DIR / "radar-flags.json").write_text(
        json.dumps(radar_flags, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Scanned {len(records)} coins, {len(flagged)} flagged (saved top {len(top_flagged)}), "
          f"breadth={market_breadth_pct_green}% green, {len(category_clusters)} category clusters, "
          f"{len(indicators)} with computed indicators, history for {len(history)} coins.")


if __name__ == "__main__":
    main()
