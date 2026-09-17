"""
Investment Radar - CoinGecko Market Scanner
--------------------------------------------
Runs periodically via GitHub Actions (server-side, no API key needed).
Writes two JSON files into data/:

  - market-scan.json : full raw snapshot (top 250 by market cap + always-include watchlist)
  - radar-flags.json : curated shortlist of coins showing notable activity right now

Claude reads radar-flags.json (small, fast) when asked to "run the radar",
and can fall back to market-scan.json for the full picture if needed.
"""
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

BASE_URL = "https://api.coingecko.com/api/v3/coins/markets"
VS_CURRENCY = "usd"
PER_PAGE = 250          # CoinGecko max per page
PAGES = 1               # 1 page = top 250 by market cap (raise to 2 for top 500, etc.)
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "watchlist.json"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# --- Flag thresholds: tune these over time based on what turns out useful ---
FLAG_24H_PCT = 8.0          # |24h change| %
FLAG_7D_PCT = 20.0          # |7d change| %
FLAG_VOL_MCAP_RATIO = 0.5   # 24h volume / market cap (unusually high turnover)
FLAG_REVERSAL_24H = 5.0     # used together with FLAG_REVERSAL_7D for reversal detection
FLAG_REVERSAL_7D = 5.0


def fetch_markets(params: dict) -> list:
    url = BASE_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "investment-radar/1.0"})
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
        out.extend(fetch_markets(params))
        time.sleep(1.5)  # stay well under the free-tier rate limit
    return out


def fetch_watchlist(ids: list) -> list:
    """Explicitly fetch coins that must always be tracked, even if outside top 250 (e.g. PAXG)."""
    if not ids:
        return []
    params = {
        "vs_currency": VS_CURRENCY,
        "ids": ",".join(ids),
        "price_change_percentage": "24h,7d",
    }
    return fetch_markets(params)


def merge_unique(*lists) -> list:
    seen = {}
    for lst in lists:
        for coin in lst:
            seen[coin["id"]] = coin
    return list(seen.values())


def compute_flags(coin: dict) -> list:
    flags = []
    chg24 = coin.get("price_change_percentage_24h_in_currency")
    chg7d = coin.get("price_change_percentage_7d_in_currency")
    vol = coin.get("total_volume") or 0
    mcap = coin.get("market_cap") or 0

    if chg24 is not None and abs(chg24) >= FLAG_24H_PCT:
        flags.append(f"حركة سعرية حادة خلال 24 ساعة ({chg24:.1f}%)")

    if chg7d is not None and abs(chg7d) >= FLAG_7D_PCT:
        flags.append(f"حركة سعرية حادة خلال 7 أيام ({chg7d:.1f}%)")

    if mcap and vol / mcap >= FLAG_VOL_MCAP_RATIO:
        flags.append(f"نشاط تداول غير عادي نسبة لحجم السوق (Vol/MCap={vol / mcap:.2f})")

    if chg24 is not None and chg7d is not None:
        if chg24 >= FLAG_REVERSAL_24H and chg7d <= -FLAG_REVERSAL_7D:
            flags.append("انعكاس صاعد محتمل (24h موجب بعد أسبوع سالب)")
        elif chg24 <= -FLAG_REVERSAL_24H and chg7d >= FLAG_REVERSAL_7D:
            flags.append("انعكاس هابط محتمل (24h سالب بعد أسبوع موجب)")

    return flags


def build_record(coin: dict, flags: list) -> dict:
    return {
        "id": coin["id"],
        "symbol": coin["symbol"].upper(),
        "name": coin["name"],
        "price_usd": coin.get("current_price"),
        "change_24h_pct": coin.get("price_change_percentage_24h_in_currency"),
        "change_7d_pct": coin.get("price_change_percentage_7d_in_currency"),
        "volume_24h_usd": coin.get("total_volume"),
        "market_cap_usd": coin.get("market_cap"),
        "market_cap_rank": coin.get("market_cap_rank"),
        "flags": flags,
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    watchlist_ids = []
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        watchlist_ids = cfg.get("always_include", [])

    top_coins = fetch_top()
    watchlist_coins = fetch_watchlist(watchlist_ids)
    all_coins = merge_unique(top_coins, watchlist_coins)

    records = [build_record(c, compute_flags(c)) for c in all_coins]
    timestamp = datetime.now(timezone.utc).isoformat()

    full_snapshot = {"updated_at": timestamp, "count": len(records), "coins": records}
    (DATA_DIR / "market-scan.json").write_text(
        json.dumps(full_snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    flagged = [r for r in records if r["flags"]]
    flagged.sort(key=lambda r: len(r["flags"]), reverse=True)

    radar_flags = {"updated_at": timestamp, "count": len(flagged), "coins": flagged[:40]}
    (DATA_DIR / "radar-flags.json").write_text(
        json.dumps(radar_flags, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"Scanned {len(records)} coins, {len(flagged)} flagged.")


if __name__ == "__main__":
    main()
