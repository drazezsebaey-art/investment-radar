"""
altseason_regime.py (v64) - is capital rotating from BTC into altcoins?

Market-level regime gauges, all keyless and zero CoinGecko usage:
  - BTC dominance + total market cap        (CoinPaprika /global)
  - ETH/BTC ratio, 7d / 30d change, vs the 0.03426 weekly-close trigger (OKX)
  - "breadth" proxy: share of the scanned universe outperforming BTC over 7d
    (from radar-flags.json - a FAST proxy, NOT the official 90-day index)
  - stablecoin supply: total circulating 7d / 30d change, and per-chain
    change since the previous snapshot (DefiLlama stablecoins)

History of each gauge is kept (data/altseason.json -> history, ~120 points)
so trends (dominance falling for N readings) can be judged, not single prints.
Own gate: GATE_MINUTES. Never fails the workflow.
"""
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "altseason.json"
FLAGS = ROOT / "data" / "radar-flags.json"
ENGINE_VERSION = "altseason-v68"
GATE_MINUTES = 240
HISTORY_MAX = 120
TIMEOUT = 20

# thresholds (tracked in config/trials-log.json)
ALT_ETHBTC_TRIGGER = 0.03426          # weekly close above -> rotation signal (cited trigger, 25/8/2026)
ALT_BTC_DOM_ROTATION = 55.0           # dominance below -> broad-rotation zone
ALT_BTC_DOM_LINE = 60.0               # line in the sand
ALT_BREADTH_ALTSEASON_PCT = 75.0      # share outperforming BTC (7d proxy)
ALT_RISK_DOM_RISE_3D_PT = 0.5         # v68: BTC.D up >= 0.5 pt in ~3 days = alts losing share
ALT_RISK_BREAKOUT_MARGIN_PT = 0.2     # v68: recent BTC.D high clears the prior window's high by this much
ALT_RISK_MIN_HISTORY = 18             # v68: ~3 days of 4h snapshots before any BTC.D trend call


def now_utc():
    return datetime.now(timezone.utc)


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "investment-radar/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def fetch_global():
    g = get_json("https://api.coinpaprika.com/v1/global")
    return {"btc_dominance_pct": g.get("bitcoin_dominance_percentage"),
            "total_mcap_usd": g.get("market_cap_usd"),
            "total_volume_24h_usd": g.get("volume_24h_usd")}


def fetch_ethbtc():
    body = get_json("https://www.okx.com/api/v5/market/candles?instId=ETH-BTC&bar=1D&limit=40")
    rows = sorted(((int(r[0]), float(r[4])) for r in body.get("data", [])), reverse=True)
    return ethbtc_metrics(rows)


def ethbtc_metrics(rows):
    """rows: [(ts_ms, close)] newest first."""
    if not rows:
        return {}
    last = rows[0][1]
    c7 = rows[7][1] if len(rows) > 7 else None
    c30 = rows[30][1] if len(rows) > 30 else None
    return {"eth_btc": last,
            "eth_btc_7d_pct": round((last / c7 - 1) * 100, 2) if c7 else None,
            "eth_btc_30d_pct": round((last / c30 - 1) * 100, 2) if c30 else None,
            "eth_btc_above_trigger": last > ALT_ETHBTC_TRIGGER}


def breadth_proxy(flags):
    coins = flags.get("coins", [])
    btc = next((c for c in coins if c.get("id") == "bitcoin"), None)
    btc7 = (btc or {}).get("change_7d_pct")
    alts = [c for c in coins if c.get("id") not in ("bitcoin", "tether", "usd-coin")
            and c.get("change_7d_pct") is not None]
    if btc7 is None or not alts:
        return {}
    out = sum(1 for c in alts if c["change_7d_pct"] > btc7)
    return {"breadth_7d_pct": round(out / len(alts) * 100, 1), "breadth_universe_n": len(alts),
            "btc_7d_pct": btc7}


def fetch_stablecoins():
    total = get_json("https://stablecoins.llama.fi/stablecoincharts/all")
    chains = get_json("https://stablecoins.llama.fi/stablecoinchains")
    return stable_metrics(total, chains)


def _circ(pt):
    v = pt.get("totalCirculatingUSD") or pt.get("totalCirculating") or {}
    return v.get("peggedUSD") if isinstance(v, dict) else None


def stable_metrics(total, chains):
    pts = [(int(p.get("date", 0)), _circ(p)) for p in (total or [])]
    pts = sorted([p for p in pts if p[1]], reverse=True)
    out = {}
    if pts:
        last = pts[0][1]
        def ago(days):
            cut = pts[0][0] - days * 86400
            return next((v for ts, v in pts if ts <= cut), None)
        p7, p30 = ago(7), ago(30)
        out = {"stablecoin_supply_usd": round(last, 0),
               "stablecoin_7d_pct": round((last / p7 - 1) * 100, 2) if p7 else None,
               "stablecoin_30d_pct": round((last / p30 - 1) * 100, 2) if p30 else None}
    by_chain = {}
    for c in chains or []:
        v = c.get("totalCirculatingUSD", {})
        usd = v.get("peggedUSD") if isinstance(v, dict) else None
        if c.get("name") and usd:
            by_chain[c["name"]] = usd
    out["_by_chain"] = by_chain
    return out


def chain_flows(by_chain, prev_by_chain, top=10):
    if not prev_by_chain:
        return []
    rows = []
    for name, usd in by_chain.items():
        p = prev_by_chain.get(name)
        if p and usd > 50_000_000:
            rows.append({"chain": name, "supply_usd": round(usd, 0),
                         "change_usd": round(usd - p, 0), "change_pct": round((usd / p - 1) * 100, 2)})
    return sorted(rows, key=lambda r: r["change_pct"], reverse=True)[:top]


def regime(m, history):
    """Rule-based label. Needs the dominance TREND (history), not one print."""
    sig = []
    dom = m.get("btc_dominance_pct")
    doms = [h.get("btc_dominance_pct") for h in history[-6:] if h.get("btc_dominance_pct")]
    falling = len(doms) >= 4 and doms[-1] < doms[0]
    if dom is not None and dom < ALT_BTC_DOM_ROTATION:
        sig.append("BTC_DOM_BELOW_55")
    elif dom is not None and dom < ALT_BTC_DOM_LINE and falling:
        sig.append("BTC_DOM_FALLING_BELOW_60")
    if m.get("eth_btc_above_trigger"):
        sig.append("ETHBTC_ABOVE_TRIGGER")
    if (m.get("breadth_7d_pct") or 0) >= ALT_BREADTH_ALTSEASON_PCT:
        sig.append("BREADTH_75")
    if (m.get("stablecoin_30d_pct") or 0) > 2:
        sig.append("STABLECOIN_EXPANSION")
    n = len(sig)
    label = "ALTSEASON_CONFIRMED" if n >= 4 else "ROTATION_BUILDING" if n >= 2 else "BTC_LED"
    return label, sig


def _dom_series(history):
    pts = []
    for h in history:
        try:
            if h.get("btc_dominance_pct") is not None:
                pts.append((datetime.fromisoformat(h["ts"]), float(h["btc_dominance_pct"])))
        except (KeyError, TypeError, ValueError):
            continue
    return pts


def _value_days_ago(pts, days):
    cut = pts[-1][0] - timedelta(days=days)
    older = [v for t, v in pts if t <= cut]
    return older[-1] if older else None


def btc_dominance_alt_risk(history):
    """v68: altcoin-risk filter from BTC dominance (Soloway: a BTC.D breakout that
    holds after a retest means alts can fall 2-3x a BTC pullback). Built from our
    own 4h snapshots, so it reads a HORIZONTAL breakout of the prior window's high
    and a 3-day rise - a hand-drawn trendline read stays a manual chart check."""
    pts = _dom_series(history)
    out = {"n_history": len(pts), "flags": [], "level": "UNKNOWN"}
    if len(pts) < ALT_RISK_MIN_HISTORY:
        out["note"] = f"needs {ALT_RISK_MIN_HISTORY} snapshots (~3 days) before a BTC.D trend call"
        return out
    last = pts[-1][1]
    d3, d7 = _value_days_ago(pts, 3), _value_days_ago(pts, 7)
    out["dom_now"] = last
    out["dom_3d_change_pt"] = round(last - d3, 2) if d3 is not None else None
    out["dom_7d_change_pt"] = round(last - d7, 2) if d7 is not None else None
    recent, prior = pts[-6:], pts[:-6]
    if prior:
        prior_high = max(v for _, v in prior)
        recent_high = max(v for _, v in recent)
        out["prior_window_high"] = prior_high
        if recent_high >= prior_high + ALT_RISK_BREAKOUT_MARGIN_PT:
            out["flags"].append("BTC_DOM_BREAKOUT_HOLDING" if last >= prior_high else "BTC_DOM_BREAKOUT_FAILED")
    if out["dom_3d_change_pt"] is not None and out["dom_3d_change_pt"] >= ALT_RISK_DOM_RISE_3D_PT:
        out["flags"].append("BTC_DOM_RISING_3D")
    if "BTC_DOM_BREAKOUT_HOLDING" in out["flags"] and "BTC_DOM_RISING_3D" in out["flags"]:
        out["level"] = "HIGH"
    elif {"BTC_DOM_BREAKOUT_HOLDING", "BTC_DOM_RISING_3D"} & set(out["flags"]):
        out["level"] = "ELEVATED"
    else:
        out["level"] = "NORMAL"
    return out


def run(now=None, fetchers=None):
    now = now or now_utc()
    fetchers = fetchers or {"global": fetch_global, "ethbtc": fetch_ethbtc, "stable": fetch_stablecoins}
    prev = load(OUT, {})
    if prev.get("updated_at"):
        try:
            if now - datetime.fromisoformat(prev["updated_at"]) < timedelta(minutes=GATE_MINUTES - 5):
                print(f"Altseason gate closed (every {GATE_MINUTES} min).")
                return None
        except ValueError:
            pass
    m, errors = {}, []
    for name, fn in fetchers.items():
        try:
            m.update(fn())
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:120]}")
    m.update(breadth_proxy(load(FLAGS, {})))
    by_chain = m.pop("_by_chain", {}) or {}
    history = prev.get("history", [])
    label, sig = regime(m, history + [m])
    snap = {"ts": now.isoformat(), **{k: v for k, v in m.items()}}
    out = {"updated_at": now.isoformat(), "engine_version": ENGINE_VERSION,
           "regime": label, "signals": sig, "metrics": m,
           "alt_risk": btc_dominance_alt_risk(history + [snap]),  # v68
           "stablecoin_chain_flows_since_last": chain_flows(by_chain, prev.get("_stable_by_chain", {})),
           "_stable_by_chain": by_chain or prev.get("_stable_by_chain", {}),
           "notes": "breadth_7d_pct is a fast 7-day proxy on the scanned universe, NOT the official 90-day Altcoin Season Index.",
           "errors": errors, "history": (history + [snap])[-HISTORY_MAX:]}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Altseason regime: {label} | signals: {', '.join(sig) or '-'} | "
          f"BTC.D={m.get('btc_dominance_pct')} ETH/BTC={m.get('eth_btc')} breadth7d={m.get('breadth_7d_pct')}")
    for e in errors:
        print(f"  ! {e}")
    return out


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # never fail the workflow
        print(f"altseason_regime failed (non-fatal): {e}", file=sys.stderr)
    sys.exit(0)
