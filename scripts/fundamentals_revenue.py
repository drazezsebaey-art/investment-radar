"""
fundamentals_revenue.py (v64) - revenue that leads price.

Lesson behind it (28/9/2026 review): PUMP's rally was preceded by two months of
rising protocol revenue that funds automatic buybacks - visible on-chain BEFORE
the price move. This layer watches exactly that, keyless and zero CoinGecko:

  DefiLlama fees overview (dataType = dailyRevenue and dailyHoldersRevenue)
    - revenue 30d, annualised, growth 30d-over-30d and 7d-over-7d
    - holders revenue (buybacks / burns / distributions) and HOLDER YIELD
      = annualised holders revenue / market cap
    - revenue yield = annualised revenue / market cap (inverse P/S)
  Snapshot (hub.snapshot.org GraphQL): ACTIVE governance proposals about
    buybacks, burns, fee switches, revenue share - dated catalysts.

Limits: off-chain businesses (e.g. Quant Network's licence revenue) are NOT
on DefiLlama - they need company filings (a Chrome job, not this script).
Output: data/fundamentals.json. Own gate (daily). Never fails the workflow.
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "fundamentals.json"
FLAGS = ROOT / "data" / "radar-flags.json"
ENGINE_VERSION = "fundamentals-v64"
GATE_MINUTES = 1440
TIMEOUT = 40
LLAMA = "https://api.llama.fi"

# thresholds (tracked in config/trials-log.json)
FUND_MIN_REVENUE_30D_USD = 300_000
FUND_ACCEL_30D_PCT = 30.0
FUND_HIGH_HOLDER_YIELD_PCT = 8.0
FUND_HIGH_REVENUE_YIELD_PCT = 15.0

GOV_KEYWORDS = re.compile(r"\b(buy\s?-?backs?|burn\w*|fee\s?switch|revenue\s?shar\w*|tokenomics|"
                          r"emission\w*|staking\s?reward\w*|treasury)\b", re.I)


def now_utc():
    return datetime.now(timezone.utc)


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def get_json(url, data=None):
    headers = {"User-Agent": "investment-radar/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(data).encode()
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def fetch_overview(data_type):
    return get_json(f"{LLAMA}/overview/fees?excludeTotalDataChart=true"
                    f"&excludeTotalDataChartBreakdown=true&dataType={data_type}").get("protocols", [])


def fetch_protocol_index():
    return get_json(f"{LLAMA}/protocols")


def fetch_snapshot():
    q = {"query": "{ proposals(first: 300, where: {state: \"active\"}, orderBy: \"created\", "
                  "orderDirection: desc) { id title end space { id name } } }"}
    return (get_json("https://hub.snapshot.org/graphql", q).get("data") or {}).get("proposals", [])


def build_gecko_map(protocol_index):
    by_slug, by_name, by_parent = {}, {}, {}
    for p in protocol_index or []:
        gid = p.get("gecko_id")
        if not gid:
            continue
        if p.get("slug"):
            by_slug[p["slug"]] = gid
        if p.get("name"):
            by_name[p["name"].lower()] = gid
        if p.get("parentProtocol"):
            by_parent.setdefault(p["parentProtocol"], gid)
    return by_slug, by_name, by_parent


def resolve_gecko(entry, maps, universe_names):
    by_slug, by_name, by_parent = maps
    slug = entry.get("slug")
    if slug and slug in by_slug:
        return by_slug[slug]
    name = (entry.get("displayName") or entry.get("name") or "").lower()
    if name in by_name:
        return by_name[name]
    parent_key = f"parent#{entry.get('slug', '')}"
    if parent_key in by_parent:          # aggregated parent entry (e.g. a protocol with v2/v3 children)
        return by_parent[parent_key]
    return universe_names.get(name)


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def protocol_rows(revenue, holders, maps, coins_by_id, universe_names):
    holders_by_name = {(h.get("displayName") or h.get("name") or "").lower(): h for h in holders or []}
    rows = {}
    for e in revenue or []:
        r30 = f(e.get("total30d"))
        if not r30 or r30 < FUND_MIN_REVENUE_30D_USD:
            continue
        gid = resolve_gecko(e, maps, universe_names)
        name = e.get("displayName") or e.get("name")
        h = holders_by_name.get((name or "").lower(), {})
        h30 = f(h.get("total30d"))
        coin = coins_by_id.get(gid, {}) if gid else {}
        mcap = f(coin.get("market_cap_usd")) or f(e.get("mcap"))
        row = {"protocol": name, "gecko_id": gid, "category": e.get("category"),
               "revenue_24h_usd": f(e.get("total24h")), "revenue_7d_usd": f(e.get("total7d")),
               "revenue_30d_usd": r30, "revenue_annualised_usd": round(r30 * 365 / 30, 0),
               "revenue_growth_30d_over_30d_pct": f(e.get("change_30dover30d")),
               "revenue_growth_7d_over_7d_pct": f(e.get("change_7dover7d")),
               "holders_revenue_30d_usd": h30, "market_cap_usd": mcap}
        if mcap:
            row["revenue_yield_pct"] = round(row["revenue_annualised_usd"] / mcap * 100, 2)
            if h30:
                row["holder_yield_pct"] = round(h30 * 365 / 30 / mcap * 100, 2)
        row["flags"] = revenue_flags(row)
        key = gid or f"protocol:{(name or '').lower()}"
        # keep the largest-revenue entry per coin (parent vs child duplicates)
        if key not in rows or r30 > rows[key]["revenue_30d_usd"]:
            rows[key] = row
    return rows


def revenue_flags(row):
    fl = []
    g30, g7 = row.get("revenue_growth_30d_over_30d_pct"), row.get("revenue_growth_7d_over_7d_pct")
    if g30 is not None and g30 >= FUND_ACCEL_30D_PCT and (g7 or 0) > 0:
        fl.append("REVENUE_ACCELERATING")
    if (row.get("holder_yield_pct") or 0) >= FUND_HIGH_HOLDER_YIELD_PCT:
        fl.append("HIGH_HOLDER_YIELD")
    if (row.get("revenue_yield_pct") or 0) >= FUND_HIGH_REVENUE_YIELD_PCT:
        fl.append("CHEAP_VS_REVENUE")
    return fl


def governance_catalysts(proposals, universe_names):
    out = []
    for p in proposals or []:
        title = p.get("title") or ""
        if not GOV_KEYWORDS.search(title):
            continue
        sp = p.get("space") or {}
        sp_name = (sp.get("name") or "").lower()
        sp_id = (sp.get("id") or "").lower().split(".")[0]
        gid = universe_names.get(sp_name) or universe_names.get(sp_id)
        out.append({"coin": gid, "space": sp.get("name") or sp.get("id"), "title": title[:160],
                    "ends": datetime.fromtimestamp(int(p.get("end", 0)), tz=timezone.utc).date().isoformat()
                    if p.get("end") else None,
                    "link": f"https://snapshot.box/#/s:{sp.get('id')}/proposal/{p.get('id')}"})
    return [g for g in out if g["coin"]] + [g for g in out if not g["coin"]][:10]


def run(now=None, fetch=None):
    now = now or now_utc()
    fetch = fetch or {"revenue": lambda: fetch_overview("dailyRevenue"),
                      "holders": lambda: fetch_overview("dailyHoldersRevenue"),
                      "index": fetch_protocol_index, "snapshot": fetch_snapshot}
    prev = load(OUT, {})
    if prev.get("updated_at"):
        try:
            if now - datetime.fromisoformat(prev["updated_at"]) < timedelta(minutes=GATE_MINUTES - 5):
                print("Fundamentals gate closed (daily).")
                return None
        except ValueError:
            pass
    flags = load(FLAGS, {})
    coins_by_id = {c.get("id"): c for c in flags.get("coins", []) if c.get("id")}
    universe_names = {}
    for c in flags.get("coins", []):
        if c.get("name") and len(c["name"]) >= 4:
            universe_names[c["name"].lower()] = c.get("id")
    data, errors = {}, []
    for k, fn in fetch.items():
        try:
            data[k] = fn()
        except Exception as e:  # noqa: BLE001
            data[k] = None
            errors.append(f"{k}: {type(e).__name__}: {str(e)[:120]}")
    rows = protocol_rows(data.get("revenue"), data.get("holders"),
                         build_gecko_map(data.get("index")), coins_by_id, universe_names)
    ranked = sorted(rows.values(), key=lambda r: r["revenue_30d_usd"], reverse=True)
    out = {"updated_at": now.isoformat(), "engine_version": ENGINE_VERSION,
           "source": "DefiLlama fees/revenue (on-chain protocols only) + Snapshot active proposals",
           "protocols": ranked[:150],
           "flagged": {r["gecko_id"] or r["protocol"]: r["flags"] for r in ranked if r["flags"]},
           "governance_catalysts": governance_catalysts(data.get("snapshot"), universe_names),
           "errors": errors}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Fundamentals: {len(ranked)} protocols >= ${FUND_MIN_REVENUE_30D_USD:,}/30d, "
          f"{len(out['flagged'])} flagged, {len(out['governance_catalysts'])} governance catalyst(s)")
    for k, v in list(out["flagged"].items())[:15]:
        print(f"  - {k}: {', '.join(v)}")
    for e in errors:
        print(f"  ! {e}")
    return out


if __name__ == "__main__":
    try:
        run()
    except Exception as e:  # never fail the workflow
        print(f"fundamentals_revenue failed (non-fatal): {e}", file=sys.stderr)
    sys.exit(0)
