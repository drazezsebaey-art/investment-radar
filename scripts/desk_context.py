"""
desk_context.py (v72) - feeds the "مكتب التداول اليومي" web page with the
inputs it cannot fetch itself, through a small Supabase table.

Why Supabase: the published claude.ai page cannot read GitHub (its security
policy blocks it), but it can read Supabase through the Supabase connector.

What it writes (table public.desk_context, one row per key, upserted):
  - fng           : Fear & Greed index (alternative.me, keyless)          gate 60 min
  - us_calendar   : this week's USD High/Medium events (Forex Factory    gate 180 min
                    weekly JSON feed, keyless)
  - btc_dominance : BTC.D now + ~24h ago, from data/altseason.json        every run
                    (CoinPaprika, written by altseason_regime.py)
  - cmc_events    : coin events for the next 7 days from CoinMarketCal   gate 360 min
                    (OPTIONAL: only when COINMARKETCAL_API_KEY is set;
                    untested until a key is added)

Also writes a local copy to data/desk-context.json for the record.
Secrets: SUPABASE_URL, SUPABASE_SERVICE_KEY (service role key; the table has
RLS on and no public policies, so only this key and the connector can read it).
Never fails the workflow.
"""
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "desk-context.json"
ALTSEASON = ROOT / "data" / "altseason.json"
ENGINE_VERSION = "desk-context-v72.1"
TIMEOUT = 20
GATES = {"fng": 60, "us_calendar": 180, "cmc_events": 360}

FNG_URL = "https://api.alternative.me/fng/?limit=2"
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
CMC_URL = "https://developers.coinmarketcal.com/v1/events"


def now_utc():
    return datetime.now(timezone.utc)


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def get_json(url, headers=None):
    h = {"User-Agent": "investment-radar/1.0", "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def due(state, key):
    last = state.get(key, {}).get("fetched_at")
    if not last:
        return True
    try:
        return now_utc() - datetime.fromisoformat(last) >= timedelta(minutes=GATES[key])
    except Exception:
        return True


def fetch_fng():
    d = get_json(FNG_URL)["data"]
    cur = d[0]
    prev = d[1] if len(d) > 1 else None
    return {"value": int(cur["value"]), "label": cur.get("value_classification"),
            "prev": int(prev["value"]) if prev else None,
            "as_of": datetime.fromtimestamp(int(cur["timestamp"]), timezone.utc).isoformat()}


def fetch_us_calendar():
    events = []
    for e in get_json(FF_URL):
        if e.get("country") != "USD" or e.get("impact") not in ("High", "Medium"):
            continue
        events.append({"title": e.get("title"), "date": e.get("date"), "impact": e.get("impact"),
                       "forecast": e.get("forecast") or None, "previous": e.get("previous") or None})
    events.sort(key=lambda x: x["date"] or "")
    return {"events": events, "source": "Forex Factory weekly feed"}


def fetch_btc_dominance():
    hist = load(ALTSEASON, {}).get("history") or []
    pts = [(h.get("ts") or h.get("at") or h.get("time"), h.get("btc_dominance_pct")) for h in hist]
    pts = [(t, v) for t, v in pts if t and v is not None]
    if not pts:
        return None
    t_now, v_now = pts[-1]
    v_24 = None
    try:
        tn = datetime.fromisoformat(t_now.replace("Z", "+00:00"))
        for t, v in reversed(pts):
            if tn - datetime.fromisoformat(t.replace("Z", "+00:00")) >= timedelta(hours=20):
                v_24 = v
                break
    except Exception:
        pass
    alt_risk = load(ALTSEASON, {}).get("alt_risk")
    if isinstance(alt_risk, dict):
        alt_risk = alt_risk.get("level") or alt_risk.get("state")
    return {"now": v_now, "ago_24h": v_24, "alt_risk": alt_risk, "delta_24h": (v_now - v_24) if v_24 is not None else None,
            "as_of": t_now, "source": "CoinPaprika via altseason.json"}


def fetch_cmc_events(key):
    end = (now_utc() + timedelta(days=7)).strftime("%d/%m/%Y")
    url = CMC_URL + "?max=75&dateRangeEnd=" + end
    d = get_json(url, {"x-api-key": key})
    out = []
    for e in (d.get("body") or []):
        title = e.get("title")
        if isinstance(title, dict):
            title = title.get("en")
        out.append({"title": title, "date": e.get("date_event"),
                    "coins": [c.get("symbol") for c in (e.get("coins") or []) if c.get("symbol")],
                    "categories": [c.get("name") for c in (e.get("categories") or []) if isinstance(c, dict)]})
    return {"events": out, "source": "CoinMarketCal"}


def upsert(rows):
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("desk_context: SUPABASE_URL / SUPABASE_SERVICE_KEY not set - local file only")
        return False
    body = json.dumps(rows).encode()
    headers = {"apikey": key, "Content-Type": "application/json",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    if not key.startswith("sb_"):  # legacy JWT service_role key; new sb_secret_ keys go in apikey only
        headers["Authorization"] = "Bearer " + key
    req = urllib.request.Request(url + "/rest/v1/desk_context?on_conflict=key", data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        print("desk_context: upserted", [x["key"] for x in rows], "HTTP", r.status)
    return True


def main():
    state = load(OUT, {})
    rows = []
    stamp = now_utc().isoformat()
    jobs = [("fng", fetch_fng), ("us_calendar", fetch_us_calendar)]
    cmc_key = os.environ.get("COINMARKETCAL_API_KEY", "")
    if cmc_key:
        jobs.append(("cmc_events", lambda: fetch_cmc_events(cmc_key)))
    for key, fn in jobs:
        if not due(state, key):
            continue
        try:
            data = fn()
            state[key] = {"data": data, "fetched_at": stamp, "error": None}
            rows.append({"key": key, "data": data, "updated_at": stamp})
        except Exception as e:
            prev = state.get(key, {})
            state[key] = {**prev, "error": str(e)[:200], "error_at": stamp}
            print("desk_context:", key, "failed:", e)
    try:
        btcd = fetch_btc_dominance()
        if btcd:
            state["btc_dominance"] = {"data": btcd, "fetched_at": stamp}
            rows.append({"key": "btc_dominance", "data": btcd, "updated_at": stamp})
    except Exception as e:
        print("desk_context: btc_dominance failed:", e)
    state["engine_version"] = ENGINE_VERSION
    OUT.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    if rows:
        try:
            upsert(rows)
        except Exception as e:
            print("desk_context: supabase upsert failed:", e)
    else:
        print("desk_context: nothing due this run")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never fail the workflow
        print("desk_context: unexpected error:", e)
    sys.exit(0)
