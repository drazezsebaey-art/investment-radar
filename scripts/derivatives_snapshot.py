"""
derivatives_snapshot.py (v63) - derivatives forensics layer.

Replaces most of the CoinGecko-free part of the manual Coinglass screenshots
with an automatic, structured snapshot per coin, so a coin analysis can start
from data/derivatives.json instead of 5-7 screenshots:

  funding (now, 7d avg, 30d percentile), open interest (USD, 24h change,
  OI / market cap), futures volume 24h, long/short accounts, TOP-TRADER
  long/short by accounts AND by positions, taker buy/sell 24h, and recent
  liquidations split long vs short - plus rule-based flags in the same
  vocabulary as the Crypto Intelligence Engine.

Source: OKX public API (keyless, already reachable from GitHub Actions -
Binance/Bybit futures endpoints refuse US-hosted runners). SINGLE VENUE:
numbers are OKX-only, not Coinglass cross-exchange aggregates - read them as
direction/shape, not as the market-wide total. Zero CoinGecko usage.

Coins: watchlist + ETF watch list + coins with open trades (all tracks) +
the top radar coins by confidence score, capped at MAX_COINS. Coins without
an OKX USDT perpetual are recorded as `no_perp`.

Output: data/derivatives.json. Own gate (GATE_MINUTES). Never fails the workflow.
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "derivatives.json"
FLAGS = ROOT / "data" / "radar-flags.json"
WATCHLIST = ROOT / "config" / "watchlist.json"
ETF_WATCH = ROOT / "config" / "etf-watch.json"
TRADE_FILES = [ROOT / "config" / "trades.json", ROOT / "data" / "shadow-trades.json",
               ROOT / "data" / "scalp-trades.json", ROOT / "data" / "v2-shadow-trades.json"]

ENGINE_VERSION = "derivatives-v63"
OKX = "https://www.okx.com/api/v5"
GATE_MINUTES = 60
MAX_COINS = 25
TOP_RADAR_COINS = 8
PAUSE_SEC = 0.45          # rubik endpoints: 5 requests / 2s
TIMEOUT = 15

# id -> symbol for coins that may be missing from the current radar scan
KNOWN_SYMBOLS = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "near": "NEAR", "avalanche-2": "AVAX",
    "ripple": "XRP", "sui": "SUI", "sei-network": "SEI", "pax-gold": "PAXG", "bittensor": "TAO",
    "zcash": "ZEC", "worldcoin-wld": "WLD", "cardano": "ADA", "chainlink": "LINK", "stellar": "XLM",
    "bitcoin-cash": "BCH", "uniswap": "UNI", "ondo-finance": "ONDO", "tellor": "TRB",
}

# flag thresholds (tracked in config/trials-log.json)
DERIV_OI_MCAP_EXTREME_PCT = 20.0
DERIV_TOP_POS_CROWDED = 2.5
DERIV_FUNDING_HOT_PCTL = 80.0
DERIV_DELEVERAGE_OI_DROP_PCT = -15.0
DERIV_TAKER_SELL_DOMINANT = 0.85
DERIV_TAKER_BUY_DOMINANT = 1.15
DERIV_LIQ_DOMINANCE_X = 3.0


def now_utc():
    return datetime.now(timezone.utc)


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def okx_get(path, params, sleep=time.sleep):
    url = f"{OKX}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "investment-radar/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = json.loads(r.read().decode())
    sleep(PAUSE_SEC)
    if str(body.get("code", "0")) != "0":
        raise RuntimeError(f"OKX {path} code={body.get('code')} msg={body.get('msg')}")
    return body.get("data") or []


# --------------------------------------------------------- selection ----
def select_coins(flags, watchlist, etf_watch, trade_lists):
    by_id = {c.get("id"): c for c in flags.get("coins", []) if c.get("id")}
    chosen, why = [], {}

    def add(cid, reason):
        if not cid or cid in ("tether", "usd-coin"):
            return
        if cid not in why:
            chosen.append(cid)
            why[cid] = []
        why[cid].append(reason)

    for cid in watchlist.get("always_include", []):
        add(cid, "watchlist")
    for cid in etf_watch.get("coins", {}):
        add(cid, "etf_watch")
    for trades in trade_lists:
        for t in trades if isinstance(trades, list) else []:
            if t.get("status") in ("open", "pending", "partial"):
                add(t.get("asset_id"), "open_trade")
    ranked = sorted(by_id.values(), key=lambda c: c.get("confidence_score") or 0, reverse=True)
    for c in ranked[:TOP_RADAR_COINS]:
        add(c["id"], "top_radar")

    out = []
    for cid in chosen[:MAX_COINS]:
        c = by_id.get(cid, {})
        sym = (c.get("symbol") or KNOWN_SYMBOLS.get(cid) or "").upper()
        if sym:
            out.append({"id": cid, "symbol": sym, "reasons": why[cid],
                        "market_cap_usd": c.get("market_cap_usd"), "change_24h_pct": c.get("change_24h_pct")})
    return out


# ------------------------------------------------------------ metrics ----
def funding_metrics(current_rows, history_rows):
    cur = f(current_rows[0].get("fundingRate")) if current_rows else None
    hist = [(int(r["fundingTime"]), f(r.get("realizedRate") or r.get("fundingRate"))) for r in history_rows
            if r.get("fundingTime")]
    hist = sorted([h for h in hist if h[1] is not None], reverse=True)
    if not hist:
        return {"funding_now_pct": round(cur * 100, 4) if cur is not None else None}
    newest = hist[0][0]
    week = [v for ts, v in hist if ts >= newest - 7 * 86400_000]
    ref = cur if cur is not None else hist[0][1]
    vals = [v for _, v in hist]
    pctl = round(sum(1 for v in vals if v <= ref) / len(vals) * 100, 1)
    return {"funding_now_pct": round(ref * 100, 4),
            "funding_7d_avg_pct": round(sum(week) / len(week) * 100, 4) if week else None,
            "funding_percentile_30d": pctl,
            "funding_negative_share_7d_pct": round(sum(1 for v in week if v < 0) / len(week) * 100, 1) if week else None}


def oi_volume_metrics(rows):
    """rubik open-interest-volume rows: [ts, oi_usd, vol_usd], newest first."""
    pts = sorted([(int(r[0]), f(r[1]), f(r[2])) for r in rows if len(r) >= 3], reverse=True)
    if not pts:
        return {}
    oi_now = pts[0][1]
    oi_24h = next((p[1] for p in pts if p[0] <= pts[0][0] - 24 * 3600_000), None)
    vol_24h = sum(p[2] or 0 for p in pts if p[0] > pts[0][0] - 24 * 3600_000)
    return {"oi_usd": oi_now, "oi_change_24h_pct": round((oi_now / oi_24h - 1) * 100, 2) if oi_now and oi_24h else None,
            "futures_volume_24h_usd": round(vol_24h, 0) if vol_24h else None}


def latest_ratio(rows):
    pts = sorted([(int(r[0]), f(r[1])) for r in rows if len(r) >= 2], reverse=True)
    return pts[0][1] if pts else None


def taker_ratio_24h(rows):
    pts = sorted([(int(r[0]), f(r[1]), f(r[2])) for r in rows if len(r) >= 3], reverse=True)
    if not pts:
        return None
    window = [p for p in pts if p[0] > pts[0][0] - 24 * 3600_000]
    sell, buy = sum(p[1] or 0 for p in window), sum(p[2] or 0 for p in window)
    return round(buy / sell, 3) if sell else None


def liquidation_metrics(rows, ct_val, now):
    details = []
    for block in rows:
        details.extend(block.get("details") or [])
    cutoff = int((now - timedelta(hours=24)).timestamp() * 1000)
    long_usd = short_usd = 0.0
    oldest = None
    for d in details:
        ts = int(d.get("ts") or 0)
        oldest = ts if oldest is None else min(oldest, ts)
        if ts < cutoff:
            continue
        usd = (f(d.get("sz")) or 0) * (ct_val or 0) * (f(d.get("bkPx")) or 0)
        pos = d.get("posSide")
        is_long = pos == "long" or (pos in (None, "", "net") and d.get("side") == "sell")
        if is_long:
            long_usd += usd
        else:
            short_usd += usd
    return {"liq_long_24h_usd": round(long_usd, 0), "liq_short_24h_usd": round(short_usd, 0),
            "liq_window_complete": bool(oldest is not None and oldest <= cutoff) or len(details) < 100,
            "liq_sample_n": len(details)}


def derive_flags(m):
    flags = []
    oi_mc = m.get("oi_to_mcap_pct")
    if oi_mc is not None and oi_mc >= DERIV_OI_MCAP_EXTREME_PCT:
        flags.append("LEVERAGE_EXTREME")
    tp, fn, pc = m.get("top_trader_position_ratio"), m.get("funding_now_pct"), m.get("funding_percentile_30d")
    if tp and tp >= DERIV_TOP_POS_CROWDED and fn is not None and fn > 0 and (pc or 0) >= DERIV_FUNDING_HOT_PCTL:
        flags.append("CROWDED_LONG")
    oic = m.get("oi_change_24h_pct")
    if fn is not None and fn < 0 and oic is not None and oic > 5:
        flags.append("SHORT_SQUEEZE_FUEL")
    if oic is not None and oic <= DERIV_DELEVERAGE_OI_DROP_PCT and abs(m.get("price_change_24h_pct") or 0) < 3:
        flags.append("QUIET_DELEVERAGING")
    tk = m.get("taker_buy_sell_24h")
    if tk is not None and tk <= DERIV_TAKER_SELL_DOMINANT:
        flags.append("AGGRESSIVE_SELLING")
    if tk is not None and tk >= DERIV_TAKER_BUY_DOMINANT:
        flags.append("AGGRESSIVE_BUYING")
    ll, ls = m.get("liq_long_24h_usd") or 0, m.get("liq_short_24h_usd") or 0
    if ls and ls >= DERIV_LIQ_DOMINANCE_X * max(ll, 1):
        flags.append("SHORT_SQUEEZE_24H")
    if ll and ll >= DERIV_LIQ_DOMINANCE_X * max(ls, 1):
        flags.append("LONG_FLUSH_24H")
    return flags


# --------------------------------------------------------------- coin ----
def snapshot_coin(c, get=okx_get, now=None):
    now = now or now_utc()
    sym = c["symbol"]
    inst, fam = f"{sym}-USDT-SWAP", f"{sym}-USDT"
    m = {"symbol": sym, "reasons": c["reasons"], "market_cap_usd": c.get("market_cap_usd"),
         "price_change_24h_pct": c.get("change_24h_pct"), "errors": []}

    def safe(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            m["errors"].append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
            return None

    inst_rows = safe("instrument", lambda: get("/public/instruments", {"instType": "SWAP", "instId": inst}))
    if not inst_rows:
        m["status"] = "no_perp"
        return m
    ct_val = f(inst_rows[0].get("ctVal"))

    cur = safe("funding", lambda: get("/public/funding-rate", {"instId": inst})) or []
    hist = safe("funding_hist", lambda: get("/public/funding-rate-history", {"instId": inst, "limit": 100})) or []
    m.update(funding_metrics(cur, hist))

    oiv = safe("oi_volume", lambda: get("/rubik/stat/contracts/open-interest-volume", {"ccy": sym, "period": "1H"}))
    m.update(oi_volume_metrics(oiv or []))
    if m.get("oi_usd") is None:  # rubik ccy not supported -> instrument-level OI
        oi = safe("oi", lambda: get("/public/open-interest", {"instType": "SWAP", "instId": inst})) or []
        if oi:
            m["oi_usd"] = f(oi[0].get("oiUsd"))

    m["ls_accounts_ratio"] = latest_ratio(safe("ls_acc", lambda: get(
        "/rubik/stat/contracts/long-short-account-ratio", {"ccy": sym, "period": "1H"})) or [])
    m["top_trader_account_ratio"] = latest_ratio(safe("top_acc", lambda: get(
        "/rubik/stat/contracts/long-short-account-ratio-contract-top-trader", {"instId": inst, "period": "1H"})) or [])
    m["top_trader_position_ratio"] = latest_ratio(safe("top_pos", lambda: get(
        "/rubik/stat/contracts/long-short-position-ratio-contract-top-trader", {"instId": inst, "period": "1H"})) or [])
    m["taker_buy_sell_24h"] = taker_ratio_24h(safe("taker", lambda: get(
        "/rubik/stat/taker-volume", {"ccy": sym, "instType": "CONTRACTS", "period": "1H"})) or [])
    liq = safe("liquidations", lambda: get("/public/liquidation-orders",
                                           {"instType": "SWAP", "instFamily": fam, "state": "filled", "limit": 100}))
    if liq is not None:
        m.update(liquidation_metrics(liq, ct_val, now))

    if m.get("oi_usd") and m.get("market_cap_usd"):
        m["oi_to_mcap_pct"] = round(m["oi_usd"] / m["market_cap_usd"] * 100, 2)
    m["flags"] = derive_flags(m)
    m["status"] = "ok" if not m["errors"] else "partial"
    return m


def run(now=None, get=okx_get):
    now = now or now_utc()
    prev = load(OUT, {})
    last = prev.get("updated_at")
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(minutes=GATE_MINUTES - 5):
                print(f"Derivatives gate closed (every {GATE_MINUTES} min). Last: {last}")
                return None
        except ValueError:
            pass
    coins = select_coins(load(FLAGS, {}), load(WATCHLIST, {}), load(ETF_WATCH, {}),
                         [load(p, []) for p in TRADE_FILES])
    out = {"updated_at": now.isoformat(), "engine_version": ENGINE_VERSION,
           "source": "OKX USDT perpetuals only (single venue, NOT a Coinglass cross-exchange aggregate)",
           "coins": {}}
    for c in coins:
        out["coins"][c["id"]] = snapshot_coin(c, get=get, now=now)
    ok = sum(1 for v in out["coins"].values() if v.get("status") == "ok")
    out["summary"] = {"n_coins": len(coins), "ok": ok,
                      "partial": sum(1 for v in out["coins"].values() if v.get("status") == "partial"),
                      "no_perp": sum(1 for v in out["coins"].values() if v.get("status") == "no_perp"),
                      "flagged": {k: v["flags"] for k, v in out["coins"].items() if v.get("flags")}}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Derivatives: {len(coins)} coins, {ok} ok, {out['summary']['partial']} partial, "
          f"{out['summary']['no_perp']} without OKX perp")
    for k, v in out["summary"]["flagged"].items():
        print(f"  - {k}: {', '.join(v)}")
    for k, v in out["coins"].items():
        for e in v.get("errors", [])[:2]:
            print(f"  ! {k} {e}")
    return out


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # never fail the workflow
        print(f"derivatives_snapshot failed (non-fatal): {e}", file=sys.stderr)
    sys.exit(0)
