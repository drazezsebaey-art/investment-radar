"""
V2 Trade Tracker - independent performance tracking for the V2 short-swing track
------------------------------------------------------------------------------
Step 19 of the V2-merge plan (24/9/2026). Deliberately independent from
scripts/track_trades.py - its own OKX fetch, its own cache, its own output
file - matching the isolation principle applied to every other V2 piece
(v2_engine.py never touches config/trades.json, data/shadow-trades.json, or
data/scalp-trades.json; this script never touches their tracker's code or
output either, even though the underlying idea - "check price against
stop/target" - is naturally similar).

Uses 4h OKX candles (not the main tracker's 5m bars) because a V2 trade can
stay open up to two weeks (step 17's extended horizon) - 100 x 4h candles
covers ~16.7 days, comfortably more than that. Precision on the exact
touch hour is intentionally traded for that reach; V2's targets are judged
in %/day terms, not intraday timing.

Three genuinely new terminal states beyond the usual stopped/closed:
  stopped                 - hit the ATR-relative stop
  closed_targets_complete - reached every one of its feasible targets
  expired_no_resolution   - the largest target's own time horizon passed
                            without either the stop or all targets being
                            hit - neither a win nor a loss, a record that
                            this specific setup simply never resolved in
                            the window V2 itself claimed was realistic,
                            which is exactly the data step 20's learning
                            pass needs to judge V2 honestly.
"""
import json
import time
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
V2_SHADOW_TRADES_PATH = DATA_DIR / "v2-shadow-trades.json"
V2_PERFORMANCE_SUMMARY_PATH = DATA_DIR / "v2-performance-summary.json"

OKX_MARKET_API_BASE = "https://www.okx.com/api/v5/market"
OKX_CANDLE_BAR = "4H"
OKX_CANDLE_LIMIT = 100      # ~16.7 days - comfortably covers the 2-week extended horizon
OKX_REQUEST_TIMEOUT = 15
OKX_POLITE_DELAY = 0.3

# v49 (24/9/2026): Cost Model - same estimate as scripts/track_trades.py's
# own constants, duplicated rather than shared to keep V2 fully independent.
FEE_BPS = 10
SLIPPAGE_BPS = 5
ROUND_TRIP_COST_PCT = round(2 * (FEE_BPS + SLIPPAGE_BPS) / 100, 3)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_okx_candle_rows(symbol: str, since_iso: str):
    """Returns [(ts_ms, high, low), ...] sorted oldest-first since since_iso,
    or None on any failure - callers fall back to a current-spot-only check,
    same graceful-degradation convention as the main tracker."""
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
        parsed = [(int(r[0]), float(r[2]), float(r[3])) for r in rows]  # ts, high, low
        parsed.sort(key=lambda r: r[0])
        return parsed
    except Exception:  # noqa: BLE001
        return None


def fetch_current_spot(symbol: str):
    try:
        url = f"{OKX_MARKET_API_BASE}/ticker?instId={symbol.upper()}-USDT"
        req = urllib.request.Request(url, headers={"User-Agent": "investment-radar/1.0"})
        with urllib.request.urlopen(req, timeout=OKX_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        rows = data.get("data") or []
        return float(rows[0]["last"]) if rows else None
    except Exception:  # noqa: BLE001
        return None


def check_trade(trade: dict, candle_rows, current_price, now: datetime) -> dict:
    entry = trade.get("entry")
    stop = trade.get("stop")
    targets = trade.get("targets") or []
    sorted_targets = sorted(targets, key=lambda t: t["target_price"]) if targets else []

    highs = [h for _, h, _ in candle_rows] if candle_rows else []
    lows = [l for _, _, l in candle_rows] if candle_rows else []
    if current_price is not None:
        highs.append(current_price)
        lows.append(current_price)
    max_high = max(highs) if highs else None
    min_low = min(lows) if lows else None

    source = "okx_candles" if candle_rows else ("current_spot_fallback" if current_price is not None else "unavailable")

    # Stop takes priority when both a stop-touch and a target-touch could
    # theoretically fall inside the same 4h bar - matches the existing
    # system's conservative convention (never assume the friendlier order).
    if stop is not None and min_low is not None and min_low <= stop:
        trade["status"] = "stopped"
        trade["exit_price"] = stop
        trade["date_closed"] = now.isoformat()
        trade["last_check_source"] = source
        trade["last_checked_at"] = now.isoformat()
        return trade

    if sorted_targets and max_high is not None:
        already_hit_prices = {h["target_price"] for h in trade.get("targets_hit", [])}
        newly_hit = [
            {"target_price": t["target_price"], "horizon_label": t.get("horizon_label"), "hit_at": now.isoformat()}
            for t in sorted_targets
            if t["target_price"] not in already_hit_prices and max_high >= t["target_price"]
        ]
        if newly_hit:
            trade.setdefault("targets_hit", []).extend(newly_hit)

    if sorted_targets and len(trade.get("targets_hit", [])) >= len(sorted_targets):
        trade["status"] = "closed_targets_complete"
        trade["exit_price"] = sorted_targets[-1]["target_price"]
        trade["date_closed"] = now.isoformat()
    elif trade.get("status") == "open" and sorted_targets:
        try:
            opened = datetime.fromisoformat(trade["created_at"])
        except (KeyError, ValueError):
            opened = None
        if opened:
            hours_open = (now - opened).total_seconds() / 3600
            max_horizon_hours = max(t["max_hours"] for t in sorted_targets)
            if hours_open > max_horizon_hours:
                trade["status"] = "expired_no_resolution"
                trade["exit_price"] = current_price
                trade["date_closed"] = now.isoformat()

    trade["last_check_source"] = source
    trade["last_checked_at"] = now.isoformat()
    return trade


def compute_performance_summary(trades: list) -> dict:
    open_trades = [t for t in trades if t.get("status") == "open"]
    closed = [t for t in trades if t.get("status") in ("stopped", "closed_targets_complete", "expired_no_resolution")]
    wins = [t for t in closed if t.get("status") == "closed_targets_complete"]
    losses = [t for t in closed if t.get("status") == "stopped"]
    expired = [t for t in closed if t.get("status") == "expired_no_resolution"]

    # v49 (24/9/2026): Cost Model, per the audit report - same estimate as
    # scripts/track_trades.py, kept as an independent constant here rather
    # than importing from that file, matching V2's isolation principle.
    returns_gross, returns_net = [], []
    for t in closed:
        entry, exit_price = t.get("entry"), t.get("exit_price")
        if entry and exit_price is not None:
            gross = (exit_price - entry) / entry * 100
            returns_gross.append(gross)
            returns_net.append(gross - ROUND_TRIP_COST_PCT)

    return {
        "n_open": len(open_trades),
        "n_closed": len(closed),
        "wins": len(wins), "losses": len(losses), "expired_no_resolution": len(expired),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "avg_return_pct": round(sum(returns_net) / len(returns_net), 2) if returns_net else None,
        "avg_return_pct_gross": round(sum(returns_gross) / len(returns_gross), 2) if returns_gross else None,
        "estimated_round_trip_cost_pct": ROUND_TRIP_COST_PCT,
        "by_regime": by_regime_split(closed),
    }


def regime_key(trade: dict) -> str:
    """v62: bucket = market_regime_at_entry.risk_state (+ volatility_state)."""
    r = trade.get("market_regime_at_entry") or {}
    risk = r.get("risk_state") or "untagged"
    vol = r.get("volatility_state")
    return f"{risk}|{vol}" if vol else risk


def by_regime_split(closed: list) -> dict:
    """v62 (backtest-discipline audit, item 7): the same edge must be shown
    per market regime - an edge that only exists in one regime is a regime
    bet, not an edge. Trades opened before v62 carry no tag -> 'untagged'."""
    buckets = {}
    for t in closed:
        buckets.setdefault(regime_key(t), []).append(t)
    out = {}
    for k, ts in sorted(buckets.items()):
        wins = [t for t in ts if t.get("status") == "closed_targets_complete"]
        nets = []
        for t in ts:
            e, x = t.get("entry"), t.get("exit_price")
            if e and x is not None:
                nets.append((x - e) / e * 100 - ROUND_TRIP_COST_PCT)
        out[k] = {"n_closed": len(ts),
                  "win_rate_pct": round(len(wins) / len(ts) * 100, 1) if ts else None,
                  "avg_return_pct": round(sum(nets) / len(nets), 2) if nets else None,
                  "meaningful": len(ts) >= 30}
    return out


SCAN_PATH = DATA_DIR / "market-scan.json"          # v69
HISTORY_PATH = DATA_DIR / "price-history.json"     # v69


def history_rows_since(points, since_iso):
    """v69: [(ts_ms, high, low)] from our own 15-30 min price snapshots, used when
    OKX does not list the pair (CAKE, RUNE were 'unavailable' and CAKE sat below
    its stop as 'open')."""
    out = []
    for p in points or []:
        try:
            if since_iso and p.get("t") and p["t"] > since_iso and p.get("price") is not None:
                ts = int(datetime.fromisoformat(p["t"]).timestamp() * 1000)
                out.append((ts, float(p["price"]), float(p["price"])))
        except (TypeError, ValueError):
            continue
    return sorted(out) or None


def main():
    trades_data = load_json(V2_SHADOW_TRADES_PATH, {"trades": []})
    scan_prices = {c.get("id"): c.get("price_usd") for c in load_json(SCAN_PATH, {}).get("coins", [])}  # v69
    history = load_json(HISTORY_PATH, {})  # v69
    trades = trades_data.get("trades", [])
    open_trades = [t for t in trades if t.get("status") == "open"]

    now = datetime.now(timezone.utc)
    n_stopped, n_completed, n_expired = 0, 0, 0
    for trade in open_trades:
        symbol = trade.get("symbol")
        # v62: start the window where the entry price was observed (scan
        # snapshot), not when the trade record was written later in the run
        since_iso = trade.get("price_observed_at") or trade.get("created_at")
        candle_rows = fetch_okx_candle_rows(symbol, since_iso)
        time.sleep(OKX_POLITE_DELAY)
        current_price = fetch_current_spot(symbol)
        time.sleep(OKX_POLITE_DELAY)
        fallback = []
        if candle_rows is None:  # v69: fall back to our own snapshots
            candle_rows = history_rows_since(history.get(trade.get("asset_id")), since_iso)
            fallback.append("price_history")
        if current_price is None:
            current_price = scan_prices.get(trade.get("asset_id"))
            fallback.append("scan_price")

        before_status = trade.get("status")
        check_trade(trade, candle_rows, current_price, now)
        if fallback and trade.get("last_check_source") != "unavailable":
            trade["last_check_source"] = "fallback:" + "+".join(fallback)  # v69
        if trade["status"] != before_status:
            if trade["status"] == "stopped":
                n_stopped += 1
            elif trade["status"] == "closed_targets_complete":
                n_completed += 1
            elif trade["status"] == "expired_no_resolution":
                n_expired += 1

    trades_data["trades"] = trades
    save_json(V2_SHADOW_TRADES_PATH, trades_data)

    summary = compute_performance_summary(trades)
    save_json(V2_PERFORMANCE_SUMMARY_PATH, {"generated_at": now.isoformat(), "overall": summary})

    print(f"V2 tracker: checked {len(open_trades)} open trades - "
          f"{n_stopped} newly stopped, {n_completed} newly completed, {n_expired} newly expired. "
          f"Overall: {summary['n_closed']} closed, win_rate={summary['win_rate_pct']}%, "
          f"avg_return={summary['avg_return_pct']}%.")


if __name__ == "__main__":
    main()
