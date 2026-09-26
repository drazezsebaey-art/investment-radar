"""
Exposure / Correlation Checker
------------------------------------------------------------
Per the 24/9/2026 audit report (item 16-17): the system's 4 tracks can each
open a position on a different coin from the same market theme (e.g. SOL +
JUP + RAY + BONK, all "Solana ecosystem beta") and treat them as 4
independent observations, when they may really be one market move counted
four times. This groups currently-OPEN positions across all 4 tracks
(real/shadow/scalp/V2) by sector cluster and by BTC-correlation, and
reports an "effective independent bets" estimate instead of a raw position
count.

Known limitation (documented, not hidden): category_clusters (produced by
scan.py) only covers coins that were BOTH in a shared CoinGecko category
AND flagged in the MOST RECENT run - a position on a coin that has since
rotated out of the flagged set won't be sector-clustered here (it counts
as its own independent exposure), though its BTC-correlation grouping
still applies if that field is available on the coin's current record. A
full historical sector taxonomy is a larger future project, not attempted
here - this is a first, honest, zero-extra-API-cost pass.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
CONFIG_DIR = BASE_DIR / "config"

RADAR_FLAGS_PATH = DATA_DIR / "radar-flags.json"
TRADES_PATH = CONFIG_DIR / "trades.json"
SHADOW_TRADES_PATH = DATA_DIR / "shadow-trades.json"
SCALP_TRADES_PATH = DATA_DIR / "scalp-trades.json"
V2_SHADOW_TRADES_PATH = DATA_DIR / "v2-shadow-trades.json"
EXPOSURE_REPORT_PATH = DATA_DIR / "exposure-report.json"

HIGH_CORRELATION_THRESHOLD = 0.7  # matches breakout_check.py's own existing beta_driven_or_cluster threshold
MIN_CLUSTER_SIZE_TO_FLAG = 3       # 3+ simultaneous open positions in the same sector gets flagged as concentrated


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def collect_open_positions() -> list:
    positions = []
    for path, track in [
        (TRADES_PATH, "real"), (SHADOW_TRADES_PATH, "shadow"),
        (SCALP_TRADES_PATH, "scalp"), (V2_SHADOW_TRADES_PATH, "v2"),
    ]:
        data = load_json(path, {"trades": []})
        for t in data.get("trades", []):
            if t.get("status") == "open":
                positions.append({"symbol": t.get("symbol"), "asset_id": t.get("asset_id"), "track": track})
    return positions


def build_sector_groups(open_positions: list, category_clusters: dict) -> dict:
    symbol_to_categories = {}
    for category, members in category_clusters.items():
        for sym in members:
            symbol_to_categories.setdefault(sym, []).append(category)

    groups = {}
    for pos in open_positions:
        cats = symbol_to_categories.get(pos["symbol"])
        if not cats:
            continue  # no known category this run - counted as independent, not forced into a bucket
        for cat in cats:
            groups.setdefault(cat, []).append(pos)
    return groups


def compute_exposure_report(open_positions: list, category_clusters: dict, coin_by_symbol: dict) -> dict:
    sector_groups = build_sector_groups(open_positions, category_clusters)
    concentrated_sectors = {
        cat: [f"{p['symbol']}({p['track']})" for p in members]
        for cat, members in sector_groups.items()
        if len(members) >= MIN_CLUSTER_SIZE_TO_FLAG
    }

    high_beta_positions = [
        p for p in open_positions
        if (coin_by_symbol.get(p["symbol"], {}).get("btc_correlation_7d") or 0) >= HIGH_CORRELATION_THRESHOLD
    ]

    symbols_in_concentrated = {s.split("(")[0] for members in concentrated_sectors.values() for s in members}
    # effective bets: each concentrated sector counts as ONE bet regardless of how many members it has;
    # every other unique symbol (not part of any concentrated sector) counts as its own independent bet
    n_effective_bets = len(concentrated_sectors) + len({
        p["symbol"] for p in open_positions if p["symbol"] not in symbols_in_concentrated
    })

    return {
        "n_total_open_positions": len(open_positions),
        "n_unique_symbols": len({p["symbol"] for p in open_positions}),
        "n_effective_independent_bets": n_effective_bets,
        "concentrated_sectors": concentrated_sectors,
        "n_high_btc_beta_positions": len(high_beta_positions),
        "high_btc_beta_symbols": sorted({p["symbol"] for p in high_beta_positions}),
    }


def main():
    radar = load_json(RADAR_FLAGS_PATH, {"coins": [], "category_clusters": {}})
    category_clusters = radar.get("category_clusters", {})
    coin_by_symbol = {c.get("symbol"): c for c in radar.get("coins", [])}

    open_positions = collect_open_positions()
    report = compute_exposure_report(open_positions, category_clusters, coin_by_symbol)
    report["generated_at"] = datetime.now(timezone.utc).isoformat()
    report["note"] = (
        "category_clusters only covers coins flagged in the MOST RECENT scan.py run - a position on "
        "a coin no longer flagged this run is counted as independent here, not a claim it truly has "
        "no sector correlation. This is a first-pass, zero-extra-API-cost exposure estimate."
    )
    EXPOSURE_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Exposure check: {report['n_total_open_positions']} open positions across "
          f"{report['n_unique_symbols']} unique symbols -> ~{report['n_effective_independent_bets']} "
          f"effective independent bets. {len(report['concentrated_sectors'])} concentrated sector(s): "
          f"{list(report['concentrated_sectors'].keys())}. "
          f"{report['n_high_btc_beta_positions']} positions are high BTC-beta (corr>={HIGH_CORRELATION_THRESHOLD}).")


if __name__ == "__main__":
    main()
