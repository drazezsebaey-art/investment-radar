"""
Investment Radar - Trade Tracker (v16)
---------------------------------------
v16 addition (from the 22/9/2026 monitoring audit): stop/target checks
previously relied entirely on data/price-history.json's 15-min-ish point
SAMPLES - a real touch between two samples could be missed if price
reverted before the next sample. This adds a real-OHLC layer: OKX's public
spot candles endpoint (confirmed reachable from GitHub Actions runners via
the same exchange already used for OI/funding in breakout_check.py -
Binance direct returns 451/geo-blocked, Bybit returns 403) gives TRUE
high/low over the queried window, not a sample. For each run, one OKX
fetch per unique symbol covers every trade on that coin across all three
tracks (real/shadow/scalp), each trade then filters that shared candle set
to its own fill/order time. Falls through to the pre-existing
price-history.json sampling (still useful for coins OKX doesn't list) and
finally to a single current-spot-price fallback - exactly the same
fallback chain as before, with a real-candle layer added on top. Every
check now records which source was actually used (last_check_source) so
coverage stays auditable.

v4 additions: Profit Factor (gross profit / gross loss, using summed % return
per trade as a proxy for dollar P&L since position sizing isn't tracked) and
Recovery Factor (net profit / max drawdown, both computed from a compounded
equity curve built by sorting closed trades chronologically) - standard
metrics from live-trading-performance literature, added alongside win rate
for a more complete picture of risk-adjusted performance.

CRITICAL FIX from v3: pending-order fills were checked against the coin's
rolling 24h low (low_24h_usd from market-scan.json), which looks backward
24 hours from THE MOMENT OF THE CHECK - not from when the order was placed.
A price dip that happened BEFORE the pending order even existed could
therefore be wrongly counted as "the market came down and filled my order
after I placed it." This is a real, serious bug: it can report a fill that
never actually happened in the order's real lifetime.

Fix: pending fills are now checked against data/price-history.json (our own
timestamped 15-minute snapshots), filtered to ONLY points recorded strictly
AFTER the order's created_at timestamp. The first run that sees a new
pending trade (no created_at yet) just stamps created_at = now and does NOT
fill it that same run - fill detection only begins from snapshots taken
after that stamp, so no pre-existing price action can count.

Trade-off: precision is limited to the ~15-minute snapshot interval (the
same approximation already disclosed everywhere else in this system), and
a coin needs to already be accumulating history (flagged before, or in the
watchlist) for this to work - if data/price-history.json has no entries yet
for that coin, the pending order simply won't fill until history starts
accumulating for it (which happens automatically the moment it's flagged).
v16 note: this whole trade-off is exactly what the OKX real-candle layer
above now fixes for any coin OKX lists - the price-history fallback below
still exists for coins it doesn't.

v3 features preserved: multiple targets with targets_hit accumulation,
stopped_after_partial_targets classification, old single target_low trades
still supported.
"""
import json
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
TRADES_PATH = BASE_DIR / "config" / "trades.json"
SCAN_PATH = BASE_DIR / "data" / "market-scan.json"
HISTORY_PATH = BASE_DIR / "data" / "price-history.json"
SUMMARY_PATH = BASE_DIR / "data" / "performance-summary.json"

# --- v16: OKX real-candle layer -------------------------------------------
OKX_MARKET_API_BASE = "https://www.okx.com/api/v5/market"
OKX_CANDLE_BAR = "5m"          # true high/low per bar is accurate regardless of bar size, as long as
                                 # bars fully cover the window - 5m keeps limit=300 covering ~25h, comfortably
                                 # more than any realistic gap between runs, while keeping hit-timestamps precise
OKX_CANDLE_LIMIT = 300
OKX_REQUEST_TIMEOUT = 15
OKX_POLITE_DELAY = 0.3          # seconds between per-symbol OKX calls - public market data has a generous
                                 # rate limit, this just avoids hammering it needlessly


def fetch_okx_candle_rows(symbol: str, since_iso: str):
    """Fetches OKX spot candles for {symbol}-USDT strictly newer than
    since_iso, as raw (ts_ms, high, low) tuples sorted oldest-first, or
    None if the instrument doesn't exist on OKX or the request fails for
    any reason (never let one bad symbol break the run - callers fall back
    to price-history.json sampling)."""
    if not symbol:
        return None
    try:
        since_ms = int(datetime.fromisoformat(since_iso).timestamp() * 1000)
    except (ValueError, TypeError):
        return None
    inst_id = f"{symbol.upper()}-USDT"
    params = {"instId": inst_id, "bar": OKX_CANDLE_BAR, "before": since_ms, "limit": OKX_CANDLE_LIMIT}
    url = f"{OKX_MARKET_API_BASE}/candles?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "investment-radar/1.0"})
        with urllib.request.urlopen(req, timeout=OKX_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        rows = data.get("data") or []
        if not rows:
            return None
        # OKX candle row shape: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
        parsed = [(int(r[0]), float(r[2]), float(r[3])) for r in rows]
        parsed.sort(key=lambda r: r[0])
        return parsed
    except Exception as exc:  # noqa: BLE001 - one bad symbol must never kill the run
        print(f"  [diagnostic] OKX candle fetch failed for {symbol}: {type(exc).__name__}: {exc}")
        return None


def okx_range_since(cache_rows, since_iso: str):
    """Filters a symbol's cached OKX candle rows down to those at/after
    since_iso and returns the TRUE {high, high_at, low, low_at} over that
    subset, or None if there's nothing to filter (no cache entry, or every
    row predates since_iso)."""
    if not cache_rows or not since_iso:
        return None
    try:
        since_ms = int(datetime.fromisoformat(since_iso).timestamp() * 1000)
    except (ValueError, TypeError):
        return None
    matching = [r for r in cache_rows if r[0] >= since_ms]
    if not matching:
        return None
    high_row = max(matching, key=lambda r: r[1])
    low_row = min(matching, key=lambda r: r[2])
    return {
        "high": high_row[1],
        "high_at": datetime.fromtimestamp(high_row[0] / 1000, tz=timezone.utc).isoformat(),
        "low": low_row[2],
        "low_at": datetime.fromtimestamp(low_row[0] / 1000, tz=timezone.utc).isoformat(),
    }


def trade_symbol(trade: dict):
    """Best-effort symbol for an OKX lookup - most trades have `symbol`
    directly; a few older manually-added trades (pre-dating that
    convention) only have `asset_id`, so fall back to its uppercase form."""
    return trade.get("symbol") or (trade.get("asset_id", "").upper() or None)


def collect_needed_symbols(all_trade_lists) -> dict:
    """Maps symbol -> earliest timestamp needed across every open/pending
    trade in ALL THREE tracks (real, shadow, scalp) combined, so a coin
    that appears in more than one track (very common - the same signal
    often ends up in all three) is fetched from OKX ONCE per run, not once
    per trade."""
    needed = {}
    for trades in all_trade_lists:
        for t in trades:
            symbol = trade_symbol(t)
            if not symbol:
                continue
            status = t.get("status")
            if status == "pending":
                since = t.get("created_at")
            elif status == "open":
                since = t.get("filled_at") or t.get("created_at") or t.get("date_opened")
            else:
                continue
            if not since:
                continue
            if symbol not in needed or since < needed[symbol]:
                needed[symbol] = since
    return needed


def build_okx_cache(all_trade_lists) -> dict:
    needed = collect_needed_symbols(all_trade_lists)
    cache = {}
    for symbol, since in needed.items():
        cache[symbol] = fetch_okx_candle_rows(symbol, since)
        time.sleep(OKX_POLITE_DELAY)
    n_hit = sum(1 for v in cache.values() if v is not None)
    print(f"OKX candle cache: {n_hit}/{len(cache)} symbols have real OHLC data this run "
          f"(the rest fall back to price-history.json sampling).")
    return cache


def load_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def build_price_lookup(scan: dict) -> dict:
    return {coin["id"]: coin for coin in scan.get("coins", [])}


def get_targets(trade: dict) -> list:
    targets = trade.get("targets")
    if targets:
        return sorted(targets)
    if trade.get("target_low") is not None:
        return [trade["target_low"]]
    return []


def check_pending(trade: dict, price_history: dict, okx_cache: dict) -> bool:
    """v16: tries the shared OKX candle cache first (real high/low since the
    order was placed - see module docstring), falls back to the pre-existing
    price-history.json sampling when OKX has nothing for this symbol.

    Returns True if the trade was filled this run.
    """
    entry = trade.get("entry")
    if entry is None:
        return False

    now_iso = datetime.now(timezone.utc).isoformat()

    if "created_at" not in trade:
        # First time we've seen this pending order - stamp it now and stop.
        # We deliberately do NOT check for a fill on this same run: doing so
        # would risk using a snapshot from the very same 15-min bucket that
        # predates our own knowledge of the order, recreating the same class
        # of bug this fix exists to close. Fill-checking starts next run.
        trade["created_at"] = now_iso
        return False

    symbol = trade_symbol(trade)
    okx_result = okx_range_since(okx_cache.get(symbol), trade["created_at"]) if symbol else None
    if okx_result:
        trade["last_check_source"] = "okx_candles"
        if okx_result["low"] <= entry:
            trade["status"] = "open"
            trade["filled_at"] = okx_result["low_at"]
            trade["actual_entry"] = entry
            return True
        return False

    points = price_history.get(trade["asset_id"], [])
    post_order_points = [
        p for p in points
        if p.get("t") and p.get("t") > trade["created_at"] and p.get("price") is not None
    ]
    trade["last_check_source"] = "price_history_snapshots" if post_order_points else "no_data_yet"
    hit = next((p for p in post_order_points if p["price"] <= entry), None)
    if hit:
        trade["status"] = "open"
        trade["filled_at"] = hit["t"]
        trade["actual_entry"] = entry
        return True
    return False


def check_open(trade: dict, coin: dict, price_history: dict, okx_cache: dict) -> None:
    """v16: tries the shared OKX candle cache first for the TRUE high/low
    since fill (see module docstring) - this is what actually closes the
    gap the 22/9/2026 audit found (freshly-filled trades with only 0-1
    price-history snapshots so far). Falls back to the v5 sampling fix
    below when OKX has nothing for this symbol, and finally to a single
    current-spot-price reading when there's no post-fill data at all yet -
    same fallback chain as before, OKX layered on top.

    v5 FIX (caught by Azez, 2026-09-20) preserved: the sampling fallback
    only ever uses price-history.json points recorded strictly after the
    trade's own fill time - never the coin's rolling 24h high/low, which is
    the bug class this whole function exists to avoid."""
    stop = trade.get("stop")
    targets = get_targets(trade)
    now = datetime.now(timezone.utc).isoformat()

    since = trade.get("filled_at") or trade.get("created_at") or trade.get("date_opened")
    symbol = trade_symbol(trade)

    okx_result = okx_range_since(okx_cache.get(symbol), since) if symbol else None
    if okx_result:
        low, high = okx_result["low"], okx_result["high"]
        trade["last_check_source"] = "okx_candles"
    else:
        points = price_history.get(trade["asset_id"], [])
        post_fill_points = [
            p for p in points
            if p.get("t") and since and p.get("t") > since and p.get("price") is not None
        ]
        if post_fill_points:
            low = min(p["price"] for p in post_fill_points)
            high = max(p["price"] for p in post_fill_points)
            trade["last_check_source"] = "price_history_snapshots"
        else:
            # No snapshot recorded yet strictly after the fill - use only the
            # coin's current spot price, not the rolling 24h window.
            current = coin.get("price_usd")
            low = high = current
            trade["last_check_source"] = "current_spot_fallback"

    trade.setdefault("targets_hit", [])
    already_hit = {t["target"] for t in trade["targets_hit"]}

    newly_hit = []
    if high is not None:
        for t in targets:
            if t not in already_hit and high >= t:
                newly_hit.append(t)

    for t in newly_hit:
        trade["targets_hit"].append({"target": t, "hit_at": now})

    stop_hit = stop is not None and low is not None and low <= stop
    final_target = targets[-1] if targets else None
    final_target_hit = final_target is not None and (
        final_target in already_hit or final_target in newly_hit
    )

    if stop_hit and newly_hit:
        trade["status"] = "stopped_after_partial_targets" if trade["targets_hit"] else "stopped"
        trade["exit_price"] = stop
        trade["date_closed"] = now
        trade["note"] = (
            "⚠️ تعارض: الستوب وهدف جديد الاتنين ظهروا متلمسين في نفس نافذة الفحص — "
            "معتبرينها ستوب كافتراض متحفظ، الترتيب الفعلي مش مؤكد من البيانات دي."
        )
    elif stop_hit:
        trade["status"] = "stopped_after_partial_targets" if trade["targets_hit"] else "stopped"
        trade["exit_price"] = stop
        trade["date_closed"] = now
    elif final_target_hit:
        trade["status"] = "closed_targets_complete"
        trade["exit_price"] = final_target
        trade["date_closed"] = now


def pct_return(trade: dict) -> float:
    entry = trade.get("actual_entry", trade.get("entry"))
    exit_price = trade.get("exit_price")
    if entry is None or exit_price is None or entry == 0:
        return None
    return round((exit_price - entry) / entry * 100, 2)


# --- v49 (24/9/2026): Cost Model, per the audit report's objection that ---
# performance numbers with zero fees/slippage/latency are structurally
# optimistic. Estimates only - real spot taker fees and slippage vary by
# exchange, pair liquidity, and order size, but a flat estimate applied
# consistently is far more honest than assuming zero cost. Applied as a
# ROUND-TRIP drag (entry + exit, each paying fee + slippage once) directly
# on the trade's realized % return - status (win/loss) itself still comes
# from whether the STRATEGY correctly hit its target vs stop, not from
# cost; a target-hit trade can still show a smaller (or even negative) net
# return once costs are subtracted, which is exactly the honest signal
# this was missing before.
FEE_BPS = 10        # ~0.10% - typical spot taker fee on major exchanges (Binance/OKX)
SLIPPAGE_BPS = 5     # ~0.05% - conservative estimate for a liquid pair at modest paper-trade size
ROUND_TRIP_COST_PCT = round(2 * (FEE_BPS + SLIPPAGE_BPS) / 100, 3)  # both sides pay both costs once


def net_pct_return(trade: dict) -> float:
    gross = pct_return(trade)
    if gross is None:
        return None
    return round(gross - ROUND_TRIP_COST_PCT, 2)


CLOSED_STATUSES = ("closed_targets_complete", "stopped", "stopped_after_partial_targets", "target_hit")


def build_equity_curve(subset: list) -> list:
    """Sort by close date and compound returns into an equity curve starting
    at 100, for max-drawdown and recovery-factor calculation. Uses NET (of
    estimated cost) % return per trade (no position-sizing data available),
    so this is a proxy for a real equity curve, not a dollar-accurate one."""
    dated = [t for t in subset if t.get("date_closed") and net_pct_return(t) is not None]
    dated.sort(key=lambda t: t["date_closed"])
    equity = 100.0
    curve = [equity]
    for t in dated:
        equity *= (1 + net_pct_return(t) / 100)
        curve.append(equity)
    return curve


def compute_max_drawdown_pct(curve: list) -> float:
    if len(curve) < 2:
        return None
    peak = curve[0]
    max_dd = 0.0
    for v in curve[1:]:
        peak = max(peak, v)
        dd = (peak - v) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
    return round(max_dd, 2)


def stats_for(subset: list) -> dict:
    n = len(subset)
    wins = [t for t in subset if t.get("status") in ("closed_targets_complete", "target_hit")]
    partial = [t for t in subset if t.get("status") == "stopped_after_partial_targets"]
    losses = [t for t in subset if t.get("status") == "stopped"]
    returns_gross = [pct_return(t) for t in subset if pct_return(t) is not None]
    returns_net = [net_pct_return(t) for t in subset if net_pct_return(t) is not None]
    win_returns_net = [net_pct_return(t) for t in wins if net_pct_return(t) is not None]
    loss_returns_net = [net_pct_return(t) for t in (losses + partial) if net_pct_return(t) is not None]

    # Profit Factor = gross profit / gross loss, using summed NET % returns
    # per trade as a proxy for dollar P&L (no position-sizing data tracked)
    gross_profit = sum(r for r in returns_net if r > 0)
    gross_loss = abs(sum(r for r in returns_net if r < 0))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else None

    # Recovery Factor = net profit / max drawdown, both from a compounded
    # equity curve built by sorting closed trades chronologically
    curve = build_equity_curve(subset)
    max_dd = compute_max_drawdown_pct(curve)
    net_profit_pct = round(curve[-1] - 100, 2) if curve else None
    recovery_factor = round(net_profit_pct / max_dd, 2) if (max_dd and max_dd > 0) else None

    return {
        "n_closed": n,
        "wins": len(wins),
        "losses": len(losses),
        "stopped_after_partial_targets": len(partial),
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else None,
        # v49: "avg_return_pct" now means NET of estimated cost (the honest,
        # headline number) - the old gross figure is kept alongside under
        # its own explicit name so the cost drag itself stays visible.
        "avg_return_pct": round(sum(returns_net) / len(returns_net), 2) if returns_net else None,
        "avg_return_pct_gross": round(sum(returns_gross) / len(returns_gross), 2) if returns_gross else None,
        "estimated_round_trip_cost_pct": ROUND_TRIP_COST_PCT,
        "avg_win_pct": round(sum(win_returns_net) / len(win_returns_net), 2) if win_returns_net else None,
        "avg_loss_pct": round(sum(loss_returns_net) / len(loss_returns_net), 2) if loss_returns_net else None,
        "profit_factor": profit_factor,
        "net_profit_pct_compounded": net_profit_pct,
        "max_drawdown_pct": max_dd,
        "recovery_factor": recovery_factor,
    }


def summarize(trades: list) -> dict:
    closed = [t for t in trades if t.get("status") in CLOSED_STATUSES]


    return {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "overall": stats_for(closed),
        "real_only": stats_for([t for t in closed if t.get("type") == "real"]),
        "paper_only": stats_for([t for t in closed if t.get("type") == "paper"]),
        "note": "n_closed under ~20-30 is not statistically meaningful yet — treat as directional only.",
    }


SHADOW_TRADES_PATH = BASE_DIR / "data" / "shadow-trades.json"
SHADOW_SUMMARY_PATH = BASE_DIR / "data" / "shadow-performance-summary.json"
SCALP_TRADES_PATH = BASE_DIR / "data" / "scalp-trades.json"
SCALP_SUMMARY_PATH = BASE_DIR / "data" / "scalp-performance-summary.json"


def process_trades(trades: list, lookup: dict, price_history: dict, okx_cache: dict) -> tuple:
    """v9: pulled out of main() so the exact same fill/stop/target logic can
    run over data/shadow-trades.json too (signals that fired but didn't
    clear AUTO_TRADE_MIN_SCORE) without duplicating it - shadow trades are
    tracked with identical rigor, just written to a separate summary file
    that never mixes into the real performance-summary.json."""
    filled = 0
    changed = 0
    for trade in trades:
        if trade.get("status") == "pending":
            if check_pending(trade, price_history, okx_cache):
                filled += 1
            continue

        if trade.get("status") != "open":
            continue

        coin = lookup.get(trade.get("asset_id"))
        if coin is None:
            continue

        before = trade.get("status")
        n_targets_before = len(trade.get("targets_hit", []))
        check_open(trade, coin, price_history, okx_cache)
        if trade.get("status") != before or len(trade.get("targets_hit", [])) != n_targets_before:
            changed += 1
    return filled, changed


def main():
    data = load_json(TRADES_PATH, {"trades": []})
    scan = load_json(SCAN_PATH, {"coins": []})
    price_history = load_json(HISTORY_PATH, {})
    lookup = build_price_lookup(scan)

    shadow_data = load_json(SHADOW_TRADES_PATH, {"trades": []})
    scalp_data = load_json(SCALP_TRADES_PATH, {"trades": []})

    # v16: one OKX fetch per unique symbol across ALL THREE tracks combined,
    # built once up front - a coin open in real+shadow+scalp simultaneously
    # (common) still only costs one OKX call, not three.
    okx_cache = build_okx_cache([
        data.get("trades", []),
        shadow_data.get("trades", []),
        scalp_data.get("trades", []),
    ])

    filled, changed = process_trades(data.get("trades", []), lookup, price_history, okx_cache)

    TRADES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = summarize(data.get("trades", []))
    SUMMARY_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Checked {len(data.get('trades', []))} trades: {filled} pending order(s) filled, "
          f"{changed} status/target change(s) this run. "
          f"Overall win rate so far: {summary['overall']['win_rate_pct']}")

    # v9: shadow trades - same logic, separate file, never touches the real summary above
    if shadow_data.get("trades"):
        s_filled, s_changed = process_trades(shadow_data["trades"], lookup, price_history, okx_cache)
        SHADOW_TRADES_PATH.write_text(json.dumps(shadow_data, ensure_ascii=False, indent=2), encoding="utf-8")
        shadow_summary = summarize(shadow_data.get("trades", []))
        shadow_summary["note"] = (
            "These are signals that fired but did NOT clear AUTO_TRADE_MIN_SCORE, tracked with the "
            "same rigor as real paper trades purely to answer 'what would have happened if we'd said "
            "yes anyway' - never counted toward the real performance-summary.json above. " + shadow_summary["note"]
        )
        SHADOW_SUMMARY_PATH.write_text(json.dumps(shadow_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Shadow log: checked {len(shadow_data['trades'])} rejected-signal trades: "
              f"{s_filled} filled, {s_changed} changed this run.")

    # v14/v2: scalp track - same logic, separate file, separate summary,
    # never touches config/trades.json, shadow-trades.json, or SCALP's own
    # real $100 fund. This is what makes scalp-trades.json trackable to a
    # win/loss outcome instead of just a live snapshot that got overwritten
    # every run (the gap Azez caught).
    if scalp_data.get("trades"):
        sc_filled, sc_changed = process_trades(scalp_data["trades"], lookup, price_history, okx_cache)
        SCALP_TRADES_PATH.write_text(json.dumps(scalp_data, ensure_ascii=False, indent=2), encoding="utf-8")
        scalp_summary = summarize(scalp_data.get("trades", []))
        scalp_summary["note"] = (
            "Scalp/momentum data-collection track (tight ATR-based stops, lower score threshold "
            "than the main radar) - never counted toward the real performance-summary.json above "
            "and separate from SCALP's own real $100 fund, which still requires the full Agent "
            "Room debate before any real trade. " + scalp_summary["note"]
        )
        SCALP_SUMMARY_PATH.write_text(json.dumps(scalp_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Scalp track: checked {len(scalp_data['trades'])} trades: "
              f"{sc_filled} filled, {sc_changed} changed this run.")


if __name__ == "__main__":
    main()
