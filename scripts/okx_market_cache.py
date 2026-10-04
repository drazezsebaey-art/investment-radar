"""
okx_market_cache.py (v73) - keeps a rolling OKX market cache in Supabase for
the trading-desk page (Volume Profile + OI/price matrix for ANY coin it asks
about, refreshed every radar run).

What it writes (Supabase, service key, tables have RLS and no public access):
  public.okx_candles  - 1H spot candles for every live <COIN>-USDT pair on OKX,
                        last ~30 days (backfilled once per coin, then the last
                        3 candles every run), pruned past 32 days once a day
  public.okx_oi       - one open-interest snapshot (USD) per USDT-swap per run,
                        ONE request for all swaps; pruned past 8 days daily

Keyless OKX public endpoints (reachable from GitHub Actions, unlike Binance).
Single venue: OKX is a proxy, NOT a cross-exchange aggregate.
State: data/okx-cache-state.json (small: last candle ts per instrument).
Zero CoinGecko usage. Never fails the workflow.
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "okx-cache-state.json"
ENGINE_VERSION = "okx-cache-v73"
OKX = "https://www.okx.com/api/v5"
TIMEOUT = 20
BACKFILL_HOURS = 720            # 30 days of 1H candles
MAX_BACKFILL_PER_RUN = 120      # spread the one-time backfill over a few runs
RATE_PER_SEC = 12               # OKX market-data limit is 20/s per IP; stay well under
WORKERS = 6
PRUNE_EVERY_HOURS = 24
CHUNK = 4000

STABLE = {"USDT", "USDC", "DAI", "USDE", "FDUSD", "USDS", "PYUSD", "TUSD", "FRAX", "USD1", "RLUSD", "USDD",
          "BUSD", "GHO", "CRVUSD", "EURC", "USDG", "USD0", "PAXG", "XAUT", "USDTB", "BFUSD", "AUSD", "GUSD"}


def now_utc():
    return datetime.now(timezone.utc)


def load(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


class Limiter:
    def __init__(self, rate):
        self.gap = 1.0 / rate
        self.lock = threading.Lock()
        self.next = 0.0

    def wait(self):
        with self.lock:
            t = time.monotonic()
            if t < self.next:
                time.sleep(self.next - t)
            self.next = max(t, self.next) + self.gap


LIM = Limiter(RATE_PER_SEC)


def okx_get(path, params):
    LIM.wait()
    url = OKX + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "investment-radar/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode())
            if str(d.get("code")) == "0":
                return d.get("data") or []
            if str(d.get("code")) == "50011":  # rate limited
                time.sleep(1.5 * (attempt + 1))
                continue
            raise RuntimeError("OKX " + str(d.get("code")) + " " + str(d.get("msg")))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    return []


def spot_instruments():
    out = []
    for x in okx_get("/public/instruments", {"instType": "SPOT"}):
        if x.get("quoteCcy") == "USDT" and x.get("state") == "live" and x.get("baseCcy") not in STABLE:
            out.append(x["instId"])
    return sorted(out)


def rows_from(inst, data):
    rows = []
    for k in data:  # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        try:
            ts = datetime.fromtimestamp(int(k[0]) / 1000, timezone.utc).isoformat()
            rows.append({"inst": inst, "ts": ts, "o": float(k[1]), "h": float(k[2]), "l": float(k[3]), "c": float(k[4]),
                         "vol": float(k[5]), "vol_quote": float(k[7]) if len(k) > 7 and k[7] not in (None, "") else None})
        except Exception:
            continue
    return rows


def fetch_backfill(inst):
    rows, after = [], None
    while len(rows) < BACKFILL_HOURS:
        p = {"instId": inst, "bar": "1H", "limit": "300"}
        if after:
            p["after"] = after
        data = okx_get("/market/candles", p)
        if not data:
            break
        rows += rows_from(inst, data)
        after = data[-1][0]
        if len(data) < 300:
            break
    return rows[:BACKFILL_HOURS]


def fetch_latest(inst):
    return rows_from(inst, okx_get("/market/candles", {"instId": inst, "bar": "1H", "limit": "3"}))


def fetch_oi():
    out, ts = [], now_utc().replace(second=0, microsecond=0).isoformat()
    for x in okx_get("/public/open-interest", {"instType": "SWAP"}):
        inst = x.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        try:
            usd = float(x.get("oiUsd") or 0)
        except Exception:
            continue
        if usd > 0:
            out.append({"ccy": inst.split("-")[0], "ts": ts, "oi_usd": usd})
    return out


def sb(method, path, body=None):
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
    headers = {"apikey": key, "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}
    if not key.startswith("sb_"):
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url + "/rest/v1/" + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status


def upsert(table, conflict, rows):
    for i in range(0, len(rows), CHUNK):
        sb("POST", table + "?on_conflict=" + conflict, rows[i:i + CHUNK])


def main():
    t0 = time.time()
    state = load(STATE, {})
    last = state.get("last_ts", {})
    report = {"engine_version": ENGINE_VERSION, "at": now_utc().isoformat(), "errors": []}

    # 1) open interest: one request for every USDT swap
    try:
        oi = fetch_oi()
        upsert("okx_oi", "ccy,ts", oi)
        report["oi_rows"] = len(oi)
    except Exception as e:
        report["errors"].append("oi: " + str(e)[:160])

    # 2) candles
    try:
        insts = spot_instruments()
    except Exception as e:
        insts = []
        report["errors"].append("instruments: " + str(e)[:160])
    need_backfill = [i for i in insts if i not in last][:MAX_BACKFILL_PER_RUN]
    regular = [i for i in insts if i in last]
    rows, failed = [], 0

    def job(inst, backfill):
        try:
            return inst, (fetch_backfill(inst) if backfill else fetch_latest(inst))
        except Exception:
            return inst, None

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(job, i, True) for i in need_backfill] + [ex.submit(job, i, False) for i in regular]
        for f in futs:
            inst, r = f.result()
            if r is None:
                failed += 1
                continue
            if r:
                rows += r
                last[inst] = max(x["ts"] for x in r)
    try:
        upsert("okx_candles", "inst,ts", rows)
        report["candle_rows"] = len(rows)
    except Exception as e:
        report["errors"].append("candles upsert: " + str(e)[:160])
        for i in need_backfill:  # retry the backfill next run
            last.pop(i, None)
    report.update({"instruments": len(insts), "backfilled": len(need_backfill), "fetch_failed": failed,
                   "pending_backfill": max(0, len([i for i in insts if i not in last]))})

    # 3) daily prune
    lp = state.get("last_prune")
    if not lp or now_utc() - datetime.fromisoformat(lp) >= timedelta(hours=PRUNE_EVERY_HOURS):
        try:
            c_cut = (now_utc() - timedelta(days=32)).strftime("%Y-%m-%dT%H:%M:%SZ")
            o_cut = (now_utc() - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
            sb("DELETE", "okx_candles?ts=lt." + c_cut)
            sb("DELETE", "okx_oi?ts=lt." + o_cut)
            state["last_prune"] = now_utc().isoformat()
        except Exception as e:
            report["errors"].append("prune: " + str(e)[:160])

    report["seconds"] = round(time.time() - t0, 1)
    state.update({"last_ts": last, "last_report": report})
    STATE.write_text(json.dumps(state, indent=1), encoding="utf-8")
    print("okx_market_cache:", json.dumps(report))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never fail the workflow
        print("okx_market_cache: unexpected error:", e)
    sys.exit(0)
