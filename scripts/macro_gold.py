"""
macro_gold.py (v64) - Gold Intelligence Engine pillar 6 + positioning, automated.

  FRED (keyless CSV; optional FRED_API_KEY secret is used if present):
    DFII10   10-year TIPS real yield
    DGS10    10-year nominal yield
    T10YIE   10-year breakeven inflation
    DTWEXBGS broad trade-weighted US dollar index
  Gold proxy price: PAXG-USDT daily closes (OKX, keyless)
  CFTC COT (Socrata, keyless, weekly): COMEX gold (code 088691), Managed Money
    net position, % of open interest, weekly change, 3-year percentile.

Pillar-6 check (the engine's mandatory step): is gold moving WITH its inverse
relation to real yields / the dollar, or deviating? Deviation is flagged, not
explained - the explanation (debasement / geopolitics / de-dollarisation) is
the analyst's job.

Output: data/macro-gold.json. Own gate. Never fails the workflow.
"""
import csv
import io
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "macro-gold.json"
ENGINE_VERSION = "macro_gold-v64"
GATE_MINUTES = 360
COT_REFRESH_HOURS = 24
TIMEOUT = 30
FRED_SERIES = {"DFII10": "real_yield_10y_pct", "DGS10": "nominal_yield_10y_pct",
               "T10YIE": "breakeven_10y_pct", "DTWEXBGS": "usd_broad_index"}
GOLD_COT_CODE = "088691"

# thresholds (tracked in config/trials-log.json)
MACRO_DIVERGENCE_GOLD_PCT = 2.0     # gold 20d move considered meaningful
MACRO_DIVERGENCE_RY_BP = 10.0       # real-yield 20d move considered meaningful (basis points)
MACRO_COT_CROWDED_PCTL = 85.0       # managed-money net long percentile (3y) = crowded


def now_utc():
    return datetime.now(timezone.utc)


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "investment-radar/1.0"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode()


# ------------------------------------------------------------- FRED ----
def parse_fred_csv(text):
    rows = []
    for rec in csv.reader(io.StringIO(text)):
        if len(rec) < 2 or not rec[0][:1].isdigit():
            continue
        try:
            rows.append((rec[0], float(rec[1])))
        except ValueError:
            continue           # FRED uses "." for missing observations
    return sorted(rows, reverse=True)


def fetch_fred(series_id):
    key = os.environ.get("FRED_API_KEY")
    if key:
        q = urllib.parse.urlencode({"series_id": series_id, "api_key": key, "file_type": "json",
                                    "sort_order": "desc", "limit": 400})
        obs = json.loads(get_text(f"https://api.stlouisfed.org/fred/series/observations?{q}")).get("observations", [])
        return [(o["date"], float(o["value"])) for o in obs if o.get("value") not in (None, ".")]
    start = (now_utc() - timedelta(days=500)).strftime("%Y-%m-%d")
    return parse_fred_csv(get_text(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"))


def series_metrics(rows, bp=False):
    """rows newest first [(date, value)]. Changes over ~5 and ~20 observations."""
    if not rows:
        return {}
    last_d, last = rows[0]
    def chg(n):
        if len(rows) <= n:
            return None
        d = last - rows[n][1]
        return round(d * 100, 1) if bp else round((last / rows[n][1] - 1) * 100, 2)
    return {"value": last, "date": last_d, "chg_1w": chg(5), "chg_1m": chg(20),
            "units_of_change": "bp" if bp else "pct"}


# ------------------------------------------------------------- gold ----
def fetch_gold():
    body = json.loads(get_text("https://www.okx.com/api/v5/market/candles?instId=PAXG-USDT&bar=1D&limit=40"))
    rows = sorted(((datetime.fromtimestamp(int(r[0]) / 1000, tz=timezone.utc).date().isoformat(), float(r[4]))
                   for r in body.get("data", [])), reverse=True)
    return rows


# -------------------------------------------------------------- COT ----
def fetch_cot():
    q = urllib.parse.urlencode({"$where": f"cftc_contract_market_code='{GOLD_COT_CODE}'",
                                "$order": "report_date_as_yyyy_mm_dd DESC", "$limit": 160})
    return json.loads(get_text(f"https://publicreporting.cftc.gov/resource/72hh-3qpy.json?{q}"))


def cot_metrics(rows):
    pts = []
    for r in rows or []:
        try:
            pts.append((r["report_date_as_yyyy_mm_dd"][:10],
                        float(r["m_money_positions_long_all"]) - float(r["m_money_positions_short_all"]),
                        float(r["open_interest_all"])))
        except (KeyError, TypeError, ValueError):
            continue
    pts.sort(reverse=True)
    if not pts:
        return {}
    d, net, oi = pts[0]
    nets = [p[1] for p in pts]
    pctl = round(sum(1 for v in nets if v <= net) / len(nets) * 100, 1)
    out = {"report_date": d, "mm_net_contracts": net, "mm_net_pct_of_oi": round(net / oi * 100, 2) if oi else None,
           "mm_net_change_1w": net - pts[1][1] if len(pts) > 1 else None,
           "mm_net_percentile_3y": pctl, "sample_weeks": len(pts)}
    out["flags"] = (["MM_CROWDED_LONG"] if pctl >= MACRO_COT_CROWDED_PCTL else []) + \
                   (["MM_WASHED_OUT"] if pctl <= 100 - MACRO_COT_CROWDED_PCTL else [])
    return out


# ---------------------------------------------------------- pillar 6 ----
def pillar6(gold, ry, usd):
    g, r, u = gold.get("chg_1m"), ry.get("chg_1m"), usd.get("chg_1m")
    fl = []
    if g is None or r is None:
        return {"status": "insufficient_data", "flags": fl}
    if g >= MACRO_DIVERGENCE_GOLD_PCT and r >= MACRO_DIVERGENCE_RY_BP:
        fl.append("GOLD_UP_DESPITE_REAL_YIELD_UP")
    if g <= -MACRO_DIVERGENCE_GOLD_PCT and r <= -MACRO_DIVERGENCE_RY_BP:
        fl.append("GOLD_DOWN_DESPITE_REAL_YIELD_DOWN")
    if u is not None and g >= MACRO_DIVERGENCE_GOLD_PCT and u >= 1.0:
        fl.append("GOLD_UP_WITH_STRONGER_DOLLAR")
    status = "DEVIATING" if fl else "CONSISTENT_OR_NEUTRAL"
    return {"status": status, "flags": fl,
            "note": "Deviation = the inverse relation is NOT holding; the engine must name the cause "
                    "(debasement / geopolitics / de-dollarisation / bond-confidence), not assume the pattern."}


def run(now=None, fetch=None):
    now = now or now_utc()
    fetch = fetch or {"fred": fetch_fred, "gold": fetch_gold, "cot": fetch_cot}
    prev = load(OUT, {})
    if prev.get("updated_at"):
        try:
            if now - datetime.fromisoformat(prev["updated_at"]) < timedelta(minutes=GATE_MINUTES - 5):
                print("Macro/gold gate closed.")
                return None
        except ValueError:
            pass
    errors, macro = [], {}
    for sid, name in FRED_SERIES.items():
        try:
            macro[name] = series_metrics(fetch["fred"](sid), bp=name.endswith("_pct"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"FRED {sid}: {type(e).__name__}: {str(e)[:100]}")
            macro[name] = {}
    try:
        gold = series_metrics(fetch["gold"]())
    except Exception as e:  # noqa: BLE001
        gold = {}
        errors.append(f"gold: {type(e).__name__}: {str(e)[:100]}")
    cot = prev.get("cot", {})
    last_cot = prev.get("cot_fetched_at")
    stale = not last_cot or now - datetime.fromisoformat(last_cot) >= timedelta(hours=COT_REFRESH_HOURS)
    cot_fetched_at = last_cot
    if stale:
        try:
            cot = cot_metrics(fetch["cot"]())
            cot_fetched_at = now.isoformat()
        except Exception as e:  # noqa: BLE001
            errors.append(f"COT: {type(e).__name__}: {str(e)[:100]}")
    out = {"updated_at": now.isoformat(), "engine_version": ENGINE_VERSION,
           "fred_mode": "api_key" if os.environ.get("FRED_API_KEY") else "keyless_csv",
           "macro": macro, "gold_paxg": gold, "cot_gold_managed_money": cot, "cot_fetched_at": cot_fetched_at,
           "pillar6_check": pillar6(gold, macro.get("real_yield_10y_pct", {}), macro.get("usd_broad_index", {})),
           "errors": errors}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    ry = macro.get("real_yield_10y_pct", {})
    print(f"Macro/gold: real10y={ry.get('value')} ({ry.get('chg_1m')}bp 1m) | USD={macro.get('usd_broad_index', {}).get('value')} "
          f"| gold(PAXG) 1m={gold.get('chg_1m')}% | pillar6={out['pillar6_check']['status']} "
          f"| COT pctl={cot.get('mm_net_percentile_3y')}")
    for e in errors:
        print(f"  ! {e}")
    return out


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # never fail the workflow
        print(f"macro_gold failed (non-fatal): {e}", file=sys.stderr)
    sys.exit(0)
