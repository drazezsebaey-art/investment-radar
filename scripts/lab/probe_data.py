"""
probe_data.py - Research Contract RC-1.1, section 2: "first build step".

Answers, from a GitHub Actions runner (the machine that will run the lab):
  1. Is Binance's public archive (data.binance.vision) reachable?  -> venue decision
  2. Is the Binance REST API reachable, or geo-blocked (HTTP 451)? Is the
     market-data-only mirror data-api.binance.vision reachable?
  3. Which USDT spot pairs exist in the archive, INCLUDING delisted ones, and
     the first / last month each has daily candles -> point-in-time universe
     (Tier A) is possible or not.
  4. How far back do 1h candles, futures funding and futures metrics (OI,
     long/short ratios) go for a reference symbol?
  5. Timestamp unit of the archive files (Binance moved spot files to
     microseconds in 2025) - the engine must normalise it.
  6. OKX fallback: do history-candles reach 2024?

Writes data/lab/probe-report.json, data/lab/probe-report.md and
data/lab/symbol-coverage.json. Reads nothing from the repo; changes nothing
else. Standard library only. Run manually: Actions -> "Lab - data probe".
"""
import csv
import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "lab"
ARCHIVE = "https://data.binance.vision"
S3_LIST = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
REF = "BTCUSDT"
TIMEOUT = 30
UA = {"User-Agent": "investment-radar-lab-probe/1.0"}
CONTRACT = "RC-1.1"


def http(url, binary=False):
    """(status, body or None, error text)."""
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = r.read()
            return r.status, (data if binary else data.decode("utf-8", "replace")), None
    except urllib.error.HTTPError as e:
        return e.code, None, f"HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return None, None, f"{type(e).__name__}: {e}"[:200]


# ------------------------------------------------------------ parsers ----
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def parse_s3_listing(xml_text):
    """-> (common_prefixes, keys, next_marker or None)."""
    root = ET.fromstring(xml_text)
    prefixes = [p.text for p in root.findall("s3:CommonPrefixes/s3:Prefix", NS)]
    keys = [k.text for k in root.findall("s3:Contents/s3:Key", NS)]
    truncated = (root.findtext("s3:IsTruncated", default="false", namespaces=NS) or "").lower() == "true"
    nxt = root.findtext("s3:NextMarker", default=None, namespaces=NS)
    if truncated and not nxt:
        nxt = (keys or prefixes or [None])[-1]
    return prefixes, keys, (nxt if truncated else None)


def s3_list(prefix, delimiter="/", max_pages=50):
    prefixes, keys, marker = [], [], None
    for _ in range(max_pages):
        q = {"prefix": prefix}
        if delimiter:
            q["delimiter"] = delimiter
        if marker:
            q["marker"] = marker
        status, body, err = http(f"{S3_LIST}?{urllib.parse.urlencode(q)}")
        if status != 200 or body is None:
            raise RuntimeError(err or f"HTTP {status}")
        p, k, marker = parse_s3_listing(body)
        prefixes += p
        keys += k
        if not marker:
            break
    return prefixes, keys


def month_of(key):
    """'.../BTCUSDT-1d-2024-01.zip' -> '2024-01' (None for checksum files)."""
    if not key.endswith(".zip"):
        return None
    stem = key.rsplit("/", 1)[-1][:-4]
    parts = stem.split("-")
    return f"{parts[-2]}-{parts[-1]}" if len(parts) >= 3 else None


def day_of(key):
    if not key.endswith(".zip"):
        return None
    parts = key.rsplit("/", 1)[-1][:-4].split("-")
    return "-".join(parts[-3:]) if len(parts) >= 4 else None


def ts_unit(first_open_time):
    """Binance archive: ms (13 digits) before 2025, microseconds (16) after."""
    n = len(str(int(first_open_time)))
    return "microseconds" if n >= 16 else "milliseconds" if n >= 13 else "seconds"


def read_zip_csv_head(blob, n=3):
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            rows = list(csv.reader(io.TextIOWrapper(f, "utf-8")))
    return rows[:n], len(rows)


# -------------------------------------------------------------- checks ----
def check_archive_file(path):
    status, blob, err = http(f"{ARCHIVE}/{path}", binary=True)
    out = {"path": path, "status": status, "error": err}
    if status == 200 and blob:
        try:
            head, n = read_zip_csv_head(blob)
            out["rows"] = n
            out["head"] = head
            first = next((r for r in head if r and r[0].strip().isdigit()), None)
            if first:
                out["ts_unit"] = ts_unit(first[0])
        except Exception as e:  # noqa: BLE001
            out["parse_error"] = str(e)[:150]
    return out


def check_rest():
    res = {}
    for name, url in [
        ("api.binance.com", "https://api.binance.com/api/v3/ping"),
        ("data-api.binance.vision", f"https://data-api.binance.vision/api/v3/klines?symbol={REF}&interval=1h&limit=2"),
        ("fapi.binance.com", "https://fapi.binance.com/fapi/v1/ping"),
    ]:
        status, body, err = http(url)
        res[name] = {"status": status, "error": err, "ok": status == 200}
    return res


def earliest(prefix, kind="month"):
    _, keys = s3_list(prefix, delimiter="")
    vals = sorted(v for v in ((month_of(k) if kind == "month" else day_of(k)) for k in keys) if v)
    return (vals[0], vals[-1], len(vals)) if vals else (None, None, 0)


def symbol_coverage():
    """Every USDT spot pair in the archive with its first/last month of 1d candles."""
    prefixes, _ = s3_list("data/spot/monthly/klines/")
    syms = sorted(p.rstrip("/").rsplit("/", 1)[-1] for p in prefixes)
    usdt = [s for s in syms if s.endswith("USDT")]
    cov = {}
    for s in usdt:
        try:
            first, last, n = earliest(f"data/spot/monthly/klines/{s}/1d/")
            cov[s] = {"first_month": first, "last_month": last, "months": n}
        except Exception as e:  # noqa: BLE001
            cov[s] = {"error": str(e)[:120]}
        time.sleep(0.05)
    return len(syms), cov


def check_okx():
    after_ms = int(datetime(2024, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
    url = f"https://www.okx.com/api/v5/market/history-candles?instId=BTC-USDT&bar=1H&after={after_ms}&limit=5"
    status, body, err = http(url)
    out = {"status": status, "error": err}
    if body:
        try:
            rows = json.loads(body).get("data", [])
            out["rows"] = len(rows)
            if rows:
                out["first_ts"] = datetime.fromtimestamp(int(rows[-1][0]) / 1000, timezone.utc).isoformat()
        except ValueError:
            out["parse_error"] = True
    return out


def decide(rep):
    a = rep["archive"]
    spot_ok = a["spot_1h_2024_01"].get("status") == 200
    listing_ok = rep["coverage"].get("usdt_pairs", 0) > 0
    delisted = rep["coverage"].get("delisted_usdt_pairs", 0)
    venue = "BINANCE" if spot_ok and listing_ok else ("OKX_FALLBACK" if rep["okx"].get("rows") else "BLOCKED")
    tier = "A" if venue == "BINANCE" and delisted > 0 else "B"
    return {"venue": venue, "universe_tier": tier,
            "note": ("Binance archive reachable and lists delisted pairs -> point-in-time universe possible"
                     if tier == "A" else "no point-in-time universe -> all historical results are Tier B")}


def to_md(rep):
    d, c, a, r = rep["decision"], rep["coverage"], rep["archive"], rep["rest"]
    L = [f"# Lab data probe ({rep['contract']}) - {rep['run_at']}", "",
         f"**Decision:** venue **{d['venue']}**, universe **Tier {d['universe_tier']}** - {d['note']}", "",
         "| Check | Result |", "| --- | --- |"]
    for k, v in a.items():
        L.append(f"| archive {k} | {v.get('status')} {v.get('error') or ''} rows={v.get('rows', '-')} ts={v.get('ts_unit', '-')} |")
    for k, v in r.items():
        L.append(f"| REST {k} | {v.get('status')} {v.get('error') or ''} |")
    for k in ("ref_1h", "funding", "metrics"):
        v = rep["depth"].get(k, {})
        L.append(f"| depth {k} | first {v.get('first')} last {v.get('last')} n={v.get('n')} {v.get('error') or ''} |")
    L.append(f"| OKX fallback | {rep['okx'].get('status')} rows={rep['okx'].get('rows', '-')} first={rep['okx'].get('first_ts', '-')} |")
    L.append(f"| archive symbols (all) | {c.get('all_symbols')} |")
    L.append(f"| USDT pairs / active / delisted | {c.get('usdt_pairs')} / {c.get('active_usdt_pairs')} / {c.get('delisted_usdt_pairs')} |")
    L.append(f"| USDT pairs with data since 2024-01 | {c.get('since_2024_01')} |")
    return "\n".join(L) + "\n"


def main():
    now = datetime.now(timezone.utc)
    rep = {"contract": CONTRACT, "run_at": now.isoformat()}
    rep["archive"] = {
        "spot_1h_2024_01": check_archive_file(f"data/spot/monthly/klines/{REF}/1h/{REF}-1h-2024-01.zip"),
        "spot_1h_2025_06": check_archive_file(f"data/spot/monthly/klines/{REF}/1h/{REF}-1h-2025-06.zip"),
        "futures_funding_2024_01": check_archive_file(f"data/futures/um/monthly/fundingRate/{REF}/{REF}-fundingRate-2024-01.zip"),
        "futures_metrics_2024_01_01": check_archive_file(f"data/futures/um/daily/metrics/{REF}/{REF}-metrics-2024-01-01.zip"),
    }
    rep["rest"] = check_rest()
    rep["depth"] = {}
    for name, prefix, kind in [("ref_1h", f"data/spot/monthly/klines/{REF}/1h/", "month"),
                               ("funding", f"data/futures/um/monthly/fundingRate/{REF}/", "month"),
                               ("metrics", f"data/futures/um/daily/metrics/{REF}/", "day")]:
        try:
            f, l, n = earliest(prefix, kind)
            rep["depth"][name] = {"first": f, "last": l, "n": n}
        except Exception as e:  # noqa: BLE001
            rep["depth"][name] = {"error": str(e)[:150]}
    try:
        n_all, cov = symbol_coverage()
        last_full = max((v.get("last_month") or "") for v in cov.values()) if cov else ""
        active = [s for s, v in cov.items() if v.get("last_month") == last_full]
        rep["coverage"] = {"all_symbols": n_all, "usdt_pairs": len(cov), "latest_month_in_archive": last_full,
                           "active_usdt_pairs": len(active), "delisted_usdt_pairs": len(cov) - len(active),
                           "since_2024_01": sum(1 for v in cov.values() if v.get("first_month") and v["first_month"] <= "2024-01")}
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "symbol-coverage.json").write_text(json.dumps({"run_at": rep["run_at"], "pairs": cov}, indent=1), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        rep["coverage"] = {"error": str(e)[:200]}
    rep["okx"] = check_okx()
    rep["decision"] = decide(rep)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "probe-report.json").write_text(json.dumps(rep, indent=1, ensure_ascii=False), encoding="utf-8")
    (OUT / "probe-report.md").write_text(to_md(rep), encoding="utf-8")
    print(to_md(rep))


if __name__ == "__main__":
    main()
