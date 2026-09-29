"""
correction_monitor.py (v65) - "has the correction ended?" answered from data.

Part 1 - Bitcoin Q4 scenario (weekly, OKX keyless):
  A  last CLOSED weekly candle above the May-2026 high (BTC_KEY_LEVEL)
  B  last closed weekly candle below it
  C  price testing the 50-week SMA (within BTC_50W_TEST_PCT) or >= BTC_DEEP_DD_PCT
     below the 30-day high
  Uses CLOSED candles only (OKX confirm flag) - a wick is not a close.

Part 2 - every coin that ran >= IMPULSE_MIN_GAIN_PCT (low -> peak inside the
last IMPULSE_LOOKBACK_DAYS, peak inside the last PEAK_MAX_AGE_DAYS):
  retracement now / deepest since peak (fraction of the impulse)
  volume ratio: avg daily quote volume since peak / during the impulse
  OI drawdown from its peak (OKX rubik daily OI, keyless)
  funding now (data/derivatives.json, OKX fallback)
  4H structure (v65.2): scripts/smc_structure.py - LuxAlgo-SMC-modelled engine
  (internal 5 / swing 50, close-based breaks, alternating right-confirmed swings,
  order blocks with ATR filter). RESUMING = the latest INTERNAL event after the
  peak is a bullish CHoCH. The legacy choch_up() below is kept for reference only.
  Status: BROKEN > RESUMING > RESET_DONE > ONGOING (see classify()).

Output: data/correction-monitor.json. Own gate. Never fails the workflow.
Thresholds are tracked in config/trials-log.json (T012) - no changes before
n>=30 outcomes.
"""
import json
import sys
import time
import urllib.parse
import urllib.request

from smc_structure import post_peak_read, levels_report  # v65.2 / v65.3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "correction-monitor.json"
FLAGS = ROOT / "data" / "radar-flags.json"
DERIV = ROOT / "data" / "derivatives.json"
WATCHLIST = ROOT / "config" / "watchlist.json"
ETF_WATCH = ROOT / "config" / "etf-watch.json"
ENGINE_VERSION = "correction-v65.3"
OKX = "https://www.okx.com/api/v5"
GATE_MINUTES = 120
PAUSE_SEC = 0.35
TIMEOUT = 15
MAX_COINS = 30

# thresholds (tracked in config/trials-log.json)
BTC_KEY_LEVEL = 82800.0          # May-2026 high (Cowen / Soloway pivot zone 81-82.8K)
BTC_50W_TEST_PCT = 3.0           # price within 3% of the 50W SMA = "testing it"
BTC_DEEP_DD_PCT = 20.0           # >= 20% below the 30-day high = deep correction
IMPULSE_MIN_GAIN_PCT = 40.0
IMPULSE_LOOKBACK_DAYS = 45
PEAK_MAX_AGE_DAYS = 30
RETR_HEALTHY_MIN = 0.382
RETR_BROKEN = 0.786
VOL_DRYUP_RATIO = 0.7
OI_RESET_DD_PCT = 20.0
FUNDING_NEUTRAL_PCT = 0.005      # funding_now_pct at or below this = neutral / negative
PIVOT_N = 2

EXCLUDE = {"tether", "usd-coin", "dai", "ethena-usde", "first-digital-usd"}


def now_utc():
    return datetime.now(timezone.utc)


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def okx_get(path, params, sleep=time.sleep):
    url = f"{OKX}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "investment-radar/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = json.loads(r.read().decode())
    sleep(PAUSE_SEC)
    if str(body.get("code", "0")) != "0":
        raise RuntimeError(f"OKX {path} code={body.get('code')} msg={body.get('msg')}")
    return body.get("data") or []


def parse_candles(rows):
    """OKX rows -> oldest-first dicts. [ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm]"""
    out = []
    for r in rows or []:
        try:
            out.append({"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                        "c": float(r[4]), "vq": float(r[7]) if len(r) > 7 else float(r[5]),
                        "closed": (r[8] == "1") if len(r) > 8 else True})
        except (TypeError, ValueError, IndexError):
            continue
    return sorted(out, key=lambda x: x["ts"])


# ------------------------------------------------------------ BTC ----
def btc_scenario(weekly, daily):
    closed = [c for c in weekly if c["closed"]]
    if len(closed) < 50 or not daily:
        return {"scenario": None, "reason": "insufficient candles"}
    sma50 = sum(c["c"] for c in closed[-50:]) / 50
    last_w = closed[-1]["c"]
    price = daily[-1]["c"]
    high30 = max(c["h"] for c in daily[-30:])
    dd = (1 - price / high30) * 100
    dist_50w = (price / sma50 - 1) * 100
    if dist_50w <= BTC_50W_TEST_PCT or dd >= BTC_DEEP_DD_PCT:
        sc, why = "C", "testing the 50W SMA or deep drawdown from the 30d high"
    elif last_w >= BTC_KEY_LEVEL:
        sc, why = "A", "last closed weekly candle above the May high"
    else:
        sc, why = "B", "last closed weekly candle below the May high"
    return {"scenario": sc, "reason": why, "price": price, "last_weekly_close": last_w,
            "key_level": BTC_KEY_LEVEL, "sma_50w": round(sma50, 0), "dist_to_50w_pct": round(dist_50w, 2),
            "drawdown_from_30d_high_pct": round(dd, 2),
            "action": {"A": "normal gate-approved sizing",
                       "B": "half size, A+ setups only, prefer flush-zone entries",
                       "C": "no entries into the fall - wait for a daily bullish CHoCH"}[sc]}


# ------------------------------------------------------------ coins ----
def find_impulse(daily, now_ms):
    win = [c for c in daily if c["ts"] >= now_ms - IMPULSE_LOOKBACK_DAYS * 86400_000]
    if len(win) < 10:
        return None
    pk_i = max(range(len(win)), key=lambda i: win[i]["h"])
    if win[pk_i]["ts"] < now_ms - PEAK_MAX_AGE_DAYS * 86400_000 or pk_i == 0:
        return None
    lo_i = min(range(pk_i + 1), key=lambda i: win[i]["l"])
    low, peak = win[lo_i]["l"], win[pk_i]["h"]
    gain = (peak / low - 1) * 100
    if gain < IMPULSE_MIN_GAIN_PCT:
        return None
    return {"win": win, "lo_i": lo_i, "pk_i": pk_i, "low": low, "peak": peak, "gain_pct": round(gain, 1)}


def pivots(c4, kind):
    out = []
    for i in range(PIVOT_N, len(c4) - PIVOT_N):
        v = c4[i]["h"] if kind == "high" else c4[i]["l"]
        nb = [c4[j]["h"] if kind == "high" else c4[j]["l"] for j in range(i - PIVOT_N, i + PIVOT_N + 1) if j != i]
        if (kind == "high" and all(v > x for x in nb)) or (kind == "low" and all(v < x for x in nb)):
            out.append(i)
    return out


def choch_up(c4, peak_ts):
    """True if, after the pullback low, a 4H close broke the last lower-high pivot
    that formed between the peak and that low."""
    seg = [c for c in c4 if c["ts"] >= peak_ts]
    if len(seg) < 2 * PIVOT_N + 3:
        return {"choch_up": False, "reason": "too few 4H candles since peak"}
    low_i = min(range(len(seg)), key=lambda i: seg[i]["l"])
    highs = [i for i in pivots(seg, "high") if 0 < i < low_i]
    if not highs:
        return {"choch_up": False, "reason": "no lower-high pivot before the pullback low yet",
                "pullback_low": seg[low_i]["l"]}
    lh = seg[highs[-1]]["h"]
    broke = any(c["c"] > lh for c in seg[low_i + 1:])
    return {"choch_up": broke, "last_lower_high": lh, "pullback_low": seg[low_i]["l"]}


def oi_drawdown(rows):
    pts = sorted([(int(r[0]), float(r[1])) for r in rows or [] if len(r) >= 2], reverse=True)
    if not pts:
        return None
    peak = max(v for _, v in pts)
    return round((1 - pts[0][1] / peak) * 100, 1) if peak else None


def classify(m):
    if m["retracement_max"] > RETR_BROKEN or m["price"] < m["impulse_low"]:
        return "BROKEN"
    if m.get("choch_up"):
        return "RESUMING"
    checks = {
        "oi_reset": (m.get("oi_drawdown_pct") or 0) >= OI_RESET_DD_PCT,
        "funding_neutral": m.get("funding_now_pct") is not None and m["funding_now_pct"] <= FUNDING_NEUTRAL_PCT,
        "volume_dried_up": m.get("volume_ratio") is not None and m["volume_ratio"] <= VOL_DRYUP_RATIO,
        "healthy_depth": RETR_HEALTHY_MIN <= m["retracement_max"] <= RETR_BROKEN,
    }
    m["reset_checks"] = checks
    return "RESET_DONE" if sum(checks.values()) >= 3 and checks["healthy_depth"] else "ONGOING"


def analyse_coin(c, get, deriv, now):
    sym = c["symbol"]
    daily = parse_candles(get("/market/candles", {"instId": f"{sym}-USDT", "bar": "1D", "limit": 60}))
    imp = find_impulse(daily, int(now.timestamp() * 1000))
    if not imp:
        return None
    win, pk_i = imp["win"], imp["pk_i"]
    price = daily[-1]["c"]
    since = win[pk_i:]
    rng = imp["peak"] - imp["low"]
    min_since = min(x["l"] for x in since)
    imp_vol = [x["vq"] for x in win[imp["lo_i"]:pk_i + 1]]
    pb_vol = [x["vq"] for x in win[pk_i + 1:]]
    m = {"coin": c["id"], "symbol": sym, "reasons": c["reasons"], "price": price,
         "impulse_low": imp["low"], "impulse_peak": imp["peak"], "impulse_gain_pct": imp["gain_pct"],
         "peak_date": datetime.fromtimestamp(win[pk_i]["ts"] / 1000, tz=timezone.utc).date().isoformat(),
         "retracement_now": round((imp["peak"] - price) / rng, 3),
         "retracement_max": round((imp["peak"] - min_since) / rng, 3),
         "fib_levels": {k: round(imp["peak"] - rng * k, 6) for k in (0.382, 0.5, 0.618, 0.786)},
         "volume_ratio": round((sum(pb_vol) / len(pb_vol)) / (sum(imp_vol) / len(imp_vol)), 2)
         if pb_vol and imp_vol and sum(imp_vol) else None}
    try:
        # v65.2: 300 x 4H (ATR(200) for the order-block volatility filter needs history)
        c4 = parse_candles(get("/market/candles", {"instId": f"{sym}-USDT", "bar": "4H", "limit": 300}))
        m.update(post_peak_read(c4, win[pk_i]["ts"]))
        # v65.3: break validation / sweeps (hold signal) / premium-discount / volume profile / invalidation
        m["levels"] = levels_report(c4, imp["low"], imp["peak"])
    except Exception as e:  # noqa: BLE001
        m["structure_error"] = str(e)[:100]
    try:
        m["oi_drawdown_pct"] = oi_drawdown(get("/rubik/stat/contracts/open-interest-volume",
                                               {"ccy": sym, "period": "1D"}))
    except Exception as e:  # noqa: BLE001
        m["oi_error"] = str(e)[:100]
    d = (deriv.get("coins") or {}).get(c["id"], {})
    m["funding_now_pct"] = d.get("funding_now_pct")
    if m["funding_now_pct"] is None:
        try:
            rows = get("/public/funding-rate", {"instId": f"{sym}-USDT-SWAP"})
            m["funding_now_pct"] = round(float(rows[0]["fundingRate"]) * 100, 4) if rows else None
        except Exception:  # noqa: BLE001
            pass
    m["status"] = classify(m)
    lv = m.get("levels") or {}
    pd_ = (lv.get("premium_discount_impulse") or {}).get("zone")
    m["entry_ready"] = bool(m["status"] in ("RESET_DONE", "RESUMING")
                            and (lv.get("bullish_hold_signal") or m.get("choch_up"))
                            and pd_ in ("DISCOUNT", "DEEP_DISCOUNT", "EQUILIBRIUM"))
    m["entry_ready_rule"] = ("status RESET_DONE/RESUMING + a hold signal (fresh low sweep or bullish internal CHoCH) "
                             "+ price at/below the impulse equilibrium - still needs gate + Agent Room + visual check")
    return m


def select_coins(flags, watchlist, etf_watch, trades_files):
    by_id = {c.get("id"): c for c in flags.get("coins", []) if c.get("id")}
    chosen, why = [], {}

    def add(cid, r):
        if cid and cid not in EXCLUDE and cid != "bitcoin":
            if cid not in why:
                chosen.append(cid)
                why[cid] = []
            why[cid].append(r)
    for cid in watchlist.get("always_include", []):
        add(cid, "watchlist")
    for cid in etf_watch.get("coins", {}):
        add(cid, "etf_watch")
    for trades in trades_files:
        for t in trades if isinstance(trades, list) else []:
            if t.get("status") in ("open", "pending", "partial"):
                add(t.get("asset_id"), "open_trade")
    for c in sorted(by_id.values(), key=lambda c: c.get("change_7d_pct") or 0, reverse=True)[:12]:
        add(c["id"], "top_7d_mover")
    out = []
    for cid in chosen[:MAX_COINS]:
        sym = (by_id.get(cid, {}).get("symbol") or "").upper()
        if not sym:
            try:
                from derivatives_snapshot import KNOWN_SYMBOLS  # same repo, optional
                sym = KNOWN_SYMBOLS.get(cid, "")
            except Exception:  # noqa: BLE001
                sym = ""
        if sym:
            out.append({"id": cid, "symbol": sym, "reasons": why[cid]})
    return out


def run(now=None, get=okx_get):
    now = now or now_utc()
    prev = load(OUT, {})
    if prev.get("updated_at"):
        try:
            if now - datetime.fromisoformat(prev["updated_at"]) < timedelta(minutes=GATE_MINUTES - 5):
                print(f"Correction monitor gate closed (every {GATE_MINUTES} min).")
                return None
        except ValueError:
            pass
    errors = []
    try:
        btc = btc_scenario(parse_candles(get("/market/candles", {"instId": "BTC-USDT", "bar": "1W", "limit": 60})),
                           parse_candles(get("/market/candles", {"instId": "BTC-USDT", "bar": "1D", "limit": 40})))
    except Exception as e:  # noqa: BLE001
        btc = {"scenario": None}
        errors.append(f"btc: {type(e).__name__}: {str(e)[:100]}")
    deriv = load(DERIV, {})
    trade_files = [load(ROOT / "config" / "trades.json", []), load(ROOT / "data" / "shadow-trades.json", []),
                   load(ROOT / "data" / "scalp-trades.json", [])]
    coins = select_coins(load(FLAGS, {}), load(WATCHLIST, {}), load(ETF_WATCH, {}), trade_files)
    results = []
    for c in coins:
        try:
            r = analyse_coin(c, get, deriv, now)
            if r:
                results.append(r)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{c['id']}: {type(e).__name__}: {str(e)[:100]}")
    order = {"RESUMING": 0, "RESET_DONE": 1, "ONGOING": 2, "BROKEN": 3}
    results.sort(key=lambda r: (order[r["status"]], -r["impulse_gain_pct"]))
    out = {"updated_at": now.isoformat(), "engine_version": ENGINE_VERSION, "btc_q4_scenario": btc,
           "coins_in_correction": results, "n_screened": len(coins),
           "status_counts": {s: sum(1 for r in results if r["status"] == s) for s in order},
           "notes": "RESUMING still needs a visual check of the 4H chart before any entry; "
                    "RESET_DONE = first-tranche zone at most. Gate + Agent Room always apply.",
           "errors": errors}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"BTC Q4 scenario: {btc.get('scenario')} ({btc.get('reason')}) | coins in correction: {len(results)} "
          f"{out['status_counts']}")
    for r in results[:12]:
        print(f"  - {r['coin']}: {r['status']} | +{r['impulse_gain_pct']}% impulse, retr now {r['retracement_now']}, "
              f"max {r['retracement_max']}, vol {r.get('volume_ratio')}, OI dd {r.get('oi_drawdown_pct')}%")
    for e in errors[:10]:
        print(f"  ! {e}")
    return out


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # never fail the workflow
        print(f"correction_monitor failed (non-fatal): {e}", file=sys.stderr)
    sys.exit(0)
